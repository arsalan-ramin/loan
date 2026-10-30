"""Stored, indexed Query 1/2/4 dataset: equivalence with streaming, replacement and reuse."""

import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import backend.loan_dataset as loan_dataset
from backend.llm import AnthropicLLM
from backend.loan_dataset import LoanDataset
from backend.loan_loading import load_loan_documents
from backend.main import create_app
from backend.models import Document
from backend.tests.test_large_preflight import synthetic_documents, workbook_bytes
from backend.tests.test_loan_loading import canonical_workbook


def relationship_workbook() -> bytes:
    return canonical_workbook([
        ("Indicator", ["Id", "analysis_id", "value"], [["I1", "D1", "Y"], ["IX", "DX", "N"]]),
        ("DataSourceAnalysis", ["Id", "entity_id"], [["D1", "E1"], ["DX", "EX"]]),
        ("LCREntity", ["Id", "response_id", "account_id"], [["E1", "R1", "A1"], ["EX", "RX", "A1"]]),
        ("LCRESPONSE", ["Id", "loan_id"], [["R1", "L1"], ["RX", "LX"]]),
        ("CreditReport", ["Id", "account_id", "personal_fico"],
         [["C1", "A1", "700"], ["C1", "A1", "600"], ["CX", "AX", "800"]]),
        ("Account", ["Id", "name"], [["A1", "Related"], ["AX", "Unrelated"]]),
        ("Loan", ["Id", "borrower_account_id", "response_id"],
         [["L1", "A1", "R1"], ["L2", "A1", "R2"], ["LX", "AX", "RX"]]),
    ])


def write_set(directory, count=40, *, canonical_query_1=False):
    directory.mkdir(parents=True, exist_ok=True)
    documents = synthetic_documents(count)
    files = []
    for document in documents:
        raw = workbook_bytes(document)
        if canonical_query_1 and "Query_1" in document.name:
            raw = relationship_workbook()
            document.name = "MTB_Query_1.xlsx"
        document.metadata = {}
        document.size = len(raw)
        (directory / document.id).write_bytes(raw)
        files.append((document.name, directory / document.id))
    return documents, files


def comparable(document: Document) -> tuple:
    metadata = {key: value for key, value in document.metadata.items() if key != "extracted_at"}
    return document.content, metadata


@pytest.mark.parametrize("canonical", [False, True])
def test_indexed_loading_matches_streamed_loading(tmp_path, canonical):
    documents, files = write_set(tmp_path / "files", canonical_query_1=canonical)
    dataset = LoanDataset(tmp_path / "data")
    assert dataset.replace(files) is True
    assert dataset.status()["status"] == "ready"
    for loan_id in ["L00000", "L00007", "L00039", "L1", "L2", "LX", "MISSING"]:
        streamed = [doc.model_copy(deep=True) for doc in documents]
        indexed = [doc.model_copy(deep=True) for doc in documents]
        load_loan_documents(streamed, tmp_path / "files", loan_id)
        load_loan_documents(indexed, tmp_path / "files", loan_id, dataset=dataset)
        assert [comparable(doc) for doc in indexed] == [comparable(doc) for doc in streamed], loan_id
        # A single document scopes its closure to that workbook only, as streaming does.
        single_streamed, single_indexed = streamed[0].model_copy(deep=True), indexed[0].model_copy(deep=True)
        load_loan_documents([single_streamed], tmp_path / "files", loan_id)
        load_loan_documents([single_indexed], tmp_path / "files", loan_id, dataset=dataset)
        assert comparable(single_indexed) == comparable(single_streamed)


def test_indexed_loading_does_not_parse_workbooks(tmp_path, monkeypatch):
    documents, files = write_set(tmp_path / "files")
    dataset = LoanDataset(tmp_path / "data")
    dataset.replace(files)
    import backend.loan_loading as loading
    monkeypatch.setattr(loading, "extract_excel", Mock(side_effect=AssertionError("must use the index")))
    load_loan_documents(documents, tmp_path / "files", "L00003", dataset=dataset)
    assert all(len(doc.metadata["records"]) == 1 for doc in documents)
    assert all("row-00003" in doc.content for doc in documents)


