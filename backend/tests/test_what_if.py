"""Isolated aggregate-FICO scenarios and aligned chat context, without paid calls."""

import copy
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.commentary import chat
from backend.llm import AnthropicLLM
from backend.llm_context import chat_context
from backend.main import create_app
from backend.models import Message, Report, Session
from backend.prompts import TEST_CHAT_INSTRUCTIONS
from backend.tests.test_underwriting import document, rows
from backend.underwriting_engine import evaluate_underwriting
from backend.what_if import fico_what_if, is_fico_what_if


def saved_session(fico="620", sbss="155", ownership="2", amount="100000", vehicle=False):
    source = rows()
    source[0][1]["ownership_years"] = ownership
    source[1][1]["requested_amount"] = amount
    source[1][1]["vehicle_only"] = vehicle
    source[2][1]["personal_fico"] = fico
    source[3][1]["sbss"] = sbss
    batch = evaluate_underwriting([document(source)], "TEST")
    return Session(status="completed", mode="TEST", selected_loan_id="L1", report=Report(
        decision="needs_review", summary="Synthetic TEST baseline", findings=[],
        document_count=1, underwriting=batch))


@pytest.mark.parametrize("query", [
    "what if the credit score/fico was 820?", "What if FICO was 820?",
    "what-if the average FICO score were 820?", "Suppose the credit score is 820.",
    "assuming aggregate FICO = 820", "what if fico: 820", "what if fico 820",
])
def test_recognizes_supported_fico_commands(query):
    assert is_fico_what_if(query)
    answer = fico_what_if(saved_session(), query)
    assert "620 -> 820" in answer and "**Decline**" in answer
    assert "SBSS remains **155**" in answer
    assert "**Why:** SBSS 155 is below 160." in answer
    assert "no report was selected" not in answer
    assert "0 provider tokens" in answer


@pytest.mark.parametrize("query", [
    "What was the FICO?", "Review loan L1", "What if SBSS was 220?",
    "FICO is 820", "What is my credit score?",
])
def test_normal_questions_are_not_scenarios(query):
    assert not is_fico_what_if(query)


@pytest.mark.parametrize("query,expected", [
    ("what if fico was 299?", "between 300 and 900"),
    ("what if fico was 901?", "between 300 and 900"),
    ("what if fico was -820?", "between 300 and 900"),
    ("what if fico was abc?", "send one aggregate FICO value"),
    ("what if fico was 820 and sbss was 220?", "multi-field"),
    ("what if account A1 FICO was 820?", "Account-specific"),
    ("what if fico was 8.2e2?", "send one aggregate FICO value"),
    ("what if fico was 820 for loan L2?", "send one aggregate FICO value"),
])
def test_invalid_or_ambiguous_request_is_explicit_not_a_partial_override(query, expected):
    assert is_fico_what_if(query)
    answer = fico_what_if(saved_session(), query)
    assert expected in answer and "No provider request was made" in answer


@pytest.mark.parametrize("score,label,branch", [
    ("300", "Decline", "KNOWN_KNOCKOUT"), ("649.99", "Decline", "KNOWN_KNOCKOUT"),
    ("650", "Approve", "SBSS_160_189_AMOUNT_LE_100K"),
    ("759.99", "Approve", "SBSS_160_189_AMOUNT_LE_100K"),
    ("760", "Approve", "SBSS_160_189_OWNERSHIP_LT_2_FICO_GE_760"),
    ("820", "Approve", "SBSS_160_189_OWNERSHIP_LT_2_FICO_GE_760"),
    ("900", "Approve", "SBSS_160_189_OWNERSHIP_LT_2_FICO_GE_760"),
])
def test_exact_thresholds_and_route_use_shared_tree(score, label, branch):
    session = saved_session(sbss="177", ownership="0")
    before = session.model_dump_json()
    answer = fico_what_if(session, f"What if FICO was {score}?")
    assert f"Hypothetical result: **{label}**" in answer
    assert f"**Branch:** {branch}." in answer
    assert session.model_dump_json() == before


