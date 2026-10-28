"""Draft design-guide regression tests: no live LLM or lending decisions."""

import json
from copy import deepcopy

import pytest

from backend.agents import DecisionAgent, RulesAgent
from backend.commentary import explain
from backend.llm import AnthropicLLM, LLMError
from backend.llm_context import report_context
from backend.models import Document, Session
from backend.policy import CONFIG_PATH, boolean_value, decimal_value, evaluate_profile, evaluate_session
from backend.reports import render_report


@pytest.fixture
def config():
    value = json.loads(CONFIG_PATH.read_text())
    value["amount_unit_confirmed"] = True
    value["effective_tgc_formula_confirmed"] = True
    return value


@pytest.fixture
def profile():
    return dict(account_id="synthetic-account", loan_id="synthetic-loan", borrower_role="borrower", product="express_baloc",
                requested_path="quick_app", requested_amount="100000000", new_money="100000000", tier_i="250000000", tier_ii="0",
                is_nfp=False, secured=False, ofac_disposition="cleared", credit_freeze=False,
                is_startup=False, is_investment_re=False, is_increase=False)


def results(profile, config):
    outcome, rules = evaluate_profile(profile, config, "synthetic-only", ["test.xlsx/Sheet!A2"])
    return outcome, {rule.rule_id: rule for rule in rules}


@pytest.mark.parametrize("field,value,rule,status", [
    ("new_money", "100000000", "MTB-QUICK-NEW-MONEY", "not_triggered"),
    ("new_money", "100000001", "MTB-QUICK-NEW-MONEY", "triggered"),
    ("tier_i", "250000000", "MTB-QUICK-TIER-I", "not_triggered"),
    ("tier_i", "250000001", "MTB-QUICK-TIER-I", "triggered"),
    ("requested_amount", "100000001", "MTB-EXPRESS-AMOUNT", "triggered"),
])
def test_exact_quick_app_boundaries(profile, config, field, value, rule, status):
    profile[field] = value
    _, checks = results(profile, config)
    assert checks[rule].status == status
    assert checks[rule].page in (1, 2)


def test_units_and_tgc_must_be_confirmed(profile, config):
    config["amount_unit_confirmed"] = False
    config["effective_tgc_formula_confirmed"] = False
    _, checks = results(profile, config)
    assert checks["MTB-QUICK-NEW-MONEY"].status == "unknown"
    assert checks["MTB-TGC-FORMULA"].status == "unknown"


def test_confirmed_ofac_not_unverified_alert(profile, config):
    profile["ofac_disposition"] = "confirmed_match"
    outcome, _ = results(profile, config)
    assert outcome.route == "COMPLIANCE_HOLD"
    profile["ofac_disposition"] = ""
    profile["ofac_alert"] = True
    outcome, checks = results(profile, config)
    assert outcome.route == "MANUAL_REVIEW"
    assert checks["MTB-OFAC-CONFIRMED"].status == "unknown"
    assert checks["MTB-OFAC-ALERT"].status == "triggered"


def test_nfp_routes_full_underwriting_not_denial(profile, config):
    profile["is_nfp"] = True
    outcome, checks = results(profile, config)
    assert outcome.route == "FULL_UNDERWRITING"
    assert checks["MTB-QUICK-NFP"].status == "triggered"


def test_freeze_and_unknown_booleans(profile, config):
    profile["credit_freeze"] = True
    outcome, checks = results(profile, config)
    assert outcome.route == "MANUAL_REVIEW"
    assert checks["MTB-CREDIT-FREEZE"].status == "triggered"
    assert boolean_value(None) is None
    assert boolean_value("unknown") is None
    assert boolean_value("false") is False


@pytest.mark.parametrize("amount,status", [("25000000", "not_triggered"), ("25000001", "triggered")])
def test_startup_and_investment_caps(profile, config, amount, status):
    profile.update(requested_amount=amount, is_startup=True, is_investment_re=True)
    _, checks = results(profile, config)
    assert checks["MTB-CAP-IS_STARTUP"].status == status
    assert checks["MTB-CAP-IS_INVESTMENT_RE"].status == status
    profile["product"] = "term_loan"
    _, checks = results(profile, config)
    assert "MTB-CAP-IS_STARTUP" not in checks


@pytest.mark.parametrize("as_of,status", [("2026-07-30", "triggered"), ("2026-07-31", "not_triggered"), ("2026-08-01", "not_triggered")])
def test_six_calendar_month_increase_boundary(profile, config, as_of, status):
    profile.update(is_increase=True, account_opened="2026-01-31", as_of_date=as_of)
    _, checks = results(profile, config)
    assert checks["MTB-SIX-MONTH-INCREASE"].status == status


def test_product_security_and_bil_letter(profile, config):
    profile.update(product="conventional_baloc", secured=False, requested_amount="100000001")
    outcome, checks = results(profile, config)
    assert outcome.route == "PRODUCT_INELIGIBLE"
    assert checks["MTB-CONVENTIONAL-SECURITY"].status == "triggered"
    profile.update(product="bil", secured=False, requested_amount="100000000", acceptance_letter=False, collateral_taken=False)
    _, checks = results(profile, config)
    assert checks["MTB-BIL-LETTER"].status == "triggered"
    assert checks["MTB-EXPRESS-AMOUNT"].status == "not_triggered"


