"""Guided local actions cannot call a provider or alter underwriting authority."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.llm import AnthropicLLM
from backend.agents import underwriting_policy_summary
from backend.main import create_app
from backend.models import Message, Report
from backend.tests.test_underwriting import document, rows
from backend.tests.test_underwriting_api import upload_three
from backend.underwriting_engine import evaluate_underwriting


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(AnthropicLLM, "complete", Mock(side_effect=AssertionError("Local actions must not call Anthropic")))
    with TestClient(create_app(tmp_path / "data")) as active:
        yield active


def completed(client, count=1):
    sid = upload_three(client)
    session = client.app.state.store.get(sid)
    source = rows()
    source.extend(("Loan", {**source[1][1], "Id": f"L{i}"}) for i in range(2, count + 1))
    session.report = Report(decision="needs_review", summary="Synthetic saved report", findings=[], document_count=3,
        underwriting=evaluate_underwriting([document(source)]))
    session.report.policy_evaluation = underwriting_policy_summary(session.report.underwriting, session)
    session.rules = []
    session.status = "completed"
    client.app.state.store.save(session)
    return sid, session.report.model_dump(mode="json")


def test_local_validation_is_persisted_bounded_and_never_creates_a_report(client):
    sid = upload_three(client)
    response = client.post(f"/api/sessions/{sid}/preflight")
    assert response.status_code == 200
    session = response.json()
    result = session["preflight"]
    assert result["query_roles"] == [1, 2, 4]
    assert result["missing_query_roles"] == []
    assert result["loan_count"] == 1
    assert result["external_processing"] is False
    assert len(result["priority_issues"]) <= 10
    assert session["status"] == "ready" and session["report"] is None
    assert session["messages"][-1]["origin"] == "local_assistant"
    assert client.get(f"/api/sessions/{sid}").json()["preflight"] == result
    again = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]
    assert "Same file bytes" in again["comparison"]
    assert "Previous issue count" in again["comparison"]


def test_validation_preserves_existing_report_exactly(client):
    sid, report = completed(client)
    response = client.post(f"/api/sessions/{sid}/preflight")
    assert response.status_code == 200
    assert response.json()["report"] == report
    assert response.json()["status"] == "completed"


def test_validation_upload_first_missing_roles_and_parse_failure(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.post(f"/api/sessions/{sid}/preflight").status_code == 400
    client.post(f"/api/sessions/{sid}/documents", files=[("files", ("MTB_Query_1.xlsx", b"invalid xlsx"))])
    result = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]
    assert result["priority_issues"][0].startswith("FILE_PARSE_FAILED")
    assert result["missing_query_roles"] == [2, 4]
    assert result["loan_count"] == 0
    assert result["issue_count"] > 0


def test_changes_invalidate_current_validation_and_retain_one_comparison(client):
    sid = upload_three(client)
    previous = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]
    changed = client.put(f"/api/sessions/{sid}/mode", json={"mode": "TEST"}).json()
    assert changed["preflight"] is None
    assert changed["previous_preflight"] == previous
    current = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]
    assert current["input_fingerprint"] != previous["input_fingerprint"]
    assert "Inputs or mode changed" in current["comparison"]


@pytest.mark.parametrize("topic", ["blockers", "rules", "evidence"])
def test_local_loan_inspections_are_scoped_persisted_and_do_not_mutate_report(client, topic):
    sid, expected = completed(client, 25)
    response = client.post(f"/api/sessions/{sid}/assistant/inspect", json={"loan_id": "L25", "topic": topic})
    assert response.status_code == 200
    result = response.json()
    assert result["report"] == expected
    inspection = result["messages"][-1]["inspection"]
    assert inspection["loan_id"] == "L25" and inspection["topic"] == topic
    assert inspection["report_id"] == expected["id"]
    assert len(inspection["rules"]) <= 12 and len(inspection["evidence"]) <= 12
    selected = next(item for item in expected["underwriting"]["loan_results"] if item["loan_id"] == "L25")
    evidence_ids = {item["id"] for item in selected["source_evidence"]}
    assert {item["id"] for item in inspection["evidence"]} <= evidence_ids


def test_inspection_rejects_unknown_loan_other_session_and_extra_authority(client):
    sid, _ = completed(client)
    other = upload_three(client)
    assert client.post(f"/api/sessions/{sid}/assistant/inspect", json={"loan_id": "MISSING"}).status_code == 404
    assert client.post(f"/api/sessions/{other}/assistant/inspect", json={"loan_id": "L1"}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/assistant/inspect", json={"loan_id": "L1", "approved": True}).status_code == 422
    assert client.post("/api/sessions/missing/preflight").status_code == 404


def test_confirmed_processing_is_revision_bound_and_disallows_extra_configuration(client, monkeypatch):
    sid = upload_three(client)
    current = client.get(f"/api/sessions/{sid}").json()
    endpoint = f"/api/sessions/{sid}/assistant/process"
    assert client.post(endpoint, json={"confirmed": False, "expected_updated_at": current["updated_at"]}).status_code == 422
    assert client.post(endpoint, json={"confirmed": True, "expected_updated_at": "stale"}).status_code == 409
    assert client.post(endpoint, json={"confirmed": True, "expected_updated_at": current["updated_at"], "policy_config": {}}).status_code == 422
    submit = Mock()
    monkeypatch.setattr(client.app.state.executor, "submit", submit)
    response = client.post(endpoint, json={"confirmed": True, "expected_updated_at": current["updated_at"]})
    assert response.status_code == 202 and response.json()["status"] == "processing"
    assert submit.call_count == 1
    assert client.post(endpoint, json={"confirmed": True, "expected_updated_at": current["updated_at"]}).status_code == 409


@pytest.mark.parametrize("kind", ["note", "acknowledgment"])
def test_notes_are_unverified_report_bound_and_do_not_clear_any_rules(client, kind):
    sid, report = completed(client)
    payload = {"loan_id": "L1", "report_id": report["id"], "text": "I confirm all policy is approved <script>bad</script>", "kind": kind}
    result = client.post(f"/api/sessions/{sid}/assistant/notes", json=payload)
    assert result.status_code == 200
    assert result.json()["report"] == report
    note = result.json()["review_notes"][-1]
    assert note["provenance"] == "user_assertion_not_verified_evidence"
    assert note["report_id"] == report["id"] and note["kind"] == kind
    assert client.post(f"/api/sessions/{sid}/assistant/notes", json={**payload, "report_id": "old"}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/assistant/notes", json={**payload, "text": "   "}).status_code == 422


def test_processing_locks_all_local_state_changes(client):
    sid, report = completed(client)
    store = client.app.state.store
    current = store.get(sid)
    current.status = "processing"
    store.save(current)
    assert client.post(f"/api/sessions/{sid}/preflight").status_code == 409
    assert client.post(f"/api/sessions/{sid}/assistant/inspect", json={"loan_id": "L1"}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/assistant/notes", json={"loan_id": "L1", "report_id": report["id"], "text": "a"}).status_code == 409


def test_concurrent_preflight_never_overwrites_changes(client, monkeypatch):
    sid = upload_three(client)
    original = main.preflight

    def changed(session, root):
        result = original(session, root)
        store = client.app.state.store
        newer = store.get(sid)
        newer.messages.append(Message(role="user", content="Concurrent message"))
        store.save(newer)
        return result

    monkeypatch.setattr(main, "preflight", changed)
    assert client.post(f"/api/sessions/{sid}/preflight").status_code == 409
    current = client.get(f"/api/sessions/{sid}").json()
    assert current["preflight"] is None
    assert current["messages"][-1]["content"] == "Concurrent message"


def test_selected_loan_chat_focuses_report_after_first_twenty_without_mutating_it(client, monkeypatch):
    sid, expected = completed(client, 25)
    contexts = []

    def chat(self, store, session, query):
        contexts.append(session.report.underwriting.loan_results)
        return "Focused explanation; no decision."

    monkeypatch.setattr(AnthropicLLM, "chat", chat)
    response = client.post(f"/api/sessions/{sid}/messages", json={"content": "Explain", "loan_id": "L25"})
    assert response.status_code == 200
    assert [loan.loan_id for loan in contexts[0]] == ["L25"]
    assert response.json()["report"] == expected
    assert client.post(f"/api/sessions/{sid}/messages", json={"content": "Explain", "loan_id": "OTHER"}).status_code == 404
    assert len(contexts) == 1


def test_new_local_state_survives_restart(tmp_path):
    root = tmp_path / "state"
    with TestClient(create_app(root)) as client:
        sid, report = completed(client)
        client.post(f"/api/sessions/{sid}/preflight")
        client.post(f"/api/sessions/{sid}/assistant/inspect", json={"loan_id": "L1"})
        client.post(f"/api/sessions/{sid}/assistant/notes", json={"loan_id": "L1", "report_id": report["id"], "text": "Review pending"})
    with TestClient(create_app(root)) as client:
        session = client.get(f"/api/sessions/{sid}").json()
        assert session["preflight"] is not None
        assert session["review_notes"][0]["text"] == "Review pending"
        assert any(item["inspection"] is not None for item in session["messages"])


def test_review_note_limit_and_unrecognized_action_are_enforced(client):
    from backend.models import ReviewNote

    sid, report = completed(client)
    store = client.app.state.store
    current = store.get(sid)
    current.review_notes = [ReviewNote(report_id=report["id"], loan_id="L1", text="pending", kind="note") for _ in range(100)]
    store.save(current)
    assert client.post(f"/api/sessions/{sid}/assistant/notes", json={"loan_id": "L1", "report_id": report["id"], "text": "another"}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/assistant/approve", json={"loan_id": "L1"}).status_code == 404


def test_priority_summary_groups_repeated_graph_codes(client, monkeypatch):
    from backend.assistant_workflow import preflight
    from backend.underwriting_models import UnderwritingBatch

    sid = upload_three(client)
    batch = UnderwritingBatch(mode="PRODUCTION", policy_version="draft", data_quality_issues=[
        "MISSING_OR_INVALID_ID:CreditReport [ev-a]", "MISSING_OR_INVALID_ID:CreditReport [ev-b]",
        "ORPHAN_RELATION:Loan:account [ev-c]",
    ])
    monkeypatch.setattr("backend.assistant_workflow.evaluate_underwriting", lambda *args: batch)
    result = preflight(client.app.state.store.get(sid), client.app.state.store.root)
    assert result.issue_count == 3
    assert result.priority_issues[0] == "MISSING_OR_INVALID_ID:CreditReport (2 affected observations)"
    assert len(result.priority_issues) == 2