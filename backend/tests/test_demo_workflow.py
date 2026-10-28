"""TEST demo regressions: no provider calls, no replacement of strict results."""

import copy
import json
from unittest.mock import Mock

import pytest

from backend.demo_workflow import SELECTION_BASIS, bounded_demo_context, evaluate_demo
from backend.entity_graph import build_entity_graph
from backend import commentary, prompts
from backend.expanded_spec import REASONS
from backend.llm import AnthropicLLM, LLMError
from backend.llm_context import demo_explanation_context, report_context
from backend.loan_review import review_selected_loan
from backend.models import Report, Session
from backend.tests.test_underwriting import document, rows
from backend.underwriting_engine import evaluate_underwriting, load_underwriting_config


def evaluate(source=None, mode="TEST"):
    return evaluate_underwriting([document(rows() if source is None else source)], mode=mode)


def demo(source=None):
    return evaluate(source).loan_results[0].normalized_data["demo_decision"]


def criterion(result, key):
    return next(item for item in result["criteria"] if item["criterion"] == key)


def test_known_low_scores_decline_without_any_dates_and_without_other_inputs():
    source = rows()
    source[1][1].pop("as_of_date")
    source[2][1].pop("report_date")
    source[2][1]["personal_fico"] = "620"
    source[3][1]["sbss"] = "155"
    source[0][1].pop("ebitda")
    result = evaluate(source).loan_results[0]
    view = result.normalized_data["demo_decision"]
    assert view["label"] == "Decline"
    assert view["selection_basis"] == SELECTION_BASIS
    assert view["selected_report_id"] == "R1"
    assert view["report_date_checked"] is False
    assert view["report_completion_checked"] is False
    assert view["freshness_asserted"] is False
    assert criterion(view, "fico")["status"] == "FAIL"
    assert criterion(view, "sbss")["status"] == "FAIL"
    assert criterion(view, "age_years")["status"] == "UNKNOWN"
    assert view["simulation_only"] and view["human_review_required"]
    assert view["authorization"] == "NOT_ISSUED"
    assert result.normalized_data["selected_report_id"] is None
    assert result.normalized_data["fico"] is None
    assert result.recommendation_path == "SIMULATED_REVIEW"


def test_no_report_does_not_crash_and_unknown_does_not_pass():
    view = demo([row for row in rows() if row[0] != "CreditReport"])
    assert view["label"] == "Refer"
    assert view["selected_report_id"] is None
    assert criterion(view, "fico")["status"] == "UNKNOWN"
    assert "fico" in view["missing_inputs"]


def test_no_report_still_allows_known_sbss_knockout():
    source = [row for row in rows() if row[0] != "CreditReport"]
    source[-1][1]["sbss"] = "155"
    assert demo(source)["label"] == "Decline"


def test_two_loans_keep_separate_account_reports_and_scores():
    source = rows()
    source.extend([
        ("Account", {"Id": "A2", "dob": "1980-01-01", "ownership_years": "2",
                     "naics": "541100", "ebitda": "1"}),
        ("Loan", {"Id": "L2", "borrower_account_id": "A2", "as_of_date": "2026-10-20"}),
        ("CreditReport", {"Id": "R2", "account_id": "A2", "personal_fico": "620",
                          "max_delinquency_public_records": "0", "bankruptcy_count": "0"}),
        ("LCRESPONSE", {"Id": "S2", "loan_id": "L2", "sbss": "200"}),
    ])
    views = {loan.loan_id: loan.normalized_data["demo_decision"] for loan in evaluate(source).loan_results}
    assert views["L1"]["label"] == "Approve"
    assert views["L1"]["selected_report_id"] == "R1"
    assert views["L2"]["label"] == "Decline"
    assert views["L2"]["selected_report_id"] == "R2"
    first_refs = {item["id"] for item in views["L1"]["source_evidence"]}
    second_refs = {item["id"] for item in views["L2"]["source_evidence"]}
    assert not first_refs & second_refs


def test_missing_asof_blocks_only_age_and_positive_bankruptcy_not_report_selection():
    source = rows()
    source[1][1].pop("as_of_date")
    source[2][1].pop("report_date")
    view = demo(source)
    assert view["label"] == "Refer"
    assert view["selected_report_id"] == "R1"
    assert criterion(view, "bankruptcy_clear")["status"] == "PASS"
    assert criterion(view, "fico")["status"] == "PASS"
    assert criterion(view, "age_years")["status"] == "UNKNOWN"
    assert "as-of" in criterion(view, "age_years")["detail"]
    source[2][1]["bankruptcy_count"] = "1"
    assert criterion(demo(source), "bankruptcy_clear")["status"] == "UNKNOWN"