def test_modified_or_stale_files_fall_back_to_streaming(tmp_path, monkeypatch):
    documents, files = write_set(tmp_path / "files")
    dataset = LoanDataset(tmp_path / "data")
    dataset.replace(files)
    renamed = [doc.model_copy(deep=True) for doc in documents]
    renamed[0].name = "MTB_Query_1_copy.xlsx"
    assert dataset.load_scoped(renamed, "L00001") is False
    monkeypatch.setattr(loan_dataset, "fingerprint", lambda: "changed-mappings")
    load_loan_documents(documents, tmp_path / "files", "L00001", dataset=dataset)
    assert dataset.load_scoped(documents, "L00001") is False
    assert dataset.status()["status"] == "stale"
    assert dataset.stale_files() is not None


def test_new_upload_clears_all_previous_data(tmp_path):
    dataset = LoanDataset(tmp_path / "data")
    _, first = write_set(tmp_path / "first", count=30)
    dataset.replace(first)
    old_files = set(dataset.directory.glob("*.xlsx"))
    assert dataset.search_loans("L00025")[1] == 1
    _, second = write_set(tmp_path / "second", count=5)
    dataset.replace(second)
    state = dataset.status()
    assert state["status"] == "ready" and state["loan_count"] == 5
    assert dataset.search_loans("L00025")[1] == 0
    assert dataset.search_loans()[1] == 5
    assert not old_files & set(dataset.directory.glob("*.xlsx"))
    assert len(list(dataset.directory.glob("*.xlsx"))) == 3


def test_incomplete_set_keeps_existing_data(tmp_path):
    dataset = LoanDataset(tmp_path / "data")
    _, files = write_set(tmp_path / "files", count=4)
    dataset.replace(files)
    _, other = write_set(tmp_path / "other", count=9)
    assert dataset.replace(other[:2]) is False
    assert dataset.replace([other[0], other[0], other[1]]) is False
    assert dataset.status()["loan_count"] == 4


def test_loan_and_account_directory_search(tmp_path):
    dataset = LoanDataset(tmp_path / "data")
    _, files = write_set(tmp_path / "files", count=12)
    dataset.replace(files)
    loans, total = dataset.search_loans("L0001")
    assert total == 2 and {item.loan_id for item in loans} == {"L00010", "L00011"}
    assert loans[0].account_id == "A00010"
    accounts, total = dataset.search_accounts("Synthetic client 00004")
    assert total == 1 and accounts[0].loan_ids == ["L00004"]
    accounts, total = dataset.search_accounts("L00005")
    assert total == 1 and accounts[0].account_id == "A00005"
    assert dataset.search_loans("%")[1] == 0
    assert dataset.search_loans("l0001")[1] == 2
    assert dataset.search_loans("synthetic CLIENT 0001")[1] == 2
    assert dataset.search_accounts("a0000")[1] == 10
    assert dataset.search_loans("")[1] == 12


