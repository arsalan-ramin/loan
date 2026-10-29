"""Bounded XLSX extraction preserving sheet, row and cell references."""

import io
import zipfile
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook

MAX_CELLS = 5_000_000  # Meaningful cells, including headers and reference sheets.
MAX_SCAN_CELLS = 50_000_000  # All streamed row positions, including padding.
MAX_TEXT = 50_000_000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024


def safe_excel_error_reason(error: Exception) -> str:
    """Allowlisted diagnostics only: never echo paths, cell values or XML."""
    message = str(error)
    if isinstance(error, OSError):
        return "FILE_UNAVAILABLE: uploaded bytes could not be read; remove this upload and upload the workbook again"
    if message.startswith("Duplicate header "):
        return "DUPLICATE_HEADERS: repeated column names make field mapping ambiguous; export unique headers without changing their meaning"
    if message.startswith("Invalid XLSX workbook."):
        return "INVALID_XLSX: file is not a readable standard XLSX package; save a new unencrypted Excel Workbook (.xlsx) copy"
    limits = {
        "Expanded XLSX exceeds the 512 MB limit.": "EXPANDED_SIZE_LIMIT: expanded workbook exceeds 512 MB",
        "XLSX exceeds the 5,000,000 meaningful-cell limit.": "CELL_LIMIT: workbook exceeds 5,000,000 meaningful nonblank cells; split the data across workbooks",
        "XLSX exceeds the 50,000,000 visited-cell scan limit.": "SCAN_CELL_LIMIT: workbook scan exceeds 50,000,000 visited cell positions; remove blank or styled padding and export only the used data range",
        "XLSX exceeds the 5,000,000 visited-cell limit.": "CELL_LIMIT: workbook exceeds 5,000,000 visited cells; export only the used data range",
        "Expanded XLSX exceeds the 250 MB limit.": "EXPANDED_SIZE_LIMIT: expanded workbook exceeds 250 MB",
        "XLSX files are limited to 30 sheets.": "SHEET_LIMIT: workbook exceeds 30 worksheets",
        "XLSX exceeds the 1,000,000 visited-cell limit.": "CELL_LIMIT: workbook exceeds 1,000,000 visited cells; export only the used data range",
        "XLSX extracted text exceeds the 50 million character limit.": "TEXT_LIMIT: extracted workbook text exceeds 50 million characters",
    }
    return limits.get(message, "UNSUPPORTED_WORKBOOK_STRUCTURE: workbook could not be parsed; attach the original failing file for diagnosis")


def extract_excel(raw: bytes | Path, *, include_text: bool = True) -> tuple[str, dict]:
    # Uploaded XLSX files are already on disk. Stream directly from that file
    # instead of retaining another compressed-workbook byte copy in memory.
    source = raw if isinstance(raw, Path) else io.BytesIO(raw)
    try:
        with zipfile.ZipFile(source) as archive:
            expanded_size = sum(item.file_size for item in archive.infolist())
            if expanded_size > MAX_EXPANDED_BYTES:
                raise ValueError("Expanded XLSX exceeds the 512 MB limit.")
        if not isinstance(source, Path):
            source.seek(0)
        # Upload storage names are opaque IDs without an .xlsx extension;
        # pass an open handle so openpyxl validates bytes, not the pathname.
        workbook_stream = source.open("rb") if isinstance(source, Path) else source
        try:
            book = load_workbook(workbook_stream, read_only=True, data_only=False, keep_links=False)
        except Exception:
            workbook_stream.close()
            raise
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("Invalid XLSX workbook. Re-save it as a standard .xlsx file.") from exc
    parts: list[str] = []
    sheets = []
    records = []
    cells_seen = meaningful_cells = text_length = formula_count = 0
    try:
        if len(book.worksheets) > 30:
            raise ValueError("XLSX files are limited to 30 sheets.")
        for sheet in book.worksheets:
            # Do not trust declared dimensions; bound both scanning and meaningful data.
            sheet.reset_dimensions()
            headers = []
            header_row = None
            data_rows = columns = 0
            is_reference = sheet.title.casefold().strip() in {"readme", "instructions", "guidelines"}
            if include_text:
                parts.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows():
                cells_seen += len(row)
                if cells_seen > MAX_SCAN_CELLS:
                    raise ValueError("XLSX exceeds the 50,000,000 visited-cell scan limit.")
                nonempty = [cell for cell in row if cell.value is not None and str(cell.value).strip()]
                meaningful_cells += len(nonempty)
                if meaningful_cells > MAX_CELLS:
                    raise ValueError("XLSX exceeds the 5,000,000 meaningful-cell limit.")
                if not nonempty:
                    continue
                columns = max(columns, max(cell.column for cell in nonempty))
                if header_row is None:
                    header_row = nonempty[0].row
                    headers = [str(cell.value) if cell.value is not None else "" for cell in row]
                else:
                    data_rows += 1
                    if not is_reference:
                        record = {}
                        sources = {}
                        for cell in nonempty:
                            column = cell.column - 1
                            key = headers[column] if column < len(headers) else ""
                            if not key:
                                continue
                            if key in record:
                                raise ValueError(f"Duplicate header {key!r} in sheet {sheet.title}; field mapping is ambiguous.")
                            # Formulas are unknown for policy, never their displayed expression or cached values.
                            value = None if cell.data_type == "f" else cell.value
                            if isinstance(value, (datetime, date, time)):
                                value = value.isoformat()
                            record[key] = value
                            sources[key] = f"{sheet.title}!{cell.coordinate}"
                        records.append({"sheet": sheet.title, "row": nonempty[0].row,
                                        "values": record, "sources": sources})
                for cell in nonempty:
                    if cell.data_type == "f":
                        formula_count += 1
                if include_text:
                    values = []
                    for cell in nonempty:
                        value = cell.value
                        if isinstance(value, (datetime, date, time)):
                            value = value.isoformat()
                        if cell.data_type == "f":
                            value = f"FORMULA (not evaluated): {value}"
                        values.append(f"{cell.coordinate}={value}")
                    line = f"[{sheet.title}!row {nonempty[0].row}] " + " | ".join(values)
                    text_length += len(line)
                    if text_length > MAX_TEXT:
                        raise ValueError("XLSX extracted text exceeds the 50 million character limit.")
                    parts.append(line)
            sheets.append({"name": sheet.title, "headers": headers, "header_row": header_row,
                           "data_rows": data_rows, "columns": columns, "is_reference": is_reference})
        total_rows = sum(sheet["data_rows"] for sheet in sheets if not sheet["is_reference"])
        metadata = {"sheet_names": book.sheetnames, "sheets": sheets, "data_row_count": total_rows,
                    "records": records,
                    "meaningful_cell_count": meaningful_cells,
                    "visited_cell_count": cells_seen,
                    "meaningful_cell_limit": MAX_CELLS,
                    "visited_cell_limit": MAX_SCAN_CELLS,
                    "expanded_size_bytes": expanded_size,
                    "formula_count": formula_count,
                    "formula_policy": "Formulas are preserved as text and never evaluated."}
        if total_rows == 0:
            metadata["warning"] = "Schema-only workbook: no data records were found outside reference sheets. No borrower decision can be made."
        return "\n".join(parts), metadata
    finally:
        book.close()
        workbook_stream.close()