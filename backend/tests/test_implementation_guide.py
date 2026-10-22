"""Implementation-guide integration: verified source and diagnostic-only routing."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.entity_graph import build_entity_graph
from backend.tests.test_underwriting import document, evaluate, rows
from backend.underwriting_engine import (
    evaluate_implementation_strategy,
    evaluate_underwriting,
    load_underwriting_config,
)
from backend.underwriting_models import (
    IMPLEMENTATION_GUIDE_DOCUMENT,
    IMPLEMENTATION_GUIDE_SHA256,
    LoanRecommendation,
)


def strategy(**overrides):
    values = {
        "identity_valid": True,
        "quick_app_eligible": True,
        "knockouts_completed": True,
        "knockout_detected": False,
        "sbss": "200",
        "fico": "760",
        "ownership_years": "2",
        "requested_amount": "100000",
        "naics": "541100",
        "vehicle_only": False,
        "product_review_required": False,
        "compliance_review_required": False,
        "manual_review_required": False,
        "source_available": True,
        "policy_conflicts": (),
        "high_risk_naics": ("23", "4451", "45", "8111", "8121", "8123"),
    }
    values.update(overrides)
    return evaluate_implementation_strategy(**values)


def test_implementation_guide_is_separately_pinned_and_readable():
    source = Path(__file__).parents[1] / "data" / IMPLEMENTATION_GUIDE_DOCUMENT
    assert source.is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == IMPLEMENTATION_GUIDE_SHA256
    from pypdf import PdfReader

    reader = PdfReader(str(source))
    assert len(reader.pages) == 7
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "8. Quick App decision tree" in text
    assert "Stage 0" in text and "Stage 6" in text
    assert "NOT_EVALUATED" in text
    config = load_underwriting_config()
    assert config["implementation_guide_source_document"] == IMPLEMENTATION_GUIDE_DOCUMENT
    assert config["implementation_guide_source_sha256"] == IMPLEMENTATION_GUIDE_SHA256


def test_batch_separates_guide_source_and_never_authorizes_or_unlocks_recommendation():
    batch = evaluate_underwriting([document(rows())], mode="PRODUCTION")
    result = batch.loan_results[0]
    assert batch.source_available is True
    assert batch.implementation_guide_available is True
    assert batch.implementation_guide_source_sha256 == IMPLEMENTATION_GUIDE_SHA256
    assert result.eligibility_route == "QUICK_APP"
    assert result.credit_assessment == "SIMULATED_APPROVAL_CANDIDATE"
    assert result.authorization == "NOT_ISSUED"
    assert result.recommendation_path == "POLICY_REVIEW_REQUIRED"
    assert result.final_action == "MANUAL_REVIEW"
    audit = result.normalized_data["implementation_guide_diagnostics"]["audit"]
    assert audit["source_available"] is True
    assert len(audit["input_fingerprint_sha256"]) == 64
    stages = result.normalized_data["implementation_guide_diagnostics"]["stages"]
    assert stages["stage_0_schema_identity"] == "PASS"
    assert stages["stage_1_policy_approval_and_mode"] == "DRAFT_UNAPPROVED_SIMULATION_ONLY"
    assert stages["stage_4_credit_strategy"] == "CANDIDATE_MATCH"
    assert stages["stage_5_non_operational_route"] == "SIMULATED_APPROVAL_CANDIDATE"
    assert result.normalized_data["out_of_scope_diagnostics"]
    assert set(result.normalized_data["out_of_scope_diagnostics"].values()) == {"NOT_EVALUATED"}
    assert all("NOT_EVALUATED" != rule.status for rule in result.rule_results)


def test_unavailable_or_unpinned_guide_means_policy_review_not_tree_assertion():
    batch = evaluate_underwriting(
        [document(rows())],
        policy_config={"implementation_guide_source_sha256": "0" * 64},
    )
    result = batch.loan_results[0]
    assert batch.implementation_guide_available is False
    assert result.eligibility_route == "POLICY_REVIEW"
    assert result.credit_assessment == "POLICY_REVIEW"
    assert result.strategy_branch is None
    assert result.normalized_data["implementation_guide_diagnostics"]["stages"]["source"] == "UNKNOWN"


@pytest.mark.parametrize(
    "sbss,fico,ownership,amount,naics,vehicle,branch,assessment",
    [
        ("159", "760", "3", "50000", "541100", False, "SBSS_LT_160", "SIMULATED_DECLINE_CANDIDATE"),
        ("160", "649", "2", "50000", "541100", False, "SBSS_160_189_OWNERSHIP_GE_2", "SIMULATED_APPROVAL_CANDIDATE"),
        ("175", "760", "1", "50000", "541100", False, "SBSS_160_189_OWNERSHIP_LT_2_FICO_GE_760", "SIMULATED_APPROVAL_CANDIDATE"),
        ("175", "700", "1", "100000", "541100", False, "SBSS_160_189_OWNERSHIP_LT_2_FICO_650_759_AMOUNT_LE_100M", "SIMULATED_APPROVAL_CANDIDATE"),
        ("190", "600", "0", "50000", "541100", False, "SBSS_190_199", "SIMULATED_APPROVAL_CANDIDATE"),
        ("200", "600", "1", "50000", "541100", False, "SBSS_GE_200_OWNERSHIP_LT_2", "SIMULATED_APPROVAL_CANDIDATE"),
        ("200", "600", "2", "50000", "541100", False, "SBSS_GE_200_OWNERSHIP_GE_2_OTHER_NAICS", "SIMULATED_APPROVAL_CANDIDATE"),
        ("200", "600", "2", "50000", "236220", False, "SBSS_GE_200_OWNERSHIP_GE_2_HIGH_RISK_NAICS", "SIMULATED_APPROVAL_CANDIDATE"),
    ],
)
def test_section8_explicit_decision_tree_branches(sbss, fico, ownership, amount, naics, vehicle, branch, assessment):
    result = strategy(
        sbss=sbss, fico=fico, ownership_years=ownership, requested_amount=amount,
        naics=naics, vehicle_only=vehicle,
    )
    assert result["strategy_branch"] == branch
    assert result["credit_assessment"] == assessment
    assert result["authorization"] == "NOT_ISSUED"


def test_request_threshold_conflict_and_vehicle_override_unknown_fail_closed():
    affected = strategy(sbss="175", fico="700", ownership_years="1", requested_amount="100000.01")
    assert affected["credit_assessment"] == "POLICY_REVIEW"
    assert affected["eligibility_route"] == "POLICY_REVIEW"
    assert "REQUEST_THRESHOLD_100M_VS_100K" in affected["unresolved_conflicts"]
    vehicle = strategy(sbss="200", ownership_years="2", naics="236220", vehicle_only=None)
    assert vehicle["credit_assessment"] == "POLICY_REVIEW"
    assert vehicle["strategy_branch"] is None
    vehicle = strategy(sbss="200", ownership_years="2", naics="236220", vehicle_only=True)
    assert vehicle["credit_assessment"] == "POLICY_REVIEW"
    assert vehicle["strategy_branch"] is None


def test_unresolved_baloc_increase_window_routes_to_product_review():
    source = rows()
    source[1][1].update(is_increase="Y", account_opened="2026-04-20")
    result = evaluate(source)
    assert "INCREASE_WAIT_6_VS_12_MONTHS" in result.conflicts
    assert result.eligibility_route == "PRODUCT_REVIEW"
    assert result.credit_assessment == "MANUAL_REVIEW"


def test_strategy_requires_identity_eligibility_and_completed_knockouts():
    assert strategy(identity_valid=None)["eligibility_route"] == "DATA_REVIEW"
    assert strategy(quick_app_eligible=None)["credit_assessment"] == "DATA_REVIEW"
    assert strategy(quick_app_eligible=False)["eligibility_route"] == "FULL_UNDERWRITING"
    assert strategy(knockouts_completed=False)["credit_assessment"] == "DATA_REVIEW"
    assert strategy(knockout_detected=True)["credit_assessment"] == "SIMULATED_DECLINE_CANDIDATE"
    assert strategy(fico=None)["credit_assessment"] == "DATA_REVIEW"
    assert strategy(product_review_required=True)["eligibility_route"] == "PRODUCT_REVIEW"
    assert strategy(compliance_review_required=True)["credit_assessment"] == "COMPLIANCE_REVIEW"


def test_nonprofit_y_n_flags_and_implementation_outputs_are_separate():
    source = rows()
    source[0][1]["is_nfp"] = "Y"
    source[2][1]["credit_freeze"] = "N"
    result = evaluate(source)
    assert result.normalized_data["quick_app_nfp_eligible"] is False
    assert result.eligibility_route == "FULL_UNDERWRITING"
    assert result.authorization == "NOT_ISSUED"

    source = rows()
    source[2][1]["credit_freeze"] = "Y"
    frozen = evaluate(source)
    assert frozen.normalized_data["credit_freeze_clear"] is False
    assert frozen.credit_assessment == "MANUAL_REVIEW"


def test_model_authorization_is_fixed_not_issued():
    payload = evaluate().model_dump()
    payload["authorization"] = "APPROVED"
    with pytest.raises(ValidationError):
        LoanRecommendation.model_validate(payload)


def test_lcr_entity_involvement_foreign_key_and_regulation_are_historical_only():
    source = rows() + [
        ("EntityInvolvement", {"Id": "J1", "loan_id": "L1", "account_id": "A1", "role": "borrower"}),
        ("LCREntity", {"Id": "E1", "account_id": "A1", "response_id": "S1", "entity_involvement_id": "J1"}),
        ("Regulation", {"Id": "RG1", "loan_id": "L1"}),
    ]
    graph = build_entity_graph([document(source)], load_underwriting_config())
    assert graph.table("LCREntity")["E1"].fields["entity_involvement_id"] == "J1"
    assert graph.table("Regulation")["RG1"].fields["loan_id"] == "L1"
    assert not any("ORPHAN_RELATION" in issue for issue in graph.issues)
    result = evaluate(source)
    baseline = evaluate(rows())
    assert result.authorization == "NOT_ISSUED"
    assert result.recommendation_path == "SIMULATED_REVIEW"
    assert (result.normalized_data["implementation_guide_diagnostics"]["strategy_branch"]
            == baseline.normalized_data["implementation_guide_diagnostics"]["strategy_branch"])


def test_orphan_lcr_entity_involvement_is_reported():
    source = rows() + [
        ("LCREntity", {"Id": "E1", "account_id": "A1", "response_id": "S1", "entity_involvement_id": "MISSING"}),
    ]
    graph = build_entity_graph([document(source)], load_underwriting_config())
    assert any("ORPHAN_RELATION:LCREntity:entity_involvement_id" in issue for issue in graph.issues)


def test_guide_canonical_regulation_sheet_alias_is_explicitly_manifest_declared():
    source = rows() + [("MTB_REGULATION__C", {"regulation_id": "RG1", "loan_id": "L1"})]
    graph = build_entity_graph([document(source)], load_underwriting_config())
    assert "RG1" in graph.table("Regulation")