def test_source_order_and_multi_account_isolation_not_latest_or_best_report():
    source = rows()
    source[2][1].update(report_date="2030-01-01", status="pending", personal_fico="620")
    source.insert(2, ("CreditReport", {"Id": "WRONG", "account_id": "A2", "personal_fico": "800"}))
    source.insert(3, ("Account", {"Id": "A2"}))
    source.append(("CreditReport", {"Id": "R2", "account_id": "A1", "personal_fico": "800"}))
    view = demo(source)
    assert view["selected_report_id"] == "R1"
    assert view["normalized_facts"]["fico"] == "620"
    assert view["label"] == "Decline"
    assert not any(item["record_id"] == "WRONG" for item in view["source_evidence"])
    source[4], source[-1] = source[-1], source[4]
    assert demo(source)["selected_report_id"] == "R2"


@pytest.mark.parametrize("typed", [False, True])
def test_generic_fico_requires_explicit_personal_category_and_borrower_role(typed):
    source = rows()
    source[2][1].pop("personal_fico")
    source[2][1]["fico"] = "620"
    source[2][1]["report_category"] = "personal" if typed else "business"
    view = demo(source)
    assert criterion(view, "fico")["status"] == ("FAIL" if typed else "UNKNOWN")
    assert view["label"] == ("Decline" if typed else "Refer")


@pytest.mark.parametrize("score,ownership,fico,amount,naics,vehicle,label,route", [
    ("159", "2", "760", "100000", "541100", False, "Decline", None),
    ("160", "2", "650", "1000000", "541100", False, "Approve", "SBA"),
    ("189", "1", "760", "1000000", "541100", False, "Approve", "SBA"),
    ("175", "1", "650", "100000.00", "541100", False, "Approve", "SBA"),
    ("175", "1", "759", "100000.01", "541100", False, "Refer", None),
    ("175", "1", "759", "100000000.00", "541100", False, "Refer", None),
    ("175", "1", "759", "100000000.01", "541100", False, "Decline", None),
    ("190", "2", "650", "100000", "541100", False, "Approve", "SBA"),
    ("199", "2", "650", "100000", "541100", True, "Approve", "CONVENTIONAL"),
    ("200", "1.99", "650", "100000", "541100", False, "Approve", "SBA"),
    ("200", "2", "650", "100000", "231111", False, "Approve", "SBA"),
    ("200", "2", "650", "100000", "541100", False, "Approve", "CONVENTIONAL"),
])
def test_tree_boundaries(score, ownership, fico, amount, naics, vehicle, label, route):
    source = rows()
    source[0][1].update(ownership_years=ownership, naics=naics)
    source[1][1].update(requested_amount=amount, vehicle_only=vehicle)
    source[2][1]["personal_fico"] = fico
    source[3][1]["sbss"] = score
    view = demo(source)
    assert (view["label"], view["candidate_route"]) == (label, route)
    if label == "Refer":
        assert view["conflicts"] == ["REQUEST_THRESHOLD_100M_VS_100K"]
        assert "thresholds conflict" in view["rationale"]


def test_vehicle_flag_required_only_for_sba_and_no_outside_tree_gates():
    source = rows()
    for field in ("requested_path", "tier_i", "tier_ii", "is_investment_re", "vehicle_only"):
        source[1][1].pop(field)
    assert demo(source)["label"] == "Approve"  # conventional, outside gates irrelevant
    source[3][1]["sbss"] = "190"
    view = demo(source)
    assert view["label"] == "Refer"
    assert "vehicle_only" in view["missing_inputs"]


@pytest.mark.parametrize("field,value", [("dob", None), ("ebitda", None)])
def test_missing_knockout_inputs_never_clear_approval(field, value):
    source = rows()
    source[0][1][field] = value
    source[0][1]["SBSS7_earn_before_int_and_taxes__c"] = "100"
    assert demo(source)["label"] == "Refer"


@pytest.mark.parametrize("field,value", [("ebitda", "-0.01"), ("dob", "2008-10-21")])
def test_known_account_knockout_shortcircuits_missing_report(field, value):
    source = [row for row in rows() if row[0] != "CreditReport"]
    source[0][1][field] = value
    assert demo(source)["label"] == "Decline"


