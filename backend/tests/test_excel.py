"""XLSX extraction and local dataset imports, without touching original workbooks."""

import ast
import io
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.styles import PatternFill

from backend.agents import ExtractionAgent, RulesAgent
from backend import excel
from backend.main import create_app
from backend.models import Document, Session


def workbook_bytes(records=True):
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "MTB Query 1"
    sheet.append(["Account", "Score", "Calculation"])
    if records:
        sheet.append(["SYNTHETIC-001", 700, "=1+1"])
    readme = book.create_sheet("README")
    readme.append(["Purpose", "Schema template"])
    readme.append(["Notes", "Not a borrower record"])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


def extract(tmp_path, records=True):
    path = tmp_path / "test.xlsx"
    raw = workbook_bytes(records)
    path.write_bytes(raw)
    document = Document(name="test.xlsx", extension=".xlsx", size=len(raw))
    ExtractionAgent().extract(document, path)
    return document


def test_excel_records_metadata_and_formula_text(tmp_path):
    doc = extract(tmp_path)
    assert doc.metadata["data_row_count"] == 1
    assert doc.metadata["sheets"][0]["headers"] == ["Account", "Score", "Calculation"]
    assert doc.metadata["sheets"][1]["is_reference"] is True
    assert doc.metadata["formula_count"] == 1
    assert "FORMULA (not evaluated): =1+1" in doc.content
    assert "[MTB Query 1!row 2] A2=SYNTHETIC-001" in doc.content


def test_excel_resource_guards_are_removed():
    # Check the removed guards without allocating millions of cells/ZIP bytes.
    tree = ast.parse(inspect.getsource(excel))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not {"MAX_CELLS", "MAX_SCAN_CELLS", "MAX_TEXT", "MAX_EXPANDED_BYTES"} & names
    assert not any(isinstance(node, ast.Compare) and any(
        isinstance(child, ast.Name) and child.id == "expanded_size"
        for child in ast.walk(node)) for node in ast.walk(tree))


@pytest.mark.parametrize("include_text", [False, True])
def test_large_query_one_exceeds_old_million_cell_limit(tmp_path, include_text, monkeypatch):
    book = Workbook(write_only=True)
    sheet = book.create_sheet("MTB Query 1")
    sheet.append(["Id", *[f"Source_Field_{i}" for i in range(1, 64)], "Account_ID"])
    for index in range(16_000):
        sheet.append([f"SYNTHETIC-{index}", *([None] * 63), "SYNTHETIC-ACCOUNT"])
    path = tmp_path / "large-query-one.xlsx"
    book.save(path)
    book.close()
    # Prove the upload path never calls Path.read_bytes for an XLSX.
    monkeypatch.setattr(type(path), "read_bytes", lambda self: pytest.fail("XLSX must stream from disk"))
    doc = Document(name="MTB_Query_1.xlsx", extension=".xlsx", size=path.stat().st_size)
    ExtractionAgent().extract(doc, path, include_text=include_text)
    assert doc.metadata["visited_cell_count"] > 1_000_000
    assert doc.metadata["data_row_count"] == 16_000
    assert len(doc.metadata["records"]) == 16_000
    assert doc.metadata["records"][-1]["values"]["Id"] == "SYNTHETIC-15999"
    assert doc.metadata["records"][-1]["sources"]["Account_ID"].endswith("BM16001")
    assert bool(doc.content) is include_text


@pytest.mark.parametrize("include_text", [False, True])
def test_excel_accepts_more_than_thirty_sheets(include_text):
    book = Workbook()
    book.remove(book.active)
    for index in range(31):
        sheet = book.create_sheet(f"Sheet {index}")
        sheet.append(["Id"])
        sheet.append([f"SYNTHETIC-{index}"])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    text, metadata = excel.extract_excel(buffer.getvalue(), include_text=include_text)
    assert len(metadata["sheet_names"]) == metadata["data_row_count"] == 31
    assert metadata["meaningful_cell_count"] == metadata["visited_cell_count"] == 62
    assert metadata["records"][-1]["values"] == {"Id": "SYNTHETIC-30"}
    assert bool(text) is include_text


def padded_workbook_bytes():
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Loans"
    sheet.append(["Id", None, "Amount"])
    sheet.append(["SYNTHETIC-001", None, 0])
    for row in (1, 2, 3):
        sheet.cell(row, 12).fill = PatternFill(fill_type="solid", fgColor="FFFF00")
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


