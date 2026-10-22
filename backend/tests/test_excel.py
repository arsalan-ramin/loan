"""XLSX extraction and local dataset imports, without touching original workbooks."""

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from backend.agents import ExtractionAgent, RulesAgent
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


def test_local_dataset_import_is_explicit_atomic_and_preserves_sources(tmp_path):
    root = tmp_path / "data"
    app = create_app(root)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.post(f"/api/sessions/{sid}/dataset").status_code == 404
        assert client.get(f"/api/sessions/{sid}").json()["documents"] == []
        raw = workbook_bytes(False)
        for name in ("MTB_Query_1.xlsx", "MTB_Query_2.xlsx", "MTB_Query_4.xlsx"):
            (root / name).write_bytes(raw)
        response = client.post(f"/api/sessions/{sid}/dataset")
        assert response.status_code == 200
        session = response.json()
        assert session["status"] == "ready"
        assert session["report"] is None
        assert len(session["documents"]) == 3
        assert client.post(f"/api/sessions/{sid}/dataset").status_code == 409
        assert (root / "MTB_Query_1.xlsx").read_bytes() == raw


def test_excel_upload_and_invalid_workbook(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        response = client.post(f"/api/sessions/{sid}/documents", files=[("files", ("test.xlsx", workbook_bytes()))])
        assert response.status_code == 200
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a zip archive")
    with pytest.raises(ValueError, match="Invalid XLSX"):
        ExtractionAgent().extract(Document(name="bad.xlsx", extension=".xlsx", size=18), bad)