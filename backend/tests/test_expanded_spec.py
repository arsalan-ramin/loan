"""Expanded draft coverage, with synthetic fixtures only and no provider calls."""

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.expanded_spec import REASONS, REGISTRY
from backend.tests.test_underwriting import document, rows
from backend.underwriting_engine import evaluate_underwriting
from backend.underwriting_models import EXPANDED_SPEC_DOCUMENT, EXPANDED_SPEC_SHA256


def source():
    data = rows()
    data[0][1].update(financial_period_start="2025-01-01", financial_period_end="2025-12-31")
    data[1][1]["vehicle_only"] = False
    data[3][1]["score_date"] = "2026-10-19"
    data.append(("EntityInvolvement", {"Id": "J1", "loan_id": "L1", "account_id": "A1", "role": "borrower"}))
    return data


def result(data=None, **kwargs):
    return evaluate_underwriting([document(source() if data is None else data)], **kwargs).loan_results[0]


def test_source_is_fourteen_pages_and_separately_pinned():
    from pypdf import PdfReader
    path = Path(__file__).parents[1] / "data" / EXPANDED_SPEC_DOCUMENT
    assert hashlib.sha256(path.read_bytes()).hexdigest() == EXPANDED_SPEC_SHA256
    assert len(PdfReader(path).pages) == 14
    batch = evaluate_underwriting([document(source())])
    assert batch.expanded_spec_available
    assert batch.expanded_spec_source_sha256 == EXPANDED_SPEC_SHA256


def test_complete_registry_and_reason_inventory_are_separate_from_v4_outcomes():
    loan = result()
    spec = loan.expanded_spec
    assert spec is not None
    assert len(spec.decline_reasons) == len(REASONS) == 43
    assert len(spec.rule_results) == len(REGISTRY) == 19
    assert len({item.code for item in spec.decline_reasons}) == 43
    assert len({item.rule_id for item in spec.rule_results}) == 19
    assert spec.simulated_credit_route == "CONVENTIONAL_APPROVAL_CANDIDATE"
    assert loan.final_action == "MANUAL_REVIEW"
    assert spec.authorized_decision == loan.authorization == "NOT_ISSUED"
    assert all(item.adverse_action_code is None for item in spec.decline_reasons)
    assert all(item.source_document == EXPANDED_SPEC_DOCUMENT for item in spec.rule_results)
    assert all(item.version == "expanded-draft-1" for item in spec.rule_results)
    assert spec.rule_results == sorted(spec.rule_results, key=lambda item: (item.precedence, item.rule_id))
    assert len(spec.input_fingerprint_sha256) == 64
    assert {item.rule_id for item in spec.rule_results}.isdisjoint({item.rule_id for item in loan.rule_results})


@pytest.mark.parametrize("score,ownership,fico,naics,expected", [
    ("215", "5", "735", "541100", "CONVENTIONAL_APPROVAL_CANDIDATE"),
    ("215", "5", "645", "541100", "SIMULATED_DECLINE_CANDIDATE"),
    ("150", "5", "710", "541100", "SIMULATED_DECLINE_CANDIDATE"),
    ("180", "1", "780", "541100", "SBA_APPROVAL_CANDIDATE"),
    ("195", "5", "720", "541100", "SBA_APPROVAL_CANDIDATE"),
    ("210", "3", "735", "236220", "SBA_APPROVAL_CANDIDATE"),
    ("210", "3", "735", "541100", "CONVENTIONAL_APPROVAL_CANDIDATE"),
])
def test_section21_branch_candidates(score, ownership, fico, naics, expected):
    data = source()
    data[0][1].update(ownership_years=ownership, naics=naics)
    data[2][1]["personal_fico"] = fico
    data[3][1]["sbss"] = score
    loan = result(data)
    assert loan.expanded_spec.simulated_credit_route == expected
    if expected == "SIMULATED_DECLINE_CANDIDATE":
        assert set(loan.expanded_spec.candidate_reason_codes) & {"KO-03", "KO-04"}


@pytest.mark.parametrize("change,expected", [
    ({"credit_freeze": "Y"}, "MANUAL_REVIEW"),
    ({"ofac_alert": "Y"}, "COMPLIANCE_REVIEW"),
    ({"report_date": None}, "DATA_REVIEW"),
    ({"bankruptcy_count": "1", "bankruptcy_discharge_date": "2022-10-20"}, "SIMULATED_DECLINE_CANDIDATE"),
])
def test_stop_conditions_and_bankruptcy(change, expected):
    data = source()
    data[2][1].update(change)
    assert result(data).expanded_spec.simulated_credit_route == expected