def test_branch_conflict_is_recomputed_not_carried_forward():
    session = saved_session(fico="700", sbss="177", ownership="0", amount="100001")
    demo = session.report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert demo["label"] == "Refer" and demo["conflicts"] == ["REQUEST_THRESHOLD_100M_VS_100K"]
    answer = fico_what_if(session, "What if FICO was 820?")
    assert "Hypothetical result: **Approve**" in answer
    assert "REQUEST_THRESHOLD" not in answer
    assert demo["conflicts"] == ["REQUEST_THRESHOLD_100M_VS_100K"]


@pytest.mark.parametrize("sbss,ownership,amount,vehicle", [
    ("155", "2", "100000", False), ("160", "0", "100001", False),
    ("177", "0", "100000001", False), ("189", "2", "100000", True),
    ("190", "0", "100000", False), ("199", "0", "100000", True),
    ("200", "0", "100000", False), ("200", "2", "100000", False),
])
@pytest.mark.parametrize("score", ["640", "700", "820"])
def test_scenario_matches_real_tree_with_only_the_fico_input_changed(sbss, ownership, amount, vehicle, score):
    baseline = saved_session(sbss=sbss, ownership=ownership, amount=amount, vehicle=vehicle)
    original = baseline.model_dump_json()
    expected = saved_session(fico=score, sbss=sbss, ownership=ownership, amount=amount, vehicle=vehicle)
    decision = expected.report.underwriting.loan_results[0].normalized_data["demo_decision"]
    answer = fico_what_if(baseline, f"what if fico was {score}?")
    assert f"Hypothetical result: **{decision['label']}**" in answer
    assert f"**Branch:** {decision['branch']}." in answer
    assert decision["rationale"] in answer
    assert baseline.model_dump_json() == original


def test_unrelated_unknown_knockout_still_blocks_approval():
    session = saved_session(sbss="195")
    demo = session.report.underwriting.loan_results[0].normalized_data["demo_decision"]
    demo["normalized_facts"]["bankruptcy_clear"] = None
    next(check for check in demo["criteria"] if check["criterion"] == "bankruptcy_clear")["status"] = "UNKNOWN"
    answer = fico_what_if(session, "What if FICO was 820?")
    assert "Hypothetical result: **Refer**" in answer
    assert "**Unresolved decision-flow inputs:** bankruptcy_clear." in answer


def test_identity_guard_and_vehicle_override_are_preserved():
    session = saved_session(sbss="177", ownership="0", vehicle=True)
    assert "_VEHICLE_ONLY_OVERRIDE" in fico_what_if(session, "What if FICO was 820?")
    demo = session.report.underwriting.loan_results[0].normalized_data["demo_decision"]
    demo["missing_inputs"].append("explicit_unique_borrower_account_relationship")
    answer = fico_what_if(session, "What if FICO was 820?")
    assert "Hypothetical result: **Refer**" in answer
    assert "explicit_unique_borrower_account_relationship" in answer


def test_scenarios_do_not_chain_and_missing_baseline_fico_can_be_overridden():
    session = saved_session(sbss="195")
    demo = session.report.underwriting.loan_results[0].normalized_data["demo_decision"]
    demo["normalized_facts"]["fico"] = None
    next(check for check in demo["criteria"] if check["criterion"] == "fico")["status"] = "UNKNOWN"
    first = fico_what_if(session, "what if fico was 820")
    second = fico_what_if(session, "what if fico was 600")
    assert "**Unknown -> 820**" in first and "Hypothetical result: **Approve**" in first
    assert "**Unknown -> 600**" in second and "Hypothetical result: **Decline**" in second
    assert demo["normalized_facts"]["fico"] is None


def test_missing_or_unsupported_snapshot_and_mode_are_explicit():
    session = Session()
    assert "completed TEST loan review" in fico_what_if(session, "what if fico was 820")
    session = saved_session()
    session.mode = "PRODUCTION"
    assert "No production decision" in fico_what_if(session, "what if fico was 820")
    session = saved_session()
    session.status = "processing"
    assert "Wait for the current review" in fico_what_if(session, "what if fico was 820")
    session = saved_session()
    session.report.underwriting.loan_results[0].normalized_data["demo_decision"]["version"] = "old"
    assert "Reprocess the loan first" in fico_what_if(session, "what if fico was 820")


