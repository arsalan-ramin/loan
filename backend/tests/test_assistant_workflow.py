"""Guided local actions cannot call a provider or alter underwriting authority."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.llm import AnthropicLLM, LLMError
from backend.agents import underwriting_policy_summary
from backend.main import create_app
from backend.models import Message, Report
from backend.tests.test_underwriting import document, rows
from backend.tests.test_underwriting_api import upload_three, workbook
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
    session.report = Report(decision="needs_review", summary="Synthetic saved report", findings=[], document_count=1,
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
    assert result["document_count"] == 3
    assert result["query_file_set_valid"] is True
    assert result["attribute_count"] >= 1
    assert "Name" in result["attributes"]
    assert result["loan_count"] == 1
    assert result["account_count"] == 1
    assert result["external_processing"] is False
    assert len(result["priority_issues"]) <= 10
    assert session["status"] == "ready" and session["report"] is None
    assert session["messages"][-1]["origin"] == "local_assistant"
    assert client.get(f"/api/sessions/{sid}").json()["preflight"] == result
    again = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]
    assert "Same file bytes" in again["comparison"]
    assert "Previous issue count" in again["comparison"]


def test_preflight_discovers_accounts_from_namespaced_soql_query1_relationships(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    prefix = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r."
    query1 = workbook([("MTB Query 1", {
        "Id": "ANALYSIS-ROW-1",
        prefix + "LLC_BI__Loan__c": "LOAN-1",
        prefix + "LLC_BI__Account__c": "ACCOUNT-1",
        prefix + "LLC_BI__Borrower_Type__c": "borrower",
    })])
    query2 = workbook([("MTB Query 2", {"Id": "RESPONSE-ROW-1"})])
    query4 = workbook([("MTB Query 4", {"Id": "REPORT-ROW-1"})])
    upload = client.post(f"/api/sessions/{sid}/documents", files=[
        ("files", ("MTB_Query_1_SOQL_Only.xlsx", query1)),
        ("files", ("MTB_Query_2_SOQL_Only.xlsx", query2)),
        ("files", ("MTB_Query_4_SOQL_Only.xlsx", query4)),
    ])
    assert upload.status_code == 200

    validation = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]

    assert validation["query_file_set_valid"] is True
    assert validation["loan_count"] == 1
    assert validation["account_count"] == 1
    account = client.get(f"/api/sessions/{sid}/accounts").json()["results"][0]
    assert account["account_id"] == "ACCOUNT-1"
    assert account["loan_ids"] == ["LOAN-1"]


@pytest.mark.parametrize("renamed", [False, True])
def test_preflight_separates_unmapped_soql_headers_from_unavailable_inputs(client, renamed):
    sid = client.post("/api/sessions", json={}).json()["id"]
    junction = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r."
    q1 = workbook([("Sheet1" if renamed else "MTB Query 1", {
        "Id": "C1", junction + "LLC_BI__Loan__c": "L1",
        junction + "LLC_BI__Account__c": "A1",
        junction + "LLC_BI__Borrower_Type__c": "Borrower",
        junction + "LLC_BI__Account__r.Birth_Date__c": "1980-01-01",
        "SCOC__CharacteristicKey__c": "numCollection",
        "SCOC__CharacteristicValue__c": "0",
        "Unsupported_Example_Field__c": "preserved",
    })])
    q2 = workbook([("Sheet1" if renamed else "MTB Query 2", {
        "Id": "I1", junction + "LLC_BI__Loan__c": "L1",
        junction + "LLC_BI__Account__c": "A1",
        junction + "LLC_BI__Borrower_Type__c": "Borrower",
        "SCOC__IndicatorKey__c": "thinFileIndicator",
        "SCOC__IndicatorValue__c": "N",
    })])
    q4 = workbook([("Sheet1" if renamed else "MTB Query 4", {
        "Id": "R1", "nFUSE__Account__c": "A1",
        "nFUSE__Credit_Score__c": "740", "nFUSE__Status__c": "Completed",
    })])
    client.post(f"/api/sessions/{sid}/documents", files=[
        ("files", ("financials.xlsx" if renamed else "MTB_Query_1_SOQL_Only.xlsx", q1)),
        ("files", ("scores.xlsx" if renamed else "MTB_Query_2_SOQL_Only.xlsx", q2)),
        ("files", ("bureau.xlsx" if renamed else "MTB_Query_4_SOQL_Only.xlsx", q4)),
    ])

    result = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]

    assert result["query_roles"] == [1, 2, 4]
    assert result["query_file_set_valid"] is True
    assert result["account_count"] == 1
    assert result["loan_count"] == 1
    assert "Unsupported_Example_Field__c" in result["unmapped_attributes"]
    assert "Loan as-of date" in result["unavailable_inputs"]
    assert "Explicit EBITDA (EBIT is not interchangeable)" in result["unavailable_inputs"]
    assert "Credit report date" in result["unavailable_inputs"]
    assert "UNMAPPED_RELATION:Indicator:analysis_id" in result["mapping_warnings"]


@pytest.mark.parametrize("missing_preflight", [False, True])
def test_validation_preserves_existing_report_exactly(client, missing_preflight):
    sid, report = completed(client)
    if missing_preflight:
        store = client.app.state.store
        current = store.get(sid)
        current.previous_preflight = current.preflight
        current.preflight = None
        store.save(current)
        assert client.get(f"/api/sessions/{sid}/accounts").status_code == 409
    response = client.post(f"/api/sessions/{sid}/preflight")
    assert response.status_code == 200
    assert response.json()["report"] == report
    assert response.json()["status"] == "completed"
    assert response.json()["preflight"] is not None
    assert client.get(f"/api/sessions/{sid}/accounts").json()["total"] == 1


def test_completed_review_can_switch_accounts_without_reuploading(client, monkeypatch):
    sid = client.post("/api/sessions", json={}).json()["id"]
    source = rows()
    groups = [
        (1, source[:2]),
        (2, [("Account", {**source[0][1], "Id": "A2"}),
             ("Loan", {**source[1][1], "Id": "L2", "borrower_account_id": "A2"}),
             source[3]]),
        (4, source[2:3]),
    ]
    uploaded = client.post(f"/api/sessions/{sid}/documents", files=[
        ("files", (f"MTB_Query_{role}.xlsx", workbook(records))) for role, records in groups
    ])
    assert uploaded.status_code == 200
    documents = uploaded.json()["documents"]
    store = client.app.state.store
    paths = [store.root / "uploads" / sid / doc["id"] for doc in documents]
    original_bytes = [path.read_bytes() for path in paths]
    validation = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]
    assert validation["account_count"] == 2
    submit = Mock()
    monkeypatch.setattr(client.app.state.executor, "submit", submit)
    explain = Mock(side_effect=LLMError("Mock provider unavailable"))
    monkeypatch.setattr(AnthropicLLM, "explain", explain)
    monkeypatch.setattr(main.MemoryAgent, "index", Mock(return_value="Mock memory index"))
    monkeypatch.setattr(store, "remember", Mock())

    for account_id, loan_id in (("A1", "L1"), ("A2", "L2")):
        candidates = client.get(f"/api/sessions/{sid}/accounts")
        assert candidates.status_code == 200
        assert {item["account_id"] for item in candidates.json()["results"]} == {"A1", "A2"}
        selected = client.put(f"/api/sessions/{sid}/account", json={"account_id": account_id})
        assert selected.status_code == 200
        current = selected.json()
        assert current["report"] is None and current["status"] == "ready"
        assert all(step["status"] == "pending" for step in current["steps"])
        assert current["preflight"] == validation
        assert submit.call_count == explain.call_count
        started = client.post(f"/api/sessions/{sid}/process")
        assert started.status_code == 202
        assert started.json()["preflight"] == validation
        main.process_session(store, sid)
        result = client.get(f"/api/sessions/{sid}").json()
        assert result["status"] == "completed", result["error"]
        assert result["preflight"] == validation
        loans = result["report"]["underwriting"]["loan_results"]
        assert [(loan["borrower_account_id"], loan["loan_id"]) for loan in loans] == [(account_id, loan_id)]
        assert [doc["id"] for doc in result["documents"]] == [doc["id"] for doc in documents]
        assert [path.read_bytes() for path in paths] == original_bytes
        assert {path.name for path in paths[0].parent.iterdir()} == {doc["id"] for doc in documents}
    assert submit.call_count == explain.call_count == 2


@pytest.mark.parametrize("mode", ["TEST", "PRODUCTION"])
def test_unmapped_workbooks_continue_to_diagnostic_report(client, monkeypatch, mode):
    sid = client.post("/api/sessions", json={"mode": mode}).json()["id"]
    payload = workbook([("Data", {"Unsupported_Field__c": "unmapped source evidence"})])
    uploaded = client.post(f"/api/sessions/{sid}/documents", files=[
        ("files", (f"MTB_Query_{role}.xlsx", payload)) for role in (1, 2, 4)
    ])
    assert uploaded.status_code == 200
    validation = client.post(f"/api/sessions/{sid}/preflight").json()
    assert validation["preflight"]["query_file_set_valid"] is True
    assert validation["preflight"]["account_count"] == 0
    assert "diagnostic-only" in validation["preflight"]["suggested_next_step"]
    assert validation["selected_account_id"] is None
    monkeypatch.setattr(client.app.state.executor, "submit", Mock())
    # Processing is explicitly confirmed (unlike local preflight). Simulate
    # unavailable provider commentary; deterministic diagnostics must survive.
    monkeypatch.setattr(AnthropicLLM, "explain", Mock(side_effect=LLMError("Provider unavailable in test")))
    started = client.post(f"/api/sessions/{sid}/assistant/process", json={
        "confirmed": True, "expected_updated_at": validation["updated_at"],
    })
    assert started.status_code == 202
    main.process_session(client.app.state.store, sid)
    result = client.get(f"/api/sessions/{sid}").json()
    assert result["status"] == "completed"
    assert result["report"]["decision"] == "needs_review"
    batch = result["report"]["underwriting"]
    assert batch["loan_results"] == []
    assert "NO_EXPLICIT_LOAN_RECORDS" in batch["data_quality_issues"]
    assert "Unsupported_Field__c" in batch["unmapped_source_fields"]


def test_validation_upload_first_missing_roles_and_parse_failure(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.post(f"/api/sessions/{sid}/preflight").status_code == 400
    client.post(f"/api/sessions/{sid}/documents", files=[("files", ("MTB_Query_1.xlsx", b"invalid xlsx"))])
    result = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]
    assert result["priority_issues"][0].startswith("FILE_PARSE_FAILED")
    assert "INVALID_XLSX" in result["priority_issues"][0]
    assert "parsing failure, not a missing field mapping" in result["suggested_next_step"]
    assert result["missing_query_roles"] == []
    assert result["loan_count"] == 0
    assert result["issue_count"] > 0


def test_upload_accepts_query_files_in_batches_but_process_requires_all_roles(client, monkeypatch):
    sid = client.post("/api/sessions", json={}).json()["id"]
    one = workbook([("Account", {"Id": "A1"}),
                    ("Loan", {"Id": "L1", "borrower_account_id": "A1"})])
    two = workbook([("Account", {"Id": "A2"}),
                    ("Loan", {"Id": "L2", "borrower_account_id": "A2"})])
    response = client.post(f"/api/sessions/{sid}/documents", files=[
        ("files", ("MTB_Query_1.xlsx", one)),
        ("files", ("MTB_Query_2.xlsx", two)),
    ])
    assert response.status_code == 200
    validation = client.post(f"/api/sessions/{sid}/preflight").json()["preflight"]
    assert validation["missing_query_roles"] == [4]
    assert not validation["query_file_set_valid"]
    submit = Mock()
    monkeypatch.setattr(client.app.state.executor, "submit", submit)
    assert client.post(f"/api/sessions/{sid}/process").status_code == 409
    assert submit.call_count == 0


def test_changes_invalidate_current_validation_and_retain_one_comparison(client):
    sid = upload_three(client, mode="PRODUCTION")
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


def test_processing_requires_single_workbook_then_selects_detected_account(client, monkeypatch):
    sid = upload_three(client)
    session = client.app.state.store.get(sid)
    session.selected_account_id = None
    client.app.state.store.save(session)
    submit = Mock()
    monkeypatch.setattr(client.app.state.executor, "submit", submit)

    blocked = client.post(f"/api/sessions/{sid}/process")
    assert blocked.status_code == 409
    assert "Select a detected borrower account" in blocked.json()["detail"]
    assert submit.call_count == 0

    page = client.get(f"/api/sessions/{sid}/accounts").json()
    selected = client.put(f"/api/sessions/{sid}/account", json={"account_id": page["results"][0]["account_id"]})
    assert selected.status_code == 200
    assert selected.json()["selected_account_id"] == page["results"][0]["account_id"]


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
    initial = client.app.state.store.get(sid)
    initial.preflight = None
    client.app.state.store.save(initial)
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


def test_account_directory_search_is_paginated_and_session_scoped(client):
    from backend.models import AccountCandidate
    sid = upload_three(client)
    # This search-directory contract uses the complete 1/2/4 upload helper.
    client.post(f"/api/sessions/{sid}/preflight")
    client.app.state.store.replace_account_candidates(sid, [
        AccountCandidate(account_id=f"A{i:03}", name=f"Client {i:03}", loan_count=i,
                 loan_ids=[f"L{i:03}"]) for i in range(1, 6)
    ])
    first = client.get(f"/api/sessions/{sid}/accounts?limit=2").json()
    assert first["total"] == 5 and len(first["results"]) == 2
    second = client.get(f"/api/sessions/{sid}/accounts?limit=2&offset=2&q=Client%2000").json()
    assert [item["account_id"] for item in second["results"]] == ["A003", "A004"]
    assert client.get("/api/sessions/missing/accounts").status_code == 404


def test_account_search_refreshes_legacy_preflight_index(client):
    sid = upload_three(client)
    store = client.app.state.store
    session = store.get(sid)
    session.preflight.account_index_version = 0
    session.preflight.account_count = 0
    store.forget_account_candidates(sid)
    store.save(session)

    response = client.get(f"/api/sessions/{sid}/accounts?q=L1")

    assert response.status_code == 200
    result = response.json()
    assert result["account_count"] == 1
    assert result["refreshed_preflight"]["account_index_version"] == 1
    assert result["results"][0]["account_id"] == "A1"
    assert result["results"][0]["loan_ids"] == ["L1"]