def test_no_borrower_junction_or_score_timestamp_never_candidate():
    assert result(source()[:-1]).expanded_spec.simulated_credit_route == "DATA_REVIEW"
    data = source()
    del data[3][1]["score_date"]
    assert result(data).expanded_spec.simulated_credit_route == "DATA_REVIEW"


def test_nonprofit_and_baloc_conflicts_and_vehicle_exception():
    data = source()
    data[0][1]["is_nfp"] = True
    assert result(data).expanded_spec.simulated_credit_route == "FULL_UNDERWRITING"
    data = source()
    data[1][1].update(is_increase=True, account_opened="2026-02-20")
    assert result(data).expanded_spec.simulated_credit_route == "POLICY_REVIEW"
    data = source()
    data[0][1]["ownership_years"] = "1"
    data[3][1]["sbss"] = "175"
    data[2][1]["personal_fico"] = "700"
    data[1][1]["requested_amount"] = "100000001"
    assert result(data).expanded_spec.simulated_credit_route == "POLICY_REVIEW"
    data = source()
    data[3][1]["sbss"] = "195"
    data[1][1]["vehicle_only"] = True
    assert result(data).expanded_spec.simulated_credit_route == "POLICY_REVIEW"


def test_guarantor_low_score_requires_aggregation_review_not_borrower_substitution():
    data = source() + [
        ("Account", {"Id": "G1"}),
        ("EntityInvolvement", {"Id": "JG", "loan_id": "L1", "account_id": "G1", "role": "guarantor"}),
        ("CreditReport", {"Id": "GR", "account_id": "G1", "status": "completed", "report_date": "2026-10-19", "personal_fico": "600"}),
    ]
    loan = result(data)
    assert loan.normalized_data["fico"] == "760"
    assert loan.expanded_spec.simulated_credit_route == "POLICY_REVIEW"
    assert "approved_personal_fico_aggregation" in loan.expanded_spec.missing_inputs


@pytest.mark.parametrize("code", [item[0] for item in REASONS])
def test_every_reason_has_evidence_or_explicit_unavailable_disposition(code):
    loan = result()
    item = next(item for item in loan.expanded_spec.decline_reasons if item.code == code)
    assert item.detail and 7 <= item.page <= 9
    assert item.status in {"PASS", "FAIL", "REVIEW", "NOT_EVALUATED"}
    assert set(item.evidence_refs) <= {e.id for e in loan.source_evidence}
    if item.disposition in {"NOT_USED", "OUT_OF_SCOPE"}:
        assert item.status == "NOT_EVALUATED"
    if item.status == "REVIEW":
        assert item.required_inputs


def test_financial_review_inputs_and_no_ebit_substitution():
    data = source()
    data[0][1].update(current_assets="100.00", current_liabilities="101.00", business_formation_date="2025-10-21")
    data[2][1].update(account_count="2", thin_file_indicator=False)
    loan = result(data)
    reasons = {item.code: item for item in loan.expanded_spec.decline_reasons}
    for key in ("WORKING_CAPITAL", "LIMITED_CREDIT", "OPERATING_HISTORY"):
        assert reasons[key].status == "FAIL"
        assert reasons[key].evidence_refs
    assert loan.expanded_spec.simulated_credit_route == "MANUAL_REVIEW"
    data = source()
    del data[0][1]["ebitda"]
    data[0][1]["SBSS7__earn_before_int_and_taxes__c"] = "100"
    loan = result(data)
    item = next(item for item in loan.expanded_spec.decline_reasons if item.code == "EXPENSES_EXCEED_INCOME")
    assert item.status == "REVIEW" and "never substituted" in item.detail


@pytest.mark.parametrize("product,fields,expected", [
    ("bil", {}, "REVIEW"),
    ("bil", {"acceptance_letter_present": True}, "PASS"),
    ("sba_bil", {}, "REVIEW"),
    ("sba_bil", {"loan_purpose": "permanent_working_capital", "sba_memo_present": True, "cam_complete": True}, "PASS"),
    ("term_loan", {}, "REVIEW"),
    ("term_loan", {"fixed_rate_confirmed": True, "repayment_terms_confirmed": True}, "PASS"),
    ("conventional_baloc", {"existing_product": "sba_baloc", "new_line": False}, "REVIEW"),
    ("conventional_baloc", {"reducing_secured_line": True, "collateral_retained": False}, "REVIEW"),
])
def test_product_documents_and_conversion_diagnostics(product, fields, expected):
    data = source()
    data[1][1].update(product=product, **fields)
    spec = result(data).expanded_spec
    check = next(item for item in spec.rule_results if item.rule_id == "EL-02")
    assert check.status == expected