@pytest.mark.parametrize("include_text", [False, True])
def test_styled_padding_counts_visited_and_meaningful_cells(include_text):
    text, metadata = excel.extract_excel(padded_workbook_bytes(), include_text=include_text)
    assert metadata["meaningful_cell_count"] == 4
    assert metadata["visited_cell_count"] == 36
    assert metadata["meaningful_cell_limit"] is None
    assert metadata["visited_cell_limit"] is None
    assert metadata["sheets"][0]["headers"] == ["Id", "", "Amount", *([""] * 9)]
    assert metadata["data_row_count"] == 1
    assert metadata["records"] == [{"sheet": "Loans", "row": 2,
                                    "values": {"Id": "SYNTHETIC-001", "Amount": 0},
                                    "sources": {"Id": "Loans!A2", "Amount": "Loans!C2"}}]
    assert bool(text) is include_text


@pytest.mark.parametrize("include_text", [False, True])
def test_whitespace_is_blank_but_raw_nonblank_values_are_preserved(include_text):
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Loans"
    sheet.append(["Id", "Whitespace", "Zero", "False", "Formula", "Raw", "Missing"])
    sheet.append(["SYNTHETIC-001", " \t\n", 0, False, '=""', "  raw \t", None])
    sheet.append([" \t\n"] * 7)
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    text, metadata = excel.extract_excel(buffer.getvalue(), include_text=include_text)
    assert metadata["meaningful_cell_count"] == 12
    # reset_dimensions streams row widths 7, 6, 7; absent G2 is not visited.
    assert metadata["visited_cell_count"] == 20
    assert metadata["data_row_count"] == len(metadata["records"]) == 1
    record = metadata["records"][0]
    assert record["values"] == {"Id": "SYNTHETIC-001", "Zero": 0, "False": False,
                                "Formula": None, "Raw": "  raw \t"}
    assert record["values"]["False"] is False
    assert record["sources"] == {"Id": "Loans!A2", "Zero": "Loans!C2", "False": "Loans!D2",
                                 "Formula": "Loans!E2", "Raw": "Loans!F2"}
    assert metadata["formula_count"] == 1
    if include_text:
        assert 'E2=FORMULA (not evaluated): =""' in text
        assert "F2=  raw \t" in text
        assert "B2=" not in text
        assert "row 3" not in text
    else:
        assert text == ""


@pytest.mark.parametrize("value", [0, False, '=""'])
@pytest.mark.parametrize("include_text", [False, True])
def test_zero_false_and_formulas_count_as_meaningful(value, include_text):
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["Value"])
    sheet.append([value])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    _, metadata = excel.extract_excel(buffer.getvalue(), include_text=include_text)
    assert metadata["meaningful_cell_count"] == 2
    assert metadata["data_row_count"] == 1
    assert metadata["records"][0]["values"]["Value"] == (None if value == '=""' else value)


@pytest.mark.parametrize("include_text", [False, True])
def test_visited_count_includes_trailing_styled_blank_rows(include_text):
    _, metadata = excel.extract_excel(padded_workbook_bytes(), include_text=include_text)
    assert metadata["visited_cell_count"] == 36
    assert metadata["meaningful_cell_count"] == 4


@pytest.mark.parametrize("include_text", [False, True])
def test_meaningful_count_is_workbook_wide(include_text):
    # Six cells on the data sheet plus four on the reference sheet.
    _, metadata = excel.extract_excel(workbook_bytes(), include_text=include_text)
    assert metadata["meaningful_cell_count"] == 10
    assert metadata["meaningful_cell_limit"] is None


def test_excel_extraction_accepts_text_above_previous_limit(tmp_path):
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Loans"
    sheet.append(["Description"])
    long_text = "x" * 32_767
    for _ in range(70):
        sheet.append([long_text])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()

    path = tmp_path / "large.xlsx"
    path.write_bytes(buffer.getvalue())
    document = Document(name="large.xlsx", extension=".xlsx", size=path.stat().st_size)
    ExtractionAgent().extract(document, path)

    assert document.metadata["character_count"] > 2_000_000
    assert document.content.count(long_text) == 70


