"""Conversational loan reviews using synthetic source data and no paid calls."""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.agents import MemoryAgent
from backend.assistant_workflow import chat_loan_number
from backend.llm import AnthropicLLM, LLMError
from backend.main import create_app, process_session
from backend.tests.test_loan_loading import canonical_workbook


@pytest.mark.parametrize("text,expected", [
    ("L1", "L1"), ("loan number L1", "L1"), ("loan id L1", "L1"),
    ("loan#L1", "L1"), ("loan # L1", "L1"), ("loan: L1", "L1"),
    ("Review loan L1", "L1"), ("process loan # L2", "L2"),
    ("loan ABC", "ABC"), ("hello", None), ("loan number", None),
    ("What happened to loan L1?", None), ("Show all loan attributes", None),
    ("L1 and L2", None), ("../L1", None),
])
def test_chat_loan_number_requires_one_explicit_number(text, expected):
    assert chat_loan_number(text) == expected


def sources():
    return [
        ("MTB_Query_1.xlsx", canonical_workbook([
            ("Account", ["Id", "Type"], [["A1", "Business"], ["A2", "Business"]]),
            ("Loan", ["Id", "borrower_account_id"],
             [["L1", "A1"], ["L2", "A2"]]),
        ])),
        ("MTB_Query_2.xlsx", canonical_workbook([
            ("LCRESPONSE", ["Id", "loan_id", "sbss"], [["S1", "L1", 195], ["S2", "L2", 150]]),
        ])),
        ("MTB_Query_4.xlsx", canonical_workbook([
            ("CreditReport", ["Id", "account_id", "personal_fico"],
             [["C1", "A1", 780], ["C2", "A2", 720]]),
        ])),
    ]


@pytest.fixture
def client(tmp_path, monkeypatch):
    def complete(_system, prompt):
        context = json.loads(prompt)
        loan = context["underwriting"]["loan_results"][0]
        return json.dumps({
            "recommended_route": "MANUAL_REVIEW", "decision_recommendation": None,
            "confidence": None, "requires_human_approval": True,
            "loan_ids": [loan["loan_id"]], "positive_factors": [], "risk_factors": [],
            "triggered_rules": [], "missing_information": [], "evidence_refs": [],
            "explanation": f"Result: {loan['loan_id']} is {loan['demo_decision']['label']}. Why: Synthetic explanation.",
        })

    provider = Mock(side_effect=complete)
    monkeypatch.setattr(AnthropicLLM, "complete", provider)
    monkeypatch.setattr(MemoryAgent, "index", lambda self, session: "Synthetic memory")
    with TestClient(create_app(tmp_path / "data")) as active:
        monkeypatch.setattr(active.app.state.executor, "submit", Mock())
        monkeypatch.setattr(active.app.state.dataset_executor, "submit", Mock())
        active.provider = provider
        yield active


def store_sources(client, tmp_path):
    files = []
    for name, raw in sources():
        path = tmp_path / name
        path.write_bytes(raw)
        files.append((name, path))
    assert client.app.state.dataset.replace(files)


def new_session(client, mode="TEST"):
    response = client.post("/api/sessions", json={"mode": mode})
    assert response.status_code == 201
    return response.json()


def finish(client, sid):
    process_session(client.app.state.store, sid)
    return client.get(f"/api/sessions/{sid}").json()


def test_greeting_asks_for_number_without_a_provider_call(client):
    session = new_session(client)
    assert "Hello!" in session["messages"][0]["content"]
    assert "What loan number" in session["messages"][0]["content"]
    assert "billable" in session["messages"][0]["content"]
    response = client.post(f"/api/sessions/{session['id']}/messages", json={"content": "hello"})
    assert response.status_code == 200
    assert "What loan number" in response.json()["messages"][-1]["content"]
    client.provider.assert_not_called()
    client.app.state.executor.submit.assert_not_called()


def test_stored_data_number_processes_and_posts_result_then_accepts_another(client, tmp_path):
    store_sources(client, tmp_path)
    sid = new_session(client)["id"]
    for index, (text, loan_id, label) in enumerate([
        ("loan # L1", "L1", "Approve"), ("L2", "L2", "Decline"),
    ], start=1):
        response = client.post(f"/api/sessions/{sid}/messages", json={"content": text})
        assert response.status_code == 200, response.text
        running = response.json()
        assert running["status"] == "processing"
        assert running["selected_loan_id"] == loan_id
        assert len(running["documents"]) == 3
        assert running["messages"][-3]["content"] == text
        assert f"I'm processing loan {loan_id}" in running["messages"][-1]["content"]
        client.app.state.executor.submit.assert_called_with(process_session, client.app.state.store, sid)
        completed = finish(client, sid)
        assert completed["status"] == "completed", completed["error"]
        result = completed["messages"][-1]["content"]
        assert f"Loan {loan_id}: {label}" in result
        assert "Claude explanation:" in result and "Synthetic explanation." in result
        assert "Send another loan number" in result
        assert client.provider.call_count == index
    assert any("Loan L1: Approve" in message["content"] for message in completed["messages"])
    assert client.get(f"/api/sessions/{sid}").json()["messages"] == completed["messages"]


