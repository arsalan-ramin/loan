"""Verified v4 draft scenarios. No production credit authorization is assumed."""

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from backend.tests.test_underwriting import document, evaluate, observations, rows, rule, synthetic_policy
from backend.underwriting_engine import evaluate_underwriting, load_underwriting_config
from backend.underwriting_models import AMOUNT_NAMES, LoanRecommendation, RecommendationPath, SOURCE_DOCUMENT, SOURCE_SHA256


def test_actual_v4_source_integrity_and_defaults():
    source = Path(__file__).parents[1] / "data" / SOURCE_DOCUMENT
    assert source.is_file(), "Acceptance requires the actual supplied v4 PDF, not a mocked availability claim"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA256
    config = load_underwriting_config()
    assert config["source_sha256"] == SOURCE_SHA256
    assert config["policy_version"] == "v4-USD-verified-draft-2"
    assert config["minimum_age"] == 18
    assert config["high_risk_naics"] == ["23", "4451", "45", "8111", "8121", "8123"]
    batch = evaluate_underwriting([document(rows())])
    assert batch.source_available
    assert rule(batch.loan_results[0], "POLICY_SOURCE_V4").status == "PASS"


@pytest.mark.parametrize("digest", [None, "", "0" * 64, "f" * 64])
def test_unverified_hash_disables_all_source_dependent_candidate_rules(digest):
    result = evaluate(config={"source_sha256": digest})
    assert rule(result, "POLICY_SOURCE_V4").status == "UNKNOWN"
    assert all(item.status in {"UNKNOWN", "NOT_APPLICABLE"} for item in result.rule_results if item.page)
    assert rule(result, "MINIMUM_AGE").status == "UNKNOWN"


def test_missing_file_cannot_be_asserted_available(monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    batch = evaluate_underwriting([document(rows())], policy_config={"source_available": True})
    assert batch.source_available is False


def test_actual_file_hash_not_config_assertion_controls_availability(monkeypatch):
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"substituted source")
    batch = evaluate_underwriting([document(rows())])
    assert not batch.source_available


@pytest.mark.parametrize("amount,status", [("24999999.99", "PASS"), ("25000000.00", "PASS"), ("25000000.01", "FAIL")])
@pytest.mark.parametrize("flag", ["is_startup", "is_investment_re"])
def test_baloc_25m_boundary_and_citation(amount, status, flag):
    source = rows()
    source[1][1].update(requested_amount=amount, **{flag: True})
    result = evaluate(source)
    candidate = rule(result, "BALOC_SPECIAL_25M_CANDIDATE")
    assert candidate.status == status
    assert candidate.page == 2
    assert candidate.policy_reference == f"{SOURCE_DOCUMENT} p.2"
    assert candidate.evidence_refs
    assert result.final_action == "NO_OPERATIONAL_ACTION"


@pytest.mark.parametrize("tier,status", [("249999999.99", "PASS"), ("250000000.00", "PASS"), ("250000000.01", "FAIL")])
def test_tier_i_250m_boundary_not_250k(tier, status):
    source = rows()
    source[1][1]["tier_i"] = tier
    assert rule(evaluate(source), "TIER_I_250M_CANDIDATE").status == status


def test_total_group_max_is_not_falsely_subject_to_tier_i_cap():
    source = rows()
    source[1][1].update(tier_i="1", tier_ii="250000000.01")
    result = evaluate(source)
    assert result.normalized_amounts_usd["total_group_credit_usd"] == Decimal("250000000.01")
    assert rule(result, "TIER_I_250M_CANDIDATE").status == "PASS"
    assert rule(result, "GROUP_EXPOSURE_POLICY").status == "UNKNOWN"


@pytest.mark.parametrize("amount,le100k,le100m", [("100000.00", True, True), ("100000.01", False, True), ("100000000.00", False, True), ("100000000.01", False, False)])
def test_disputed_request_thresholds_remain_separate(amount, le100k, le100m):
    source = rows()
    source[1][1]["requested_amount"] = amount
    result = evaluate(source, mode="PRODUCTION")
    assert result.normalized_data["requested_amount_le_100k"] is le100k
    assert result.normalized_data["requested_amount_le_100m"] is le100m
    assert "REQUEST_THRESHOLD_100M_VS_100K" in result.conflicts
    assert "INCREASE_WAIT_6_VS_12_MONTHS" in result.conflicts
    assert result.final_action == "MANUAL_REVIEW"


@pytest.mark.parametrize("naics", ["23", "236220", "445110", "451110", "811111", "812112", "812310"])
def test_high_risk_naics_are_prefix_candidates(naics):
    source = rows()
    source[0][1]["naics"] = naics
    result = evaluate(source)
    assert result.normalized_data["naics_not_high_risk"] is False
    assert rule(result, "HIGH_RISK_NAICS").status == "FAIL"


@pytest.mark.parametrize("naics", [None, 236220, "23.0", "", "=23", "2362200"])
def test_naics_ambiguous_input_is_unknown(naics):
    source = rows()
    source[0][1]["naics"] = naics
    assert rule(evaluate(source), "HIGH_RISK_NAICS").status == "UNKNOWN"


