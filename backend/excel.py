"""Read-only XLSX extraction preserving sheet, row and cell references."""

import io
import zipfile
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook

def safe_excel_error_reason(error: Exception) -> str:
    """Allowlisted diagnostics only: never echo paths, cell values or XML."""
    message = str(error)
    if isinstance(error, OSError):
        return "FILE_UNAVAILABLE: uploaded bytes could not be read; remove this upload and upload the workbook again"
    if message.startswith("Duplicate header "):
        return "DUPLICATE_HEADERS: repeated column names make field mapping ambiguous; export unique headers without changing their meaning"
    if message.startswith("Invalid XLSX workbook."):
        return "INVALID_XLSX: file is not a readable standard XLSX package; save a new unencrypted Excel Workbook (.xlsx) copy"
    return "UNSUPPORTED_WORKBOOK_STRUCTURE: workbook could not be parsed; attach the original failing file for diagnosis"


def extract_excel(raw: bytes | Path, *, include_text: bool = True) -> tuple[str, dict]:
    # Uploaded XLSX files are already on disk. Stream directly from that file
    # instead of retaining another compressed-workbook byte copy in memory.
    source = raw if isinstance(raw, Path) else io.BytesIO(raw)
    try:
        with zipfile.ZipFile(source) as archive:
            expanded_size = sum(item.file_size for item in archive.infolist())
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
    cells_seen = meaningful_cells = formula_count = 0
    try:
        for sheet in book.worksheets:
            # Do not trust declared dimensions; stream actual rows and count their cells.
            sheet.reset_dimensions()
            headers = []
            header_row = None
            data_rows = columns = 0
            is_reference = sheet.title.casefold().strip() in {"readme", "instructions", "guidelines"}
            if include_text:
                parts.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows():
                cells_seen += len(row)
                nonempty = [cell for cell in row if cell.value is not None and str(cell.value).strip()]
                meaningful_cells += len(nonempty)
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
                    parts.append(line)
            sheets.append({"name": sheet.title, "headers": headers, "header_row": header_row,
                           "data_rows": data_rows, "columns": columns, "is_reference": is_reference})
        total_rows = sum(sheet["data_rows"] for sheet in sheets if not sheet["is_reference"])
        metadata = {"sheet_names": book.sheetnames, "sheets": sheets, "data_row_count": total_rows,
                    "records": records,
                    "meaningful_cell_count": meaningful_cells,
                    "visited_cell_count": cells_seen,
                    "meaningful_cell_limit": None,
                    "visited_cell_limit": None,
                    "expanded_size_bytes": expanded_size,
                    "formula_count": formula_count,
                    "formula_policy": "Formulas are preserved as text and never evaluated."}
        if total_rows == 0:
            metadata["warning"] = "Schema-only workbook: no data records were found outside reference sheets. No borrower decision can be made."
        return "\n".join(parts), metadata
    finally:
        book.close()
        workbook_stream.close()