def test_uploaded_sources_can_be_processed_from_chat_without_stored_data(client):
    sid = new_session(client)["id"]
    uploaded = client.post(f"/api/sessions/{sid}/documents",
                           files=[("files", (name, raw)) for name, raw in sources()])
    assert uploaded.status_code == 200
    response = client.post(f"/api/sessions/{sid}/messages", json={"content": "L1"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "processing"
    assert finish(client, sid)["messages"][-1]["content"].startswith("Loan L1: Approve")


def test_real_background_worker_finishes_the_chat_review(client, tmp_path, monkeypatch):
    store_sources(client, tmp_path)
    futures = []
    executor = client.app.state.executor

    def submit(*args, **kwargs):
        future = ThreadPoolExecutor.submit(executor, *args, **kwargs)
        futures.append(future)
        return future

    monkeypatch.setattr(executor, "submit", submit)
    sid = new_session(client)["id"]
    response = client.post(f"/api/sessions/{sid}/messages", json={"content": "L1"})
    assert response.status_code == 200
    futures[0].result(timeout=10)
    completed = client.get(f"/api/sessions/{sid}").json()
    assert completed["status"] == "completed", completed["error"]
    assert completed["messages"][-1]["content"].startswith("Loan L1: Approve")
    client.provider.assert_called_once()


def test_missing_sources_and_unknown_loan_do_not_start_a_review(client, tmp_path):
    sid = new_session(client)["id"]
    missing = client.post(f"/api/sessions/{sid}/messages", json={"content": "loan # L1"})
    assert missing.status_code == 409
    assert "Upload" in missing.json()["detail"]
    store_sources(client, tmp_path)
    unknown = client.post(f"/api/sessions/{sid}/messages", json={"content": "loan # L999"})
    assert unknown.status_code == 404
    assert client.get(f"/api/sessions/{sid}").json()["selected_loan_id"] is None
    client.provider.assert_not_called()
    client.app.state.executor.submit.assert_not_called()


def test_incomplete_upload_is_not_replaced_or_processed(client, tmp_path):
    store_sources(client, tmp_path)
    sid = new_session(client)["id"]
    name, raw = sources()[0]
    client.post(f"/api/sessions/{sid}/documents", files=[("files", (name, raw))])
    response = client.post(f"/api/sessions/{sid}/messages", json={"content": "L1"})
    assert response.status_code == 409
    assert "Query 1, Query 2 and Query 4" in response.json()["detail"]
    assert len(client.get(f"/api/sessions/{sid}").json()["documents"]) == 1
    client.provider.assert_not_called()
    client.app.state.executor.submit.assert_not_called()


def test_number_during_processing_does_not_queue_duplicate(client, tmp_path):
    store_sources(client, tmp_path)
    sid = new_session(client)["id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"content": "L1"}).status_code == 200
    duplicate = client.post(f"/api/sessions/{sid}/messages", json={"content": "L2"})
    assert duplicate.status_code == 409
    assert client.app.state.executor.submit.call_count == 1
    assert client.app.state.store.get(sid).selected_loan_id == "L1"


def test_provider_failure_preserves_deterministic_chat_result(client, tmp_path):
    store_sources(client, tmp_path)
    client.provider.side_effect = LLMError("Anthropic is not configured.", 503)
    sid = new_session(client)["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"content": "L1"})
    completed = finish(client, sid)
    assert completed["status"] == "completed"
    assert "Loan L1: Approve" in completed["messages"][-1]["content"]
    assert "Claude explanation unavailable: Anthropic is not configured." in completed["messages"][-1]["content"]
    assert completed["report"]["llm_error"] == "Anthropic is not configured."


def test_processing_failure_is_reported_in_the_same_chat(client, tmp_path, monkeypatch):
    store_sources(client, tmp_path)
    sid = new_session(client)["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"content": "L1"})
    monkeypatch.setattr("backend.main.load_loan_documents", Mock(side_effect=ValueError("Source workbook unavailable.")))
    failed = finish(client, sid)
    assert failed["status"] == "failed"
    assert "The review could not finish: Source workbook unavailable." in failed["messages"][-1]["content"]
    assert failed["report"] is None
    client.provider.assert_not_called()


def test_production_chat_remains_manual_review(client, tmp_path):
    store_sources(client, tmp_path)
    client.provider.side_effect = LLMError("Synthetic provider disabled.", 503)
    sid = new_session(client, mode="PRODUCTION")["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"content": "L1"})
    completed = finish(client, sid)
    assert completed["status"] == "completed"
    assert completed["messages"][-1]["content"].startswith("Loan L1: Human review required.")
    assert completed["report"]["underwriting"]["loan_results"][0]["authorization"] == "NOT_ISSUED"