@pytest.mark.parametrize("category,role,expected", [(None, None, "UNKNOWN"), ("business", "borrower", "UNKNOWN"), ("personal", None, "UNKNOWN"), ("personal", "guarantor", "UNKNOWN"), ("personal", "borrower", "FAIL"), ("consumer", "borrower", "FAIL")])
def test_untyped_or_business_score_is_not_personal_fico(category, role, expected):
    source = rows()
    source[2][1].pop("personal_fico")
    source[2][1].update(fico="649", report_category=category, report_role=role)
    result = evaluate(source)
    assert rule(result, "PERSONAL_FICO_650_CANDIDATE").status == expected
    if expected == "UNKNOWN":
        assert result.normalized_data["fico"] is None


@pytest.mark.parametrize("score,status", [("649", "FAIL"), ("650", "PASS"), ("759", "PASS"), ("760", "PASS")])
def test_explicit_personal_fico_candidate_boundaries(score, status):
    source = rows()
    source[2][1]["personal_fico"] = score
    assert rule(evaluate(source), "PERSONAL_FICO_650_CANDIDATE").status == status


@pytest.mark.parametrize("code,status", [("0", "PASS"), ("1", "FAIL"), ("2", "FAIL"), ("3", "FAIL")])
def test_screenshot_public_record_codes_match_knockout_candidate(code, status):
    source = rows()
    source[0][1].update(identity_verified=True, income_verified=True, ownership_years="1", naics="541100")
    source[1][1].update(requested_amount="100000", new_money="100000", tier_i="250000",
                        tier_ii="250000", secured=False, product="express_baloc", requested_path="quick_app")
    source[2][1].update(report_category="personal", report_role="borrower", report_date="2026-10-19",
                        ofac_disposition="cleared", ofac_alert=False, credit_freeze=False,
                        bankruptcy_count="0")
    source[3][1]["sbss"] = "175"
    source[3][1]["score_date"] = "2026-10-19"
    source[2][1]["max_delinquency_public_records"] = code

    result = evaluate(source, mode="PRODUCTION")
    diagnostic = next(item for item in result.expanded_spec.rule_results if item.rule_id == "KO-07")
    v4_check = rule(result, "MAX_DELINQUENCY_PUBLIC_RECORDS_KNOCKOUT_CANDIDATE")

    assert diagnostic.status == status
    assert v4_check.status == status
    assert result.expanded_spec.simulated_credit_route == (
        "SIMULATED_DECLINE_CANDIDATE" if code in {"1", "2", "3"} else "SBA_APPROVAL_CANDIDATE"
    )
    assert result.final_action == "MANUAL_REVIEW"
    assert result.recommendation_path == "POLICY_REVIEW_REQUIRED"


@pytest.mark.parametrize("score,path,status", [("159", "KNOCKOUT_CANDIDATE", "FAIL"), ("160", "UNSPECIFIED_OWNERSHIP_FICO_AMOUNT", "PASS"), ("189", "UNSPECIFIED_OWNERSHIP_FICO_AMOUNT", "PASS"), ("190", "SBA_CANDIDATE", "PASS"), ("199", "SBA_CANDIDATE", "PASS"), ("200", "OWNERSHIP_NAICS_CANDIDATE", "PASS")])
def test_sbss_candidate_routes_are_not_approvals(score, path, status):
    source = rows()
    source[3][1]["sbss"] = score
    result = evaluate(source)
    assert result.normalized_data["sbss_candidate_path"] == path
    assert rule(result, "SBSS_160_CANDIDATE").status == status
    assert result.recommendation_path == "SIMULATED_REVIEW"
    if score in {"160", "189"}:
        assert rule(result, "SBSS_160_189_BRANCH").status == "UNKNOWN"


@pytest.mark.parametrize("table,field,value,rule_id", [
    ("Account", "ebitda", "-0.01", "EBITDA_NONNEGATIVE_DIAGNOSTIC"),
    ("Account", "dob", "2008-10-21", "MINIMUM_AGE"),
    ("CreditReport", "personal_fico", "649", "PERSONAL_FICO_650_CANDIDATE"),
    ("LCRESPONSE", "sbss", "159", "SBSS_160_CANDIDATE"),
])
def test_knockouts_are_deterministic_candidates_with_manual_production(table, field, value, rule_id):
    source = rows()
    next(fields for name, fields in source if name == table)[field] = value
    result = evaluate(source, mode="PRODUCTION", config=synthetic_policy())
    assert rule(result, rule_id).status == "FAIL"
    assert rule(result, rule_id).page == 2
    assert result.final_action == "MANUAL_REVIEW"
    assert result.recommendation_path == "POLICY_REVIEW_REQUIRED"