@pytest.mark.parametrize("change", ["absent", "null", "malformed", "missing-knockout"])
def test_legacy_or_incomplete_criteria_have_an_actionable_reply(change):
    session = saved_session()
    demo = session.report.underwriting.loan_results[0].normalized_data["demo_decision"]
    if change == "absent":
        demo.pop("criteria")
    elif change == "null":
        demo["criteria"] = None
    elif change == "malformed":
        demo["criteria"] = [{"criterion": "fico", "status": "UNKNOWN"}]
    else:
        demo["criteria"] = [item for item in demo["criteria"] if item["criterion"] != "fico"]
    assert "incomplete decision checks" in fico_what_if(session, "what if fico was 820")


def test_selected_loan_scope_never_borrows_another_loans_facts():
    session = saved_session()
    other = copy.deepcopy(session.report.underwriting.loan_results[0])
    other.loan_id = "L2"
    other.normalized_data["demo_decision"]["normalized_facts"]["sbss"] = "195"
    next(check for check in other.normalized_data["demo_decision"]["criteria"]
         if check["criterion"] == "sbss")["status"] = "PASS"
    session.report.underwriting.loan_results.append(other)
    assert "Hypothetical result: **Decline**" in fico_what_if(session, "what if fico was 820")
    assert "Hypothetical result: **Approve**" in fico_what_if(session, "what if fico was 820", "L2")
    assert "Select and review one loan" in fico_what_if(session, "what if fico was 820", "not-found")
    session.selected_loan_id = None
    assert "Select and review one loan" in fico_what_if(session, "what if fico was 820")


def test_chat_uses_demo_context_and_prompt_not_strict_diagnostics():
    session = saved_session()
    session.messages = [Message(role="assistant", content="Old STRICT FICO_BAND_DIAGNOSTIC is UNKNOWN.")]
    store = Mock()
    store.recall.return_value = []
    context = chat_context(store, session, "Why was it declined?")
    trace = context["report"]["underwriting"]
    assert trace["explanation_basis"] == "BOUNDED_TEST_DEMO_ONLY"
    assert "report_selection" not in json.dumps(trace)
    assert "POLICY_REVIEW" not in json.dumps(trace)
    assert context["local_preflight"] is None and context["underwriting_rule_catalog"] == []
    llm = Mock(spec=AnthropicLLM)
    llm.complete.return_value = "Decline: SBSS below 160."
    chat(llm, store, session, "Why was it declined?")
    assert TEST_CHAT_INSTRUCTIONS in llm.complete.call_args.args[0]
    assert "from prior messages or memory" in llm.complete.call_args.args[0]


def test_api_saves_scenario_messages_without_mutating_report_or_calling_provider(tmp_path, monkeypatch):
    provider = Mock(side_effect=AssertionError("No provider request expected"))
    monkeypatch.setattr(AnthropicLLM, "complete", provider)
    with TestClient(create_app(tmp_path / "data")) as client:
        session = saved_session()
        client.app.state.store.save(session)
        before = client.get(f"/api/sessions/{session.id}").json()
        answer = client.post(f"/api/sessions/{session.id}/messages",
                             json={"content": "what if the credit score/fico was 820?"})
        assert answer.status_code == 200, answer.text
        after = answer.json()
        assert after["report"] == before["report"] and after["documents"] == before["documents"]
        assert after["selected_loan_id"] == before["selected_loan_id"]
        assert "620 -> 820" in after["messages"][-1]["content"]
        assert after["messages"][-1]["origin"] == "local_assistant"
        assert client.get("/api/usage").json()["total"] == 0
        provider.assert_not_called()
        assert client.get(f"/api/sessions/{session.id}").json()["messages"] == after["messages"]
