"""Selected-loan commentary cannot change underwriting or leak raw workbooks."""

import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.llm import AnthropicLLM, LLMError
from backend.models import Report, Session
from backend.tests.test_underwriting import document, rows
from backend.underwriting_engine import evaluate_underwriting


@pytest.fixture
def setup_review(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as client:
        source = rows()
        source.extend(("Loan", {**source[1][1], "Id": f"L{i}"}) for i in range(2, 26))
        report = Report(decision="needs_review", summary="Synthetic fixture", findings=[], document_count=3,
                        underwriting=evaluate_underwriting([document(source)], mode="TEST"))
        session = Session(status="completed", report=report)
        client.app.state.store.save(session)
        yield client, session


def payload(session):
    return {"loan_id": "L25", "report_id": session.report.id,
            "expected_updated_at": session.updated_at, "confirmed": True}


def output(**changes):
    return {"recommended_route": "MANUAL_REVIEW", "decision_recommendation": None,
            "confidence": None, "requires_human_approval": True, "loan_ids": ["L25"],
            "positive_factors": ["Explicit identity supplied"], "risk_factors": ["Draft policy conflicts"],
            "missing_information": ["Authorized review"], "explanation": "Review selected evidence; no lending decision issued.",
            "triggered_rules": [], "evidence_refs": [], **changes}


def reconciliation(context):
    return [{"rule_id": rule["rule_id"], "evidence_state": "POLICY_UNRESOLVED",
             "rationale": "Supplied diagnostic remains unresolved; no clearance inferred.",
             "next_step": "Obtain the stated missing evidence or policy clarification.",
             "evidence_refs": rule["evidence_refs"][:1]}
            for rule in context["checks_to_reconcile"]]


def test_selected_loan_is_scoped_before_context_limits_and_report_is_unchanged(setup_review, monkeypatch):
    client, session = setup_review
    prompts = []
    def complete(self, system, prompt):
        assert "Result:, Why:, What is unresolved:, Next steps:" in system
        assert "do not assert Quick App eligibility" in system
        prompts.append(json.loads(prompt))
        refs = prompts[-1]["underwriting"]["evidence_refs"]
        return json.dumps(output(evidence_refs=refs[:1], check_reconciliation=reconciliation(prompts[-1])))
    monkeypatch.setattr(AnthropicLLM, "complete", complete)
    before = session.report.model_dump(mode="json")
    response = client.post(f"/api/sessions/{session.id}/assistant/loan-review", json=payload(session))
    assert response.status_code == 200
    data = response.json()
    assert data["report"] == before
    assert data["messages"] == []
    review = data["loan_ai_reviews"][0]
    assert review["loan_id"] == "L25" and review["report_id"] == before["id"]
    trace = prompts[0]["underwriting"]
    assert len(trace["loan_results"]) == 1
    assert trace["loan_results"][0]["loan_id"] == "L25"
    assert len(trace["loan_results"][0]["rule_results"]) <= 40
    assert len(trace["evidence_refs"]) <= 40
    assert "raw" not in trace and "documents" not in prompts[0]
    assert "source_evidence" not in trace["loan_results"][0]
    assert "normalized_amounts_usd" in trace["loan_results"][0]
    assert len(review["analysis"]["check_reconciliation"]) == len(prompts[0]["checks_to_reconcile"])
    assert client.get(f"/api/sessions/{session.id}").json()["loan_ai_reviews"] == data["loan_ai_reviews"]


@pytest.mark.parametrize("changes", [
    {"recommended_route": "APPROVE"}, {"decision_recommendation": "DENY"},
    {"confidence": 0.99}, {"requires_human_approval": False},
    {"loan_ids": ["L1"]}, {"evidence_refs": ["made-up"]},
    {"triggered_rules": ["NONEXISTENT"]}, {"policy_config": {}},
])
def test_invalid_model_output_discarded(setup_review, monkeypatch, changes):
    client, session = setup_review
    monkeypatch.setattr(AnthropicLLM, "complete", Mock(return_value=json.dumps(output(**changes))))
    response = client.post(f"/api/sessions/{session.id}/assistant/loan-review", json=payload(session))
    assert response.status_code == 502
    saved = client.get(f"/api/sessions/{session.id}").json()
    assert saved["loan_ai_reviews"] == []
    assert saved["report"] == session.report.model_dump(mode="json")


def test_explicit_confirmation_revision_and_loan_guards(setup_review, monkeypatch):
    client, session = setup_review
    call = Mock(side_effect=AssertionError("Must not call provider"))
    monkeypatch.setattr(AnthropicLLM, "complete", call)
    endpoint = f"/api/sessions/{session.id}/assistant/loan-review"
    for changes, status in [({"confirmed": False}, 422), ({"expected_updated_at": "stale"}, 409),
                            ({"report_id": "old"}, 409), ({"loan_id": "other-session-loan"}, 404),
                            ({"policy_config": {}}, 422)]:
        assert client.post(endpoint, json={**payload(session), **changes}).status_code == status
    call.assert_not_called()


def test_provider_failure_preserves_report(setup_review, monkeypatch):
    client, session = setup_review
    monkeypatch.setattr(AnthropicLLM, "complete", Mock(side_effect=LLMError("Provider unavailable", 503)))
    assert client.post(f"/api/sessions/{session.id}/assistant/loan-review", json=payload(session)).status_code == 503
    assert client.app.state.store.get(session.id).report == session.report


def test_concurrent_revision_rejects_response(setup_review, monkeypatch):
    client, session = setup_review
    def complete(self, system, prompt):
        latest = client.app.state.store.get(session.id)
        latest.title = "Changed during request"
        client.app.state.store.save(latest)
        return json.dumps(output(check_reconciliation=reconciliation(json.loads(prompt))))
    monkeypatch.setattr(AnthropicLLM, "complete", complete)
    assert client.post(f"/api/sessions/{session.id}/assistant/loan-review", json=payload(session)).status_code == 409
    assert client.app.state.store.get(session.id).loan_ai_reviews == []


@pytest.mark.parametrize("defect", ["omitted", "duplicate", "foreign_citation", "mapped_without_evidence"])
def test_per_check_reconciliation_must_be_complete_and_grounded(setup_review, monkeypatch, defect):
    client, session = setup_review
    def complete(self, system, prompt):
        checks = reconciliation(json.loads(prompt))
        assert checks
        if defect == "omitted":
            checks.pop()
        elif defect == "duplicate":
            checks.append(checks[0])
        elif defect == "foreign_citation":
            checks[0]["evidence_refs"] = ["not-this-rules-evidence"]
        else:
            checks[0]["evidence_state"] = "MAPPED_EVIDENCE_BLOCKED"
            checks[0]["evidence_refs"] = []
        return json.dumps(output(check_reconciliation=checks))
    monkeypatch.setattr(AnthropicLLM, "complete", complete)
    response = client.post(f"/api/sessions/{session.id}/assistant/loan-review", json=payload(session))
    assert response.status_code == 502
    assert client.app.state.store.get(session.id).loan_ai_reviews == []
    assert client.app.state.store.get(session.id).report == session.report