def test_excel_extraction_accepts_text_above_previous_10m_limit(tmp_path):
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Loans"
    sheet.append(["Description"])
    long_text = "y" * 32_767
    for _ in range(320):
        sheet.append([long_text])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()

    path = tmp_path / "very-large.xlsx"
    path.write_bytes(buffer.getvalue())
    document = Document(name="very-large.xlsx", extension=".xlsx", size=path.stat().st_size)
    ExtractionAgent().extract(document, path)

    assert document.metadata["character_count"] > 10_000_000
    assert document.content.count(long_text) == 320


def test_excel_extraction_accepts_more_than_previous_cell_limit(tmp_path):
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Loans"
    sheet.append(["Account", *([None] * 18), "Amount"])
    for row_number in range(1, 10_002):
        sheet.append([f"SYNTHETIC-{row_number}", *([None] * 18), row_number])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()

    path = tmp_path / "many-cells.xlsx"
    path.write_bytes(buffer.getvalue())
    document = Document(name="many-cells.xlsx", extension=".xlsx", size=path.stat().st_size)
    ExtractionAgent().extract(document, path)

    assert document.metadata["data_row_count"] == 10_001
    assert "SYNTHETIC-10001" in document.content


def test_schema_only_workbook_cannot_pass_unnoticed(tmp_path):
    doc = extract(tmp_path, records=False)
    assert doc.metadata["data_row_count"] == 0
    assert "Schema-only" in doc.metadata["warning"]
    findings = RulesAgent().evaluate(Session(documents=[doc]))
    assert any(f.rule_id == "system-excel-records" and f.status == "review" for f in findings)


def test_mtb_dataset_requires_verified_policy(tmp_path):
    doc = extract(tmp_path)
    doc.name = "MTB_Query_1.xlsx"
    assert any(f.rule_id == "system-mtb-guideline" for f in RulesAgent().evaluate(Session(documents=[doc])))


def test_local_dataset_import_is_explicit_atomic_and_preserves_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIO_MAX_XLSX_MB", "0")
    # Import must stream, never read whole source workbooks into byte buffers.
    original_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("dataset must stream from disk"))
    root = tmp_path / "data"
    app = create_app(root)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.post(f"/api/sessions/{sid}/dataset").status_code == 404
        assert client.get(f"/api/sessions/{sid}").json()["documents"] == []
        raw = workbook_bytes(False)
        for name in ("MTB_Query_1.xlsx", "MTB_Query_2.xlsx"):
            (root / name).write_bytes(raw)
        assert client.post(f"/api/sessions/{sid}/dataset").status_code == 404
        assert client.get(f"/api/sessions/{sid}").json()["documents"] == []
        for name in ("MTB_Query_1.xlsx", "MTB_Query_2.xlsx", "MTB_Query_4.xlsx"):
            (root / name).write_bytes(raw)
        response = client.post(f"/api/sessions/{sid}/dataset")
        assert response.status_code == 200
        session = response.json()
        assert session["status"] == "ready"
        assert session["report"] is None
        assert len(session["documents"]) == 3
        assert client.post(f"/api/sessions/{sid}/dataset").status_code == 409
        assert original_read_bytes(root / "MTB_Query_1.xlsx") == raw


def test_excel_upload_and_invalid_workbook(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        response = client.post(f"/api/sessions/{sid}/documents", files=[("files", ("test.xlsx", workbook_bytes()))])
        assert response.status_code == 200
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a zip archive")
    with pytest.raises(ValueError, match="Invalid XLSX"):
        ExtractionAgent().extract(Document(name="bad.xlsx", extension=".xlsx", size=18), bad)


@pytest.mark.parametrize("error,code", [
    (ValueError("Duplicate header 'private-source-value' in sheet internal; field mapping is ambiguous."), "DUPLICATE_HEADERS"),
    (ValueError("Invalid XLSX workbook. Re-save it as a standard .xlsx file."), "INVALID_XLSX"),
    (ValueError("private-source-value C:/private/path"), "UNSUPPORTED_WORKBOOK_STRUCTURE"),
    (OSError("private-source-value C:/private/path"), "FILE_UNAVAILABLE"),
])
def test_excel_failure_reasons_are_allowlisted(error, code):
    reason = excel.safe_excel_error_reason(error)
    assert reason.startswith(code + ":")
    assert "private-source-value" not in reason
    assert "C:/private/path" not in reason