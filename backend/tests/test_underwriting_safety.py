"""Additional adversarial and provenance tests; all data and policy are synthetic."""

import copy
from datetime import date
from decimal import Decimal

import pytest

from backend.entity_graph import build_entity_graph
from backend.tests.test_underwriting import document, evaluate, observations, rows, rule, synthetic_policy
from backend.underwriting_engine import calendar_month_anniversary, evaluate_underwriting, load_underwriting_config


@pytest.mark.parametrize("opened,asof,six,twelve", [
    ("2026-04-20", "2026-10-19", False, False),
    ("2026-04-20", "2026-10-20", True, False),
    ("2025-10-20", "2026-10-20", True, True),
    ("2026-08-31", "2027-02-28", True, False),
    (None, "2026-10-20", None, None),
    ("2027-01-01", "2026-10-20", None, None),
])
def test_increase_six_and_twelve_months_remain_distinct(opened, asof, six, twelve):
    source = rows()
    source[1][1].update(account_opened=opened, as_of_date=asof, is_increase=True)
    result = evaluate(source)
    assert result.normalized_data["increase_at_least_6_months"] is six
    assert result.normalized_data["increase_at_least_12_months"] is twelve
    assert "INCREASE_WAIT_6_VS_12_MONTHS" in result.conflicts
    assert rule(result, "INCREASE_WAIT_POLICY").status == "UNKNOWN"
    assert calendar_month_anniversary(date(2026, 8, 31), 6) == date(2027, 2, 28)


@pytest.mark.parametrize("table,key", [("Account", "dob"), ("Account", "ownership_years"), ("Account", "ebitda"),
                                        ("Account", "is_nfp"), ("Account", "naics"), ("Loan", "tier_ii"),
                                        ("Loan", "new_money"), ("Loan", "as_of_date"),
                                        ("CreditReport", "personal_fico"), ("CreditReport", "report_date"),
                                        ("CreditReport", "ofac_disposition"), ("CreditReport", "ofac_alert"),
                                        ("CreditReport", "credit_freeze"), ("CreditReport", "bankruptcy_count"),
                                        ("LCRESPONSE", "sbss")])
def test_omitted_core_evidence_never_allows_synthetic_approval(table, key):
    source = rows()
    next(fields for name, fields in source if name == table).pop(key)
    config = synthetic_policy()
    # A trivial positive criterion cannot mask unknown required evidence.
    config["test_policy"]["criteria"] = [{"rule_id": "TEST-IDENTITY", "feature": "identity_valid", "op": "eq", "value": True}]
    assert evaluate(source, config=config).recommendation_path == "SIMULATED_REVIEW"


@pytest.mark.parametrize("field,value", [("ofac_disposition", "confirmed_match"), ("ofac_alert", True), ("credit_freeze", True)])
def test_compliance_issue_requires_review_even_if_synthetic_score_criteria_pass(field, value):
    source = rows()
    source[2][1][field] = value
    assert evaluate(source, config=synthetic_policy()).recommendation_path == "SIMULATED_REVIEW"


@pytest.mark.parametrize("status", [None, "", "unknown", "not completed"])
def test_unknown_report_status_not_silently_ignored(status):
    source = rows() + [("CreditReport", {"Id": "R2", "account_id": "A1", "report_date": "2026-10-20", "status": status, "fico": "649"})]
    assert evaluate(source).normalized_data["selected_report_id"] is None


def test_other_account_entity_response_cannot_supply_borrower_score_or_indicators():
    source = rows() + observations() + [("Account", {"Id": "A2"})]
    next(fields for table, fields in source if table == "LCREntity")["account_id"] = "A2"
    result = evaluate(source)
    assert result.normalized_data["sbss"] is None
    assert result.normalized_data["indicators"] == []


def test_response_without_loan_needs_both_explicit_applicable_id_and_account_entity():
    source = rows() + observations()
    source[1][1]["response_id"] = "S1"
    del source[3][1]["loan_id"]
    assert evaluate(source).normalized_data["sbss_bucket"] == "GE_200"
    del source[1][1]["response_id"]
    assert evaluate(source).normalized_data["sbss_bucket"] is None


