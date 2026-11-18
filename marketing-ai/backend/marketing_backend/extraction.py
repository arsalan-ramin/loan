import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

from docx import Document
from lxml.etree import XMLSyntaxError
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pptx import Presentation
from pptx.exc import PackageNotFoundError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_BYTES = 12 * 1024 * 1024
MAX_TEXT = 1_000_000
FORMATS = {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".md", ".json"}


def extract(name: str, raw: bytes) -> tuple[dict, list[tuple[str, str]]]:
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError("Unsupported file type.")
    if not raw:
        raise ValueError("The file is empty.")
    if len(raw) > MAX_BYTES:
        raise ValueError("The file exceeds the 12 MB limit.")
    metadata = {
        "format": suffix[1:], "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    sections: list[tuple[str, str]] = []
    length = 0

    def add(location: str, text: str) -> None:
        nonlocal length
        text = text.replace("\x00", "").strip()
        length += len(text)
        if length > MAX_TEXT or len(sections) >= 5000:
            raise ValueError("Extracted text exceeds the workspace limit. Split the file and retry.")
        if text:
            sections.append((location, text))

    stream = io.BytesIO(raw)
    try:
        if suffix in {".docx", ".pptx", ".xlsx"}:
            with zipfile.ZipFile(stream) as archive:
                entries = archive.infolist()
                if len(entries) > 4000 or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
                    raise ValueError("The expanded Office file is too large.")
            stream.seek(0)
        if suffix == ".pdf":
            reader = PdfReader(stream)
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported.")
            if len(reader.pages) > 300:
                raise ValueError("Split PDFs longer than 300 pages.")
            metadata["pages"] = len(reader.pages)
            if reader.metadata:
                metadata.update({key: str(value)[:300] for key, value in {
                    "title": reader.metadata.title, "author": reader.metadata.author,
                }.items() if value})
            for index, page in enumerate(reader.pages, 1):
                add(f"Page {index}", page.extract_text() or "")
        elif suffix in {".docx", ".pptx"}:
            document = Document(stream) if suffix == ".docx" else Presentation(stream)
            for key in ("title", "author"):
                value = getattr(document.core_properties, key)
                if value:
                    metadata[key] = value[:300]
            if suffix == ".docx":
                for index, paragraph in enumerate(document.paragraphs, 1):
                    add(f"Paragraph {index}", paragraph.text)
                for index, table in enumerate(document.tables, 1):
                    for row_index, row in enumerate(table.rows, 1):
                        add(f"Table {index}, row {row_index}", " | ".join(cell.text for cell in row.cells))
            else:
                metadata["slides"] = len(document.slides)

                def slide_text(shapes):
                    for shape in shapes:
                        if shape.has_text_frame:
                            yield shape.text
                        if shape.has_table:
                            for row in shape.table.rows:
                                yield " | ".join(cell.text for cell in row.cells)
                        if hasattr(shape, "shapes"):
                            yield from slide_text(shape.shapes)

                for index, slide in enumerate(document.slides, 1):
                    add(f"Slide {index}", "\n".join(slide_text(slide.shapes)))
        elif suffix == ".xlsx":
            workbook = load_workbook(stream, read_only=True, data_only=True)
            try:
                metadata["sheets"] = workbook.sheetnames
                for sheet in workbook.worksheets:
                    if (sheet.max_column or 0) > 256 or (sheet.max_row or 0) > 10000:
                        raise ValueError("Split wide or long workbook sheets before upload.")
                    rows = sheet.iter_rows(values_only=True)
                    headers = next(rows, ())
                    for index, row in enumerate(rows, 2):
                        add(f"{sheet.title}, row {index}", labeled_row(headers, row))
                    if headers and (sheet.max_row or 0) == 1:
                        add(f"{sheet.title}, row 1", labeled_row((), headers))
            finally:
                workbook.close()
        else:
            text = raw.decode("utf-8-sig")
            if "\x00" in text:
                raise ValueError("Expected a UTF-8 text file.")
            if suffix == ".csv":
                rows = csv.reader(io.StringIO(text), strict=True)
                headers = next(rows, [])
                metadata["columns"] = [column[:150] for column in headers[:100]]
                metadata["rows"] = 0
                for index, row in enumerate(rows, 2):
                    metadata["rows"] += 1
                    add(f"CSV record {index}", labeled_row(headers, row))
                if not metadata["rows"]:
                    add("CSV header", " | ".join(headers))
            else:
                if suffix == ".json":
                    value = json.loads(text)
                    if isinstance(value, dict):
                        metadata["keys"] = [key[:150] for key in list(value)[:100]]
                    text = json.dumps(value, ensure_ascii=False, indent=2)
                add("Document text", text)
    except UnicodeDecodeError as exc:
        raise ValueError("Save text files as UTF-8 and retry.") from exc
    except (PdfReadError, zipfile.BadZipFile, InvalidFileException, XMLSyntaxError,
            PackageNotFoundError, KeyError, TypeError, IndexError, RecursionError,
            csv.Error, json.JSONDecodeError) as exc:
        raise ValueError("The file could not be read. Check its format or export a fresh copy.") from exc

    if not sections:
        raise ValueError("No readable text found. Run OCR first for scanned files.")
    passages = [chunk for location, text in sections for chunk in split_text(location, text)]
    metadata.update(characters=length, passages=len(passages))
    return metadata, passages


def labeled_row(headers: tuple | list, row: tuple | list) -> str:
    return " | ".join(f"{headers[index] if index < len(headers) and headers[index] else f'Column {index + 1}'}: {value}"
                      for index, value in enumerate(row) if value is not None and value != "")


def split_text(location: str, text: str) -> list[tuple[str, str]]:
    text = re.sub(r"[ \t]+", " ", text)
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + 950, len(text))
        if end < len(text):
            boundary = max(text.rfind(delimiter, start + 650, end) for delimiter in ("\n", " "))
            if boundary > start:
                end = boundary
        excerpt = text[start:end].strip()
        if excerpt:
            chunks.append((location, excerpt))
        if end == len(text):
            break
        start = end - 100
    return chunks
