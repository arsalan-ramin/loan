"""Bounded XLSX extraction preserving sheet, row and cell references."""

import io
import zipfile
from datetime import date, datetime, time

from openpyxl import load_workbook

MAX_CELLS = 1_000_000
MAX_TEXT = 50_000_000
MAX_EXPANDED_BYTES = 250 * 1024 * 1024


def extract_excel(raw: bytes, *, include_text: bool = True) -> tuple[str, dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            expanded_size = sum(item.file_size for item in archive.infolist())
            if expanded_size > MAX_EXPANDED_BYTES:
                raise ValueError("Expanded XLSX exceeds the 250 MB limit.")
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("Invalid XLSX workbook. Re-save it as a standard .xlsx file.") from exc
    parts: list[str] = []
    sheets = []
    records = []
    cells_seen = text_length = formula_count = 0
    try:
        if len(book.worksheets) > 30:
            raise ValueError("XLSX files are limited to 30 sheets.")
        for sheet in book.worksheets:
            # Do not trust declared dimensions; count actual streamed cells with a hard budget.
            sheet.reset_dimensions()
            headers = []
            header_row = None
            data_rows = columns = 0
            is_reference = sheet.title.casefold().strip() in {"readme", "instructions", "guidelines"}
            if include_text:
                parts.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows():
                cells_seen += len(row)
                if cells_seen > MAX_CELLS:
                    raise ValueError("XLSX exceeds the 1,000,000 visited-cell limit.")
                nonempty = [cell for cell in row if cell.value is not None and str(cell.value).strip()]
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
                        for column, cell in enumerate(row):
                            key = headers[column] if column < len(headers) else ""
                            if not key or cell.value is None:
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
                    "formula_count": formula_count,
                    "formula_policy": "Formulas are preserved as text and never evaluated."}
        if total_rows == 0:
            metadata["warning"] = "Schema-only workbook: no data records were found outside reference sheets. No borrower decision can be made."
        return "\n".join(parts), metadata
    finally:
        book.close()