@pytest.mark.parametrize("code", ["1", "2", "3"])
def test_known_public_record_codes(code):
    source = rows()
    source[2][1]["max_delinquency_public_records"] = code
    source[0][1].pop("dob")
    assert demo(source)["label"] == "Decline"


@pytest.mark.parametrize("discharge,label", [("2021-10-20", "Approve"), ("2021-10-21", "Decline"),
                                           (None, "Refer"), ("2027-01-01", "Refer")])
def test_five_calendar_year_discharge_boundary(discharge, label):
    source = rows()
    source[2][1].update(bankruptcy_count="1", bankruptcy_discharge_date=discharge)
    assert demo(source)["label"] == label


def test_query2_mapped_scalar_score_works_without_response_id():
    source = rows()[:3]
    source[1][1].pop("as_of_date")
    loan_fk = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r."
    score = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__LCResponse__r.SCOC__SBSS7Score__c"
    q2 = document([("Data", {"Id": "I1", loan_fk + "LLC_BI__Loan__c": "L1",
                            loan_fk + "LLC_BI__Account__c": "A1",
                            loan_fk + "LLC_BI__Borrower_Type__c": "Borrower", score: "155",
                            "SCOC__IndicatorKey__c": "uninterpreted", "SCOC__IndicatorValue__c": "raw"})],
                  "MTB_Query_2_Validated_Revision_SOQL.xlsx", "q2")
    q2.metadata["sheets"] = [{"name": "Data", "headers": list(q2.metadata["records"][0]["values"])}]
    result = evaluate_underwriting([document(source), q2], mode="TEST").loan_results[0]
    view = result.normalized_data["demo_decision"]
    assert view["label"] == "Decline"
    assert view["normalized_facts"]["sbss"] == "155"
    assert criterion(view, "sbss")["evidence_refs"]
    assert any(item["field"] == score for item in view["source_evidence"])


def test_graph_documents_and_strict_result_are_immutable(monkeypatch):
    source = document(rows())
    original = copy.deepcopy(source)
    config = load_underwriting_config()
    graph = build_entity_graph([source], config)
    original_graph = copy.deepcopy(graph)
    evaluate_demo(graph, graph.table("Loan")["L1"], config, mode="TEST")
    assert graph == original_graph
    assert source.metadata == original.metadata
    actual = evaluate_underwriting([source], mode="TEST").model_dump()
    monkeypatch.setattr("backend.underwriting_engine.evaluate_demo", lambda *a, **k: None)
    strict = evaluate_underwriting([source], mode="TEST").model_dump()
    for payload in (actual, strict):
        payload["loan_results"][0]["normalized_data"].pop("demo_decision")
    assert actual == strict