def test_conversion_and_sba_validation(profile, config):
    profile.update(conversion_from="sba_baloc", conversion_to="conventional_baloc", scores_as_sba=True)
    original = deepcopy(profile)
    _, checks = results(profile, config)
    assert checks["MTB-BALOC-CONVERSION"].status == "triggered"
    assert checks["MTB-SBA-DOUBLING"].status == "triggered"
    assert profile == original


def test_missing_risk_thresholds_never_invented(profile, config):
    profile.update(bankruptcies=3, past_due="100000", sbss=200, credit_score=800)
    _, checks = results(profile, config)
    for rule in ("MTB-RISK-BANKRUPTCIES", "MTB-RISK-PAST_DUE", "MTB-SCORE-SBSS", "MTB-SCORE-CREDIT_SCORE"):
        assert checks[rule].status == "unknown"
    config["multiple_bankruptcies_threshold"] = "2"
    _, checks = results(profile, config)
    assert checks["MTB-RISK-BANKRUPTCIES"].status == "triggered"


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "-1", "$100M", "1,000"])
def test_invalid_numbers_are_unknown(value):
    assert decimal_value(value) is None


def test_schema_only_report_is_draft_manual_review(monkeypatch):
    monkeypatch.setattr(AnthropicLLM, "complete", lambda *args: (_ for _ in ()).throw(LLMError("No test network")))
    session = Session(documents=[Document(name=f"MTB_Query_{n}.xlsx", size=1, extension=".xlsx", metadata={"records": [], "data_row_count": 0}) for n in (1, 2, 4)])
    session.report = DecisionAgent().decide(session, RulesAgent().evaluate(session))
    policy = session.report.policy_evaluation
    assert policy is not None
    assert session.report.decision == "needs_review"
    assert policy.recommended_route == "MANUAL_REVIEW"
    assert policy.decision_recommendation is None
    assert policy.requires_human_approval
    assert len(policy.profiles) == 0
    assert len([item for item in policy.missing_information if "no data records" in item]) == 3
    # New runs use verified v4, not the old evaluator's p.4 draft rule.
    assert session.report.underwriting is not None
    assert session.report.underwriting.loan_results == []
    assert session.report.underwriting.source_document in render_report(session)
    assert "No explicit loan records" in render_report(session)
    assert "No lending approval or denial" in session.report.summary
    assert report_context(session.report)["policy_evaluation"]["recommended_route"] == "MANUAL_REVIEW"


def test_no_cross_record_join_or_indicator_guessing():
    docs = [Document(name="MTB_Query_1.xlsx", size=1, extension=".xlsx", metadata={"records": [
        {"sheet": "S", "row": 2, "values": {"LLC_BI__Account__c": "A"}, "sources": {"LLC_BI__Account__c": "S!A2"}}]}),
        Document(name="MTB_Query_2.xlsx", size=1, extension=".xlsx", metadata={"records": [
        {"sheet": "S", "row": 2, "values": {"SCOC__IndicatorKey__c": "is_nfp", "SCOC__IndicatorValue__c": "false"},
         "sources": {"SCOC__IndicatorKey__c": "S!A2", "SCOC__IndicatorValue__c": "S!B2"}}]})]
    policy = evaluate_session(Session(documents=docs))
    assert policy is not None
    assert len(policy.profiles) == 2
    assert all(item.route == "MANUAL_REVIEW" for item in policy.profiles)
    assert any("MTB-IDENTITY" in item for item in policy.missing_information)


@pytest.mark.parametrize("change,error", [
    ({"recommended_route": "APPROVAL_CANDIDATE"}, "contradicted"),
    ({"triggered_rules": ["INVENTED"]}, "unsupported"),
    ({"confidence": 0.9}, "confidence"),
    ({"decision_recommendation": "APPROVE"}, "invalid structured"),
])
def test_structured_commentary_guards(monkeypatch, change, error):
    session = Session(documents=[Document(name="MTB_Query_1.xlsx", extension=".xlsx", size=1)])
    policy = evaluate_session(session)
    from backend.models import Report
    report = Report(decision="needs_review", summary="Draft", findings=[], document_count=1, policy_evaluation=policy)
    data = dict(recommended_route="MANUAL_REVIEW", decision_recommendation=None, confidence=None,
                positive_factors=[], risk_factors=[], triggered_rules=["MTB-DRAFT-POLICY"],
                missing_information=[], explanation="Human review required (PDF p.4).", requires_human_approval=True)
    data.update(change)
    monkeypatch.setattr(AnthropicLLM, "complete", lambda *args: json.dumps(data))
    with pytest.raises(LLMError, match=error):
        explain(AnthropicLLM(), report)
    assert report.llm_policy_output is None


def test_structured_commentary_success(monkeypatch):
    from backend.models import Report
    policy = evaluate_session(Session(documents=[Document(name="MTB_Query_1.xlsx", extension=".xlsx", size=1)]))
    report = Report(decision="needs_review", summary="Draft", findings=[], document_count=1, policy_evaluation=policy)
    data = dict(recommended_route="MANUAL_REVIEW", decision_recommendation=None, confidence=None,
                positive_factors=[], risk_factors=["Schema-only"], triggered_rules=["MTB-DRAFT-POLICY"],
                missing_information=["Borrower records"], explanation="Human review required (PDF p.4).", requires_human_approval=True)
    monkeypatch.setattr(AnthropicLLM, "complete", lambda *args: json.dumps(data))
    assert explain(AnthropicLLM(), report) == data["explanation"]
    assert report.llm_policy_output is not None
    assert report.llm_policy_output.requires_human_approval