def test_decimal_equivalent_duplicate_money_merges():
    source = rows()
    duplicate = copy.deepcopy(source[1][1])
    duplicate["requested_amount"] = "100000.0000"
    source.append(("Loan", duplicate))
    result = evaluate(source)
    assert result.normalized_amounts_usd["loan_requested_amount_usd"] == Decimal("100000")
    assert not any("CONFLICTING_DUPLICATE" in issue for issue in result.conflicts)


def test_conflicting_aliases_hold_no_selection_by_priority():
    doc = document([("Data", {"Loan_Request_Id": "L1", "LLC_BI__Loan__c": "L2", "borrower_account_id": "A1"})], "MTB_Query_1.xlsx")
    batch = evaluate_underwriting([doc])
    assert any("CONFLICTING_ALIASES" in issue for issue in batch.data_quality_issues)
    assert not batch.loan_results


def test_flattened_explicit_role_has_auditable_source_evidence():
    doc = document([("Data", {"loan_id": "L1", "account_id": "A1", "borrower_role": "Borrower"})], "MTB_Query_1.xlsx")
    result = evaluate_underwriting([doc]).loan_results[0]
    role_evidence = [item for item in result.source_evidence if item.field == "borrower_role"]
    assert len(role_evidence) == 1
    assert role_evidence[0].id in rule(result, "IDENTITY_EXPLICIT_RELATIONSHIP").evidence_refs


def test_same_key_identical_observations_retained_not_conflicted():
    source = rows() + observations() + [("Indicator", {"Id": "I3", "analysis_id": "D1", "key": "OWNERSHIP", "value": "2"})]
    result = evaluate(source)
    assert len(result.normalized_data["indicators"]) == 3
    assert not any("CONFLICTING_INDICATOR" in issue for issue in result.conflicts)


def test_explicit_indicator_mapping_is_required_and_money_targets_are_unsupported():
    source = rows() + observations()
    del source[0][1]["ownership_years"]
    assert evaluate(source).normalized_data["ownership_at_least_2_years"] is None
    config = {"mapping_manifest": {"indicator_feature_mapping": {"OWNERSHIP": "ownership_years"}}}
    result = evaluate(source, config=config)
    assert result.normalized_data["ownership_at_least_2_years"] is True
    assert rule(result, "OWNERSHIP_2_YEARS_DIAGNOSTIC").evidence_refs
    config["mapping_manifest"]["indicator_feature_mapping"]["OWNERSHIP"] = "requested_amount"
    assert evaluate(source, config=config).recommendation_path == "SIMULATED_REVIEW"


@pytest.mark.parametrize("override", [{"test_policy": "invalid"}, {"test_policy": {"resolved_conflicts": [None]}},
                                        {"minimum_age": True}, {"minimum_age": -1}, {"minimum_age": 121},
                                        {"high_risk_naics": [123]}, {"unresolved_conflicts": [None]},
                                        {"source_document": "old-draft.pdf"}])
def test_invalid_config_fails_explicitly(override):
    with pytest.raises(ValueError):
        evaluate(config=override)


def test_unhashable_test_operator_yields_unknown_not_exception():
    config = synthetic_policy()
    config["test_policy"]["criteria"][0]["op"] = []
    assert evaluate(config=config).recommendation_path == "SIMULATED_REVIEW"


def test_conflicting_duplicate_ids_poison_all_later_observations():
    source = rows() + [("Account", {"Id": "A1", "ownership_years": "3"}),
                       ("Account", {"Id": "A1", "ownership_years": "2"})]
    graph = build_entity_graph([document(source)], load_underwriting_config())
    assert graph.table("Account")["A1"].fields["ownership_years"] is None
    assert "ownership_years" in graph.table("Account")["A1"].poisoned


def test_source_and_rule_ids_stable_for_identical_input():
    first = evaluate()
    second = evaluate()
    assert first.model_dump_json() == second.model_dump_json()


def test_empty_input_batch_no_fabricated_loans():
    batch = evaluate_underwriting([])
    assert batch.loan_results == []
    assert batch.data_quality_issues == ["NO_EXPLICIT_LOAN_RECORDS"]