"""Diagram eligibility/order never becomes operational authority."""
import pytest

from backend.business_workflow import interpret_business_workflow


def evaluate(profile=None, results=None, issues=None):
    return interpret_business_workflow(
        profile=profile or {}, identity_valid=True, graph_issues=issues or [],
        knockout_results=results if results is not None else [{"rule_id": "SBSS", "status": "PASS"}],
        strategy={"credit_assessment": "SIMULATED_APPROVAL_CANDIDATE"})


def test_missing_eligibility_does_not_substitute_existing_fields():
    output = evaluate({"requested_path": "quick_app", "tier_i": "100", "requested_amount": "100", "is_investment_re": False, "secured": False})
    assert output["eligibility"] == "UNKNOWN"
    assert output["recommendation_candidate"] == "REFER"
    assert output["stages"][2]["status"] == "NOT_EVALUATED"


@pytest.mark.parametrize("amount,status", [("250000", "PASS"), ("250000.01", "OUT_OF_SCOPE"), (250000.0, "UNKNOWN"), (True, "UNKNOWN"), ("-1", "UNKNOWN")])
def test_exact_group_credit_boundary(amount, status):
    output = evaluate({"requested_path": "quick_app", "group_credit_usd": amount, "real_estate_secured": False})
    assert output["eligibility"] == status
    assert output["authorization"] == "NOT_ISSUED"
    assert output["human_review_required"] is True


def test_real_estate_secured_is_outside_scope_before_knockouts():
    output = evaluate({"requested_path": "quick_app", "group_credit_usd": "1", "real_estate_secured": True}, [{"rule_id": "SBSS", "status": "FAIL"}])
    assert output["recommendation_candidate"] == "REFER"
    assert output["stages"][2]["status"] == "NOT_EVALUATED"


def test_knockout_precedes_remaining_strategy_and_graph_defects_hold():
    profile = {"requested_path": "quick_app", "group_credit_usd": "1", "real_estate_secured": False}
    result = [{"rule_id": "SBSS", "status": "FAIL"}]
    output = evaluate(profile, result)
    assert output["recommendation_candidate"] == "DECLINE_CANDIDATE"
    assert output["stages"][3]["status"] == "NOT_EVALUATED"
    assert evaluate(profile, result, ["CONFLICT"])["recommendation_candidate"] == "REFER"
    assert evaluate(profile, [])["recommendation_candidate"] == "REFER"


def test_no_claim_of_accuracy_or_lender_authority():
    output = evaluate({"requested_path": "quick_app", "group_credit_usd": "1", "real_estate_secured": False})
    assert output["stages"][-2]["status"] == "NOT_IMPLEMENTED"
    assert output["stages"][-1]["status"] == "REQUIRED"
    assert "DIAGRAM_250K_GROUP_CREDIT_VS_V4_250M_TIER_I" in output["policy_conflicts"]