def test_lookups_seek_indexes_instead_of_scanning(tmp_path, monkeypatch):
    dataset = LoanDataset(tmp_path / "data")
    documents, files = write_set(tmp_path / "files", count=12)
    dataset.replace(files)
    statements = []
    connect = dataset.connect

    @contextmanager
    def tracing():
        with connect() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(dataset, "connect", tracing)
    for document in documents:
        document.metadata = {}
    load_loan_documents(documents, tmp_path / "files", "L00004", dataset=dataset)
    assert documents[0].metadata["scoped_loan_id"] == "L00004"
    monkeypatch.undo()

    with dataset.connect() as conn:
        conn.execute("CREATE TEMP TABLE dataset_frontier (tbl TEXT, key TEXT, value TEXT)")
        conn.execute("CREATE TEMP TABLE dataset_kept (id INTEGER PRIMARY KEY)")
        record_queries = [sql for sql in statements
                          if sql.lstrip().upper().startswith("SELECT") and "dataset_records" in sql]
        assert len(record_queries) >= 2
        for sql in record_queries:
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
            assert any("dataset_keys_lookup" in step or "INTEGER PRIMARY KEY" in step for step in plan), plan
            assert not any(step.startswith("SCAN r") for step in plan), (sql, plan)
        for sql in ("SELECT rowid FROM dataset_loans WHERE loan_id COLLATE NOCASE >= 'a' "
                    "AND loan_id COLLATE NOCASE < 'b'",
                    "SELECT rowid FROM dataset_loans WHERE account_id COLLATE NOCASE >= 'a' "
                    "AND account_id COLLATE NOCASE < 'b'",
                    "SELECT rowid FROM dataset_loans WHERE account_name COLLATE NOCASE >= 'a' "
                    "AND account_name COLLATE NOCASE < 'b'",
                    "SELECT account_id FROM dataset_accounts WHERE account_id COLLATE NOCASE >= 'a' "
                    "AND account_id COLLATE NOCASE < 'b'",
                    "SELECT account_id FROM dataset_account_loans WHERE loan_id COLLATE NOCASE >= 'a' "
                    "AND loan_id COLLATE NOCASE < 'b'"):
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
            assert all(step.startswith("SEARCH") for step in plan), (sql, plan)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(AnthropicLLM, "complete", Mock(side_effect=AssertionError("No provider")))
    with TestClient(create_app(tmp_path / "data")) as active:
        yield active


def wait_ready(client, expected_loans):
    for _ in range(200):
        state = client.get("/api/dataset").json()
        if state["status"] == "ready" and state["loan_count"] == expected_loans:
            return state
        time.sleep(0.05)
    pytest.fail(f"Stored dataset not ready: {state}")


def upload_set(client, count):
    sid = client.post("/api/sessions", json={}).json()["id"]
    response = client.post(f"/api/sessions/{sid}/documents", files=[
        ("files", (doc.name, workbook_bytes(doc))) for doc in synthetic_documents(count)])
    assert response.status_code == 200, response.text
    return sid


def test_uploaded_set_is_stored_and_reused_by_later_sessions(client, monkeypatch):
    assert client.get("/api/dataset").json()["status"] == "empty"
    empty = client.post("/api/sessions", json={}).json()["id"]
    assert client.post(f"/api/sessions/{empty}/stored-dataset").status_code == 409

    upload_set(client, 6)
    state = wait_ready(client, 6)
    assert sorted(item["query_role"] for item in state["files"]) == [1, 2, 4]
    assert client.get("/api/dataset/loans", params={"q": "L00003"}).json()["total"] == 1
    assert client.get("/api/dataset/accounts").json()["account_count"] == 6

    later = client.post("/api/sessions", json={}).json()["id"]
    attached = client.post(f"/api/sessions/{later}/stored-dataset")
    assert attached.status_code == 200, attached.text
    assert len(attached.json()["documents"]) == 3
    assert "What loan number" in attached.json()["messages"][-1]["content"]
    assert client.post(f"/api/sessions/{later}/stored-dataset").status_code == 409

    import backend.loan_loading as loading
    monkeypatch.setattr(loading, "extract_excel", Mock(side_effect=AssertionError("must use the index")))
    selected = client.put(f"/api/sessions/{later}/loan", json={"loan_id": "L00004"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["selected_account_id"] == "A00004"
    assert selected.json()["preflight"]["scoped_loan_id"] == "L00004"
    assert client.put(f"/api/sessions/{later}/loan", json={"loan_id": "L99999"}).status_code == 404


def test_later_upload_replaces_stored_dataset(client):
    upload_set(client, 6)
    wait_ready(client, 6)
    attached = client.post("/api/sessions", json={}).json()["id"]
    assert client.post(f"/api/sessions/{attached}/stored-dataset").status_code == 200
    upload_set(client, 3)
    wait_ready(client, 3)
    assert client.get("/api/dataset/loans", params={"q": "L00005"}).json()["total"] == 0
    # Sessions that already attached the previous files keep working from their own copies.
    selected = client.put(f"/api/sessions/{attached}/loan", json={"loan_id": "L00005"})
    assert selected.status_code == 200, selected.text