MAPPED_MONEY = [
    ("Loan", "LLC_BI_Amount__c", "loan_requested_amount_usd"),
    ("Account", "Estimated_Annual_Revenue__c", "estimated_annual_revenue_usd"),
    ("Account", "Business_Net_Income__c", "business_net_income_usd"),
    ("Account", "SBSS7_cash_and_equiv__c", "cash_and_equiv_usd"),
    ("Account", "SBSS7_net_worth__c", "net_worth_usd"),
    ("Account", "SBSS7_total_assets__c", "total_assets_usd"),
    ("Account", "SBSS7_total_liabilities__c", "total_liabilities_usd"),
    ("CreditReport", "nFUSE_Total_Past_Due__c", "total_past_due_usd"),
    ("CreditReport", "nFUSE_Available_Credit__c", "available_credit_usd"),
    ("CreditReport", "nFUSE_Bankruptcy_Amount__c", "bankruptcy_amount_usd"),
]


@pytest.mark.parametrize("table,field,normalized", MAPPED_MONEY)
def test_verified_api_money_mapping(table, field, normalized):
    source = rows()
    fields = next(fields for name, fields in source if name == table)
    # Remove a legacy alias when testing the source field independently.
    old = next(key for key, target in AMOUNT_NAMES.items() if target == normalized)
    fields.pop(old, None)
    fields[field] = "123456.78"
    result = evaluate(source)
    assert result.normalized_amounts_usd[normalized] == Decimal("123456.78")
    assert all(key.endswith("_usd") for key in result.normalized_amounts_usd)
    assert any(item.field == field and item.value == "123456.78" for item in result.source_evidence)


@pytest.mark.parametrize("table,field,normalized", MAPPED_MONEY)
def test_every_mapped_money_rejects_binary_float(table, field, normalized):
    source = rows()
    fields = next(fields for name, fields in source if name == table)
    old = next(key for key, target in AMOUNT_NAMES.items() if target == normalized)
    fields.pop(old, None)
    fields[field] = 123456.78
    result = evaluate(source, mode="PRODUCTION")
    assert result.normalized_amounts_usd[normalized] is None
    assert result.recommendation_path == "DATA_QUALITY_HOLD"


def test_group_credit_source_requires_explicit_tier_i_scope_manifest():
    source = rows()
    source[1][1].pop("tier_i")
    source[1][1]["Group_Credit_Amount__c"] = "250000000.00"
    assert evaluate(source).normalized_amounts_usd["tier1_group_credit_usd"] is None
    result = evaluate(source, config={"mapping_manifest": {"group_credit_tier1_scope_confirmed": True}})
    assert result.normalized_amounts_usd["tier1_group_credit_usd"] == Decimal("250000000.00")


def test_explicit_usd_amounts_stay_distinct_and_total_is_max():
    source = rows()
    fields = source[1][1]
    for old in ("requested_amount", "new_money", "tier_i", "tier_ii"):
        fields.pop(old)
    fields.update(loan_requested_amount_usd="1.01", new_money_usd="2.02", tier1_group_credit_usd="3.03", tier2_group_credit_usd="4.04", credit_limit_usd="5.05")
    result = evaluate(source)
    assert [result.normalized_amounts_usd[key] for key in ("loan_requested_amount_usd", "new_money_usd", "tier1_group_credit_usd", "tier2_group_credit_usd", "total_group_credit_usd", "credit_limit_usd")] == list(map(Decimal, ("1.01", "2.02", "3.03", "4.04", "4.04", "5.05")))


def test_ebit_and_net_income_do_not_silently_derive_ebitda():
    source = rows()
    source[0][1].pop("ebitda")
    source[0][1].update(EBIT__c="100000", Business_Net_Income__c="100000")
    result = evaluate(source)
    assert result.normalized_amounts_usd["ebitda_usd"] is None
    assert rule(result, "EBITDA_NONNEGATIVE_DIAGNOSTIC").status == "UNKNOWN"


@pytest.mark.parametrize("target", ["personal_fico", "fico", "sbss", "loan_requested_amount_usd", "ebitda_usd"])
def test_indicator_pivots_cannot_forge_scores_or_money(target):
    config = {"mapping_manifest": {"indicator_feature_mapping": {"OWNERSHIP": target}}}
    result = evaluate(rows() + observations(), config=config, mode="PRODUCTION")
    assert "UNSUPPORTED_INDICATOR_TARGET" in result.conflicts
    assert result.recommendation_path == "DATA_QUALITY_HOLD"


def test_exact_test_enum_contract_and_production_disposition_validation():
    assert {value for value in get_args(RecommendationPath) if value.startswith("SIMULATED_")} == {"SIMULATED_APPROVE_PATH", "SIMULATED_DECLINE_PATH", "SIMULATED_REVIEW"}
    payload = evaluate().model_dump()
    for bad in ("SIMULATED_APPROVE", "SIMULATED_DECLINE", "DATA_QUALITY_HOLD", "POLICY_REVIEW_REQUIRED"):
        with pytest.raises(ValidationError):
            LoanRecommendation.model_validate({**payload, "recommendation_path": bad})
    prod = evaluate(mode="PRODUCTION").model_dump()
    with pytest.raises(ValidationError):
        LoanRecommendation.model_validate({**prod, "final_action": "NO_OPERATIONAL_ACTION"})


def test_usd_key_validation_rejects_legacy_public_money_names():
    payload = evaluate().model_dump()
    with pytest.raises(ValidationError):
        LoanRecommendation.model_validate({**payload, "normalized_amounts_usd": {"requested_amount": "1.00"}})