def test_missing_expanded_source_cannot_assert_rule_or_reason_results():
    loan = result(policy_config={"expanded_spec_source_sha256": "0" * 64})
    spec = loan.expanded_spec
    assert not spec.source_available
    assert spec.simulated_credit_route == "POLICY_REVIEW"
    assert all(item.status == "NOT_EVALUATED" for item in spec.rule_results + spec.decline_reasons)
    assert spec.candidate_reason_codes == []


def test_authorization_and_adverse_action_codes_cannot_be_injected():
    spec = result().expanded_spec
    with pytest.raises(ValidationError):
        spec.authorized_decision = "APPROVED"
    with pytest.raises(ValidationError):
        spec.decline_reasons[0].adverse_action_code = "LEGAL_NOTICE_CODE"


def test_scoped_contradictory_indicators_hold_without_silent_selection():
    data = source() + [
        ("LCREntity", {"Id": "E1", "account_id": "A1", "response_id": "S1", "entity_involvement_id": "J1"}),
        ("DataSourceAnalysis", {"Id": "D1", "entity_id": "E1"}),
        ("Indicator", {"Id": "I1", "analysis_id": "D1", "key": "thinFileIndicator", "value": "Y"}),
        ("Indicator", {"Id": "I2", "analysis_id": "D1", "key": "thinFileIndicator", "value": "N"}),
    ]
    spec = result(data).expanded_spec
    assert spec.simulated_credit_route == "DATA_REVIEW"
    assert "CONFLICTING_SCOPED_OBSERVATIONS" in spec.policy_conflicts


def test_financial_yoy_requires_explicit_comparable_periods():
    data = source()
    data[0][1].update(prior_revenue="1000", current_revenue="900", prior_expenses="400",
        current_expenses="500", prior_profit="600", current_profit="400",
        prior_period_end="2024-12-31", current_period_end="2025-12-31")
    spec = result(data).expanded_spec
    assert next(item for item in spec.decline_reasons if item.code == "FINANCIAL_CONDITION").status == "FAIL"
    data[0][1]["current_period_end"] = "2025-06-30"
    spec = result(data).expanded_spec
    assert next(item for item in spec.decline_reasons if item.code == "FINANCIAL_CONDITION").status == "REVIEW"


def test_underage_is_eligibility_review_not_operational_denial():
    data = source()
    data[0][1]["dob"] = "2010-01-01"
    spec = result(data).expanded_spec
    assert spec.simulated_credit_route == spec.eligibility_route == "DATA_REVIEW"


def test_direct_and_scoped_thin_file_disagreement_is_a_data_conflict():
    data = source() + [
        ("LCREntity", {"Id": "E1", "account_id": "A1", "response_id": "S1"}),
        ("DataSourceAnalysis", {"Id": "D1", "entity_id": "E1"}),
        ("Indicator", {"Id": "I1", "analysis_id": "D1", "key": "thinFileIndicator", "value": "Y"}),
    ]
    data[2][1]["thin_file_indicator"] = False
    assert result(data).expanded_spec.simulated_credit_route == "DATA_REVIEW"


@pytest.mark.parametrize("field,value", [("current_assets", 100.5), ("current_liabilities", True), ("prior_revenue", "=100+1"), ("current_profit", "1e5")])
def test_new_financial_fields_keep_exact_decimal_guards(field, value):
    data = source()
    data[0][1][field] = value
    loan = result(data)
    assert any("INVALID_USD_AMOUNT" in item for item in loan.conflicts)
    assert loan.expanded_spec.simulated_credit_route == "DATA_REVIEW"


def test_missing_earnings_period_cannot_yield_expanded_approval_candidate():
    data = source()
    del data[0][1]["financial_period_start"]
    spec = result(data).expanded_spec
    assert spec.simulated_credit_route == "DATA_REVIEW"
    assert "verified_ebitda_period" in spec.missing_inputs


def test_every_reason_fails_closed_when_expanded_source_unverified():
    data = source()
    data[2][1]["personal_fico"] = "600"
    data[0][1]["ebitda"] = "-100.00"
    spec = result(data, policy_config={"expanded_spec_source_sha256": "invalid"}).expanded_spec
    assert all(item.status == "NOT_EVALUATED" for item in spec.rule_results + spec.decline_reasons)
    assert spec.candidate_reason_codes == []