def test_production_does_not_invoke_demo_or_include_trace(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Production evaluated TEST demo")
    monkeypatch.setattr("backend.underwriting_engine.evaluate_demo", forbidden)
    result = evaluate(mode="PRODUCTION").loan_results[0]
    assert "demo_decision" not in result.normalized_data
    assert result.decision_status == "Human Review"
    assert result.final_action == "MANUAL_REVIEW"
    assert evaluate_demo(None, None, {}, mode="PRODUCTION") is None


def test_bounded_context_has_simulated_scores_not_raw_bureau_or_source_values():
    batch = evaluate()
    loan = batch.loan_results[0]
    view = loan.normalized_data["demo_decision"]
    view["raw_report"] = {"SSN": "RAW_PRIVATE_SENTINEL"}
    view["rationale"] = "RAW_PRIVATE_SENTINEL"
    view["normalized_facts"]["bankruptcy_count"] = "RAW_PRIVATE_SENTINEL"
    view["criteria"][0]["value"] = "RAW_PRIVATE_SENTINEL"
    view["source_evidence"][0]["value"] = "RAW_PRIVATE_SENTINEL"
    report = Report(decision="needs_review", summary="Review", findings=[], document_count=1, underwriting=batch)
    before = report.model_dump_json()
    context = report_context(report)
    serialized = json.dumps(context)
    assert "RAW_PRIVATE_SENTINEL" not in serialized
    normalized = context["underwriting"]["loan_results"][0]
    assert normalized["demo_decision"]["label"] == "Approve"
    assert normalized["demo_decision"]["normalized_facts"]["fico"] == "760"
    assert "source_evidence" not in normalized["demo_decision"]
    assert context["decision_recommendation"] is None
    assert context["policy_evaluation"]["recommended_route"] == "MANUAL_REVIEW"
    assert before == report.model_dump_json()
    assert bounded_demo_context(view, mode="PRODUCTION", allowed_refs=set()) is None


def test_conflicting_same_report_scores_remain_unknown_not_overwritten():
    source = rows()
    source.append(("CreditReport", {"Id": "R1", "account_id": "A1", "personal_fico": "620"}))
    view = demo(source)
    assert view["selected_report_id"] == "R1"
    assert view["label"] == "Refer"
    assert criterion(view, "fico")["status"] == "UNKNOWN"
    assert len(criterion(view, "fico")["evidence_refs"]) == 2


def test_context_keeps_demo_fico_separate_from_strict_saved_selection_guard():
    source = rows()
    source[1][1].pop("as_of_date")
    source[2][1].pop("report_date")
    source[2][1]["personal_fico"] = "620"
    batch = evaluate(source)
    loan = batch.loan_results[0]
    # Simulate a legacy saved strict result that claimed an undated selection.
    loan.normalized_data.update(selected_report_id="R1", fico="800", report_date=None,
                                report_selection={"status": "SELECTED"})
    report = Report(decision="needs_review", summary="Review", findings=[], document_count=1, underwriting=batch)
    context = report_context(report)["underwriting"]["loan_results"][0]
    assert context["report_selection"]["status"] == "SAVED_REPORT_INCONSISTENT_REPROCESS_REQUIRED"
    assert context["normalized_features"]["fico"] is None
    assert context["demo_decision"]["normalized_facts"]["fico"] == "620"
    assert context["demo_decision"]["label"] == "Decline"
    assert "strict" not in context["demo_decision"]["rationale"].lower()


def test_context_global_budgets_remain_bounded_with_many_demo_loans():
    batch = evaluate()
    batch.loan_results = [batch.loan_results[0].model_copy(deep=True) for _ in range(25)]
    report = Report(decision="needs_review", summary="Review", findings=[], document_count=1, underwriting=batch)
    context = report_context(report)["underwriting"]
    assert len(context["loan_results"]) == 20
    assert len(context["evidence_refs"]) <= 40
    assert sum(len(item["rule_results"]) for item in context["loan_results"]) <= 40
    assert all(len(item["demo_decision"]["criteria"]) <= 10 for item in context["loan_results"])


def demo_report(source=None):
    return Report(decision="needs_review", summary="Review", findings=[], document_count=1,
                  underwriting=evaluate(source))


def response(loan_ids, **changes):
    return json.dumps({"recommended_route": "MANUAL_REVIEW", "decision_recommendation": None,
                       "confidence": None, "requires_human_approval": True, "loan_ids": loan_ids,
                       "positive_factors": [], "risk_factors": [], "missing_information": [],
                       "explanation": "Result: Refer. Why: SBSS 177 is above the knockout boundary.",
                       "triggered_rules": [], "evidence_refs": [], **changes})


@pytest.mark.parametrize("criterion_key,field,value,label,provenance", [
    ("bankruptcy_clear", "bankruptcy_discharge_date", "2025-01-01", "Bankruptcy", "SUPPLIED_DECLINE_REASON_CATALOG"),
    ("ebitda_usd", "ebitda", "-1", "Business expenses exceed business income", "SUPPLIED_DECLINE_REASON_CATALOG"),
    ("public_record_code", "max_delinquency_public_records", "2", "Derogatory personal credit", "SUPPLIED_DECLINE_REASON_CATALOG"),
    ("fico", "personal_fico", "620", "Average personal FICO below 650", "SUPPLIED_STRATEGY"),
    ("sbss", "sbss", "155", "SBSS below 160", "SUPPLIED_STRATEGY"),
    ("age_years", "dob", "2010-01-01", "Age below 18", "SUPPLIED_STRATEGY"),
])
def test_demo_matched_reason_labels_and_provenance(criterion_key, field, value, label, provenance):
    source = rows()
    node = 0 if field in {"ebitda", "dob"} else 3 if field == "sbss" else 2
    source[node][1][field] = value
    if criterion_key == "bankruptcy_clear":
        source[2][1]["bankruptcy_count"] = "1"
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert view["matched_reasons"] == [{
        "criterion": criterion_key, "label": label, "source": provenance, "status": "MATCHED",
        "evidence_refs": criterion(view, criterion_key)["evidence_refs"], "adverse_action_code": None,
    }]
    bounded = demo_explanation_context(report)["underwriting"]["loan_results"][0]["demo_decision"]
    assert bounded["matched_reasons"] == view["matched_reasons"]


def test_demo_does_not_activate_unmapped_catalog_or_clear_unknowns():
    source = rows()
    source[0][1].pop("ebitda")
    source[0][1].update(EBIT="100", attr={"ebitda": "100"})
    report = demo_report(source)
    loan = report.underwriting.loan_results[0]
    assert loan.normalized_data["demo_decision"]["matched_reasons"] == []
    assert criterion(loan.normalized_data["demo_decision"], "ebitda_usd")["status"] == "UNKNOWN"
    assert len(REASONS) == len(loan.expanded_spec.decline_reasons) == 43
    assert {item.label for item in loan.expanded_spec.decline_reasons} == {item[1] for item in REASONS}
    assert all(item.status == "NOT_EVALUATED" for item in loan.expanded_spec.decline_reasons
               if item.disposition in {"OUT_OF_SCOPE", "NOT_USED"})
    assert all(item.adverse_action_code is None for item in loan.expanded_spec.decline_reasons)


def test_aggregate_demo_177_isolated_from_strict_failures_and_raw_attributes():
    source = rows()
    source[3][1]["sbss"] = "177"
    source[0][1].pop("ebitda")
    report = demo_report(source)
    loan = report.underwriting.loan_results[0]
    loan.rule_results[0].status = "FAIL"
    loan.rule_results[0].detail = "STRICT_SBSS_BELOW_160_SENTINEL"
    loan.normalized_data["demo_decision"]["attr"] = {"raw": "RAW_SENTINEL"}
    # Saved stale FAIL statuses must not defeat the normalized score boundary.
    criterion(loan.normalized_data["demo_decision"], "sbss")["status"] = "FAIL"
    provider = Mock(spec=AnthropicLLM)
    provider.complete.return_value = response(["L1"])
    before = report.model_dump_json()
    commentary.explain(provider, report)
    provider.complete.assert_called_once()
    system, payload = provider.complete.call_args.args
    assert system == prompts.DEMO_SYSTEM + prompts.DEMO_AGGREGATE_INSTRUCTIONS
    assert prompts.V4_SYSTEM not in system and prompts.REVIEW_REASONING_GUIDANCE not in system
    assert "STRICT_SBSS_BELOW_160_SENTINEL" not in payload and "RAW_SENTINEL" not in payload
    context = json.loads(payload)
    scoped = context["underwriting"]["loan_results"][0]
    assert set(scoped) == {"loan_id", "demo_decision"}
    assert criterion(scoped["demo_decision"], "sbss")["status"] == "PASS"
    assert scoped["demo_decision"]["normalized_facts"]["sbss"] == "177"
    assert scoped["demo_decision"]["matched_reasons"] == []
    for key in ("rule_results", "expanded_spec", "normalized_features", "report_selection", "policy_evaluation"):
        assert key not in payload
    assert before == report.model_dump_json()
    provider.complete.return_value = response(["L1"], triggered_rules=[loan.rule_results[0].rule_id])
    with pytest.raises(LLMError, match="unsupported"):
        commentary.explain(provider, report)


def test_demo_aggregate_per_loan_low_score_and_evidence_isolation():
    source = rows()
    source[3][1]["sbss"] = "177"
    source.extend([
        ("Account", {"Id": "A2"}),
        ("Loan", {"Id": "L2", "borrower_account_id": "A2"}),
        ("CreditReport", {"Id": "R2", "account_id": "A2", "personal_fico": "620"}),
        ("LCRESPONSE", {"Id": "S2", "loan_id": "L2", "sbss": "155"}),
    ])
    report = demo_report(source)
    context = demo_explanation_context(report)["underwriting"]
    views = {loan["loan_id"]: loan["demo_decision"] for loan in context["loan_results"]}
    assert views["L1"]["normalized_facts"]["sbss"] == "177"
    assert views["L1"]["matched_reasons"] == []
    assert {r["label"] for r in views["L2"]["matched_reasons"]} == {
        "Average personal FICO below 650", "SBSS below 160"}
    refs = [{ref for item in view["criteria"] for ref in item["evidence_refs"]} for view in views.values()]
    assert not refs[0] & refs[1]
    assert "another loan's" in prompts.DEMO_SYSTEM
    assert "identify the loan ID" in prompts.DEMO_SYSTEM


def test_demo_dedicated_context_global_limits_and_production_fallback():
    report = demo_report()
    report.underwriting.loan_results = [report.underwriting.loan_results[0].model_copy(deep=True)
                                        for _ in range(25)]
    context = demo_explanation_context(report)["underwriting"]
    assert len(context["loan_results"]) == 20 and context["loan_count"] == 25
    assert len(context["evidence_refs"]) <= 40
    production = report.model_copy(update={
        "underwriting": report.underwriting.model_copy(update={"mode": "PRODUCTION"})})
    assert demo_explanation_context(production) is None
    for loan in report.underwriting.loan_results:
        loan.normalized_data["demo_decision"]["version"] = "untrusted"
    assert demo_explanation_context(report) is None


def test_selected_demo_prompt_excludes_strict_reconciliation_and_rejects_strict_ids(monkeypatch):
    session = Session(status="completed", report=demo_report())
    provider = Mock(model="test-model")
    provider.complete.return_value = response(["L1"], check_reconciliation=[])
    monkeypatch.setattr("backend.loan_review.AnthropicLLM", Mock(return_value=provider))
    before = session.report.model_dump_json()
    review = review_selected_loan(session, "L1")
    provider.complete.assert_called_once()
    system, payload = provider.complete.call_args.args
    assert system == prompts.DEMO_SYSTEM + prompts.DEMO_SELECTED_LOAN_INSTRUCTIONS
    context = json.loads(payload)
    assert context["checks_to_reconcile"] == []
    assert "reconciliation_scope" not in context
    assert "rule_results" not in payload and "expanded_spec" not in payload
    assert review.analysis.check_reconciliation == []
    assert review.context_note == prompts.DEMO_SELECTED_LOAN_CONTEXT_NOTE
    assert session.report.model_dump_json() == before
    provider.complete.return_value = response(["L1"], check_reconciliation=[{
        "rule_id": "STRICT_UNKNOWN", "evidence_state": "POLICY_UNRESOLVED",
        "rationale": "Unknown", "next_step": "Review", "evidence_refs": [],
    }])
    with pytest.raises(LLMError, match="reconcile"):
        review_selected_loan(session, "L1")


def test_selected_demo_after_twenty_scoped_before_bounds_and_preference_retained(monkeypatch):
    report = demo_report()
    template = report.underwriting.loan_results[0]
    report.underwriting.loan_results = [template.model_copy(deep=True, update={"loan_id": f"L{i}"})
                                        for i in range(1, 26)]
    selected = report.underwriting.loan_results[-1]
    selected.normalized_data["demo_decision"]["normalized_facts"]["sbss"] = "177"
    refs = {item["id"] for item in selected.normalized_data["demo_decision"]["source_evidence"]}
    preferred = criterion(selected.normalized_data["demo_decision"], "sbss")["evidence_refs"][0]
    assert preferred in refs
    session = Session(status="completed", report=report)
    provider = Mock(model="test-model")
    provider.complete.return_value = response(["L25"], check_reconciliation=[])
    monkeypatch.setattr("backend.loan_review.AnthropicLLM", Mock(return_value=provider))
    review_selected_loan(session, "L25", preferred)
    context = json.loads(provider.complete.call_args.args[1])
    assert context["user_review_preference"]["evidence_ref"] == preferred
    loans = context["underwriting"]["loan_results"]
    assert len(loans) == 1 and loans[0]["loan_id"] == "L25"
    assert loans[0]["demo_decision"]["normalized_facts"]["sbss"] == "177"
    assert not loans[0]["demo_decision"]["matched_reasons"]


def test_mixed_legacy_demo_batch_never_falls_back_to_strict_for_other_loans():
    report = demo_report()
    legacy = report.underwriting.loan_results[0].model_copy(deep=True, update={"loan_id": "LEGACY"})
    legacy.normalized_data.pop("demo_decision")
    report.underwriting.loan_results.append(legacy)
    context = demo_explanation_context(report)
    assert context["underwriting"]["loan_results"][1] == {"loan_id": "LEGACY", "demo_decision": None}
    assert "rule_results" not in json.dumps(context)


def test_bounded_demo_never_promotes_unknown_status_to_clearance():
    report = demo_report()
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    criterion(view, "sbss")["status"] = "UNKNOWN"
    bounded = demo_explanation_context(report)["underwriting"]["loan_results"][0]["demo_decision"]
    assert criterion(bounded, "sbss")["status"] == "UNKNOWN"
    assert bounded["matched_reasons"] == []