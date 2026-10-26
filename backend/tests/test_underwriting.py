"""Synthetic fixtures, NOT approved credit policy or fabricated customer data.

The TEST policy below is deliberately injected per call. It resolves both known
conflicts solely for simulation and never mutates the checked-in defaults.
"""

import copy
import json
import logging
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.entity_graph import build_entity_graph
from backend.underwriting_engine import (
    calendar_anniversary, evaluate_underwriting, fico_band,
    load_underwriting_config, sbss_bucket,
)
from backend.underwriting_models import (
    SOURCE_DOCUMENT, USD_25K, USD_100K, USD_250K, USD_25M, USD_100M, USD_250M,
    LoanRecommendation, SourceEvidence, UnderwritingBatch, exact_decimal,
)


def document(rows, name="canonical.xlsx", document_id="fixture"):
    records = [{"sheet": table, "row": index + 2, "values": values,
                "sources": {key: f"{table}!{index + 2}:{key}" for key in values}}
               for index, (table, values) in enumerate(rows)]
    return SimpleNamespace(id=document_id, name=name, metadata={"records": records})


def rows():
    return [
        ("Account", {"Id": "A1", "dob": "1980-10-20", "is_nfp": False,
                     "ownership_years": "2", "naics": "541100", "ebitda": "0.00"}),
        ("Loan", {"Id": "L1", "borrower_account_id": "A1", "as_of_date": "2026-10-20",
                  "product": "express_baloc", "requested_path": "quick_app",
                  "requested_amount": "100000.00", "new_money": "100000", "tier_i": "250000",
                  "tier_ii": "249999.99", "secured": False, "is_startup": False,
                  "is_investment_re": False, "is_increase": False, "vehicle_only": False}),
        ("CreditReport", {"Id": "R1", "account_id": "A1", "status": "completed", "report_date": "2026-10-19",
                          "personal_fico": "760", "report_role": "borrower", "ofac_disposition": "cleared", "ofac_alert": False,
                          "credit_freeze": False, "bankruptcy_count": "0", "past_due": "0",
                          "max_delinquency_public_records": "0"}),
        ("LCRESPONSE", {"Id": "S1", "loan_id": "L1", "sbss": "200"}),
    ]


def synthetic_policy():
    return {
        "minimum_age": 18,
        "high_risk_naics": ["999999"],
        "test_policy": {
            "synthetic": True, "approved": True, "label": "SYNTHETIC UNIT TEST ONLY",
            "resolved_conflicts": ["REQUEST_THRESHOLD_100M_VS_100K", "INCREASE_WAIT_6_VS_12_MONTHS"],
            "criteria": [
                {"rule_id": "TEST-SBSS", "feature": "sbss", "op": "ge", "value": "200"},
                {"rule_id": "TEST-FICO", "feature": "fico", "op": "ge", "value": "760"},
                {"rule_id": "TEST-AMOUNT", "feature": "requested_amount", "op": "le", "value": "100000"},
                {"rule_id": "TEST-OWNERSHIP", "feature": "ownership_at_least_2_years", "op": "eq", "value": True},
                {"rule_id": "TEST-EBITDA", "feature": "ebitda_nonnegative", "op": "eq", "value": True},
            ],
        },
    }


def evaluate(source=None, mode="TEST", config=None):
    return evaluate_underwriting([document(rows() if source is None else source)], mode,
                                 policy_config=config).loan_results[0]


def rule(result, name):
    return next(item for item in result.rule_results if item.rule_id == name)


def test_default_production_contract_and_missing_source():
    batch = evaluate_underwriting([document(rows())], policy_config={"source_sha256": "0" * 64})
    assert isinstance(batch, UnderwritingBatch)
    assert batch.source_document == SOURCE_DOCUMENT
    assert batch.source_available is False
    result = batch.loan_results[0]
    assert result.recommendation_path == "POLICY_REVIEW_REQUIRED"
    assert result.final_action == "MANUAL_REVIEW"
    assert result.human_review_required is True
    assert set(result.conflicts) == {"REQUEST_THRESHOLD_100M_VS_100K", "INCREASE_WAIT_6_VS_12_MONTHS"}
    assert result.normalized_amounts_usd["loan_requested_amount_usd"] == Decimal("100000.00")
    assert json.loads(batch.model_dump_json())["loan_results"][0]["normalized_amounts_usd"]["loan_requested_amount_usd"] == "100000.00"
    assert rule(result, "SBSS_FICO_BRANCH_POLICY").status == "UNKNOWN"


@pytest.mark.parametrize("mode", ["test", "production", "APPROVE", "DENY", "", None])
def test_modes_fail_closed(mode):
    with pytest.raises(ValueError):
        evaluate_underwriting([], mode)


@pytest.mark.parametrize("key,value", [
    ("final_action", "APPROVE"), ("final_action", "DENY"),
    ("human_review_required", False), ("currency", "EUR"),
    ("recommendation_path", "APPROVE"), ("mode", "test"),
    ("normalized_amounts_usd", {"x": 0.1}),
])
def test_model_cannot_represent_operational_action_or_float(key, value):
    payload = evaluate().model_dump()
    payload[key] = value
    with pytest.raises(ValidationError):
        LoanRecommendation.model_validate(payload)


def test_production_model_cannot_use_simulated_path_or_mismatched_batch():
    payload = evaluate().model_dump()
    payload.update(mode="PRODUCTION", recommendation_path="SIMULATED_APPROVE_PATH")
    with pytest.raises(ValidationError):
        LoanRecommendation.model_validate(payload)
    with pytest.raises(ValidationError):
        UnderwritingBatch(mode="PRODUCTION", policy_version="different", loan_results=[evaluate()])


@pytest.mark.parametrize("value", [0.1, 100000.0, True, "NaN", "Infinity", "1e5", "100,000", "$100000", "=100000", {}, []])
def test_money_rejects_lossy_or_ambiguous_values(value):
    with pytest.raises(ValueError):
        exact_decimal(value)


@pytest.mark.parametrize("value", ["0.10", Decimal("0.10"), 1, "9999999999999999999999999999.99"])
def test_money_exact_no_context_rounding(value):
    assert exact_decimal(value) == Decimal(str(value))


def test_exact_constants():
    assert (USD_25K, USD_100K, USD_250K, USD_100M) == tuple(map(Decimal, ("25000", "100000", "250000", "100000000")))
    assert (USD_100K, USD_25M, USD_100M, USD_250M) == tuple(map(Decimal, ("100000.00", "25000000.00", "100000000.00", "250000000.00")))
    assert all(value.as_tuple().exponent == -2 for value in (USD_100K, USD_25M, USD_100M, USD_250M))


@pytest.mark.parametrize("field,value", [
    ("currency", "EUR"), ("currency", ""), ("currency", None),
    ("currency", "unknown"), ("amount_scale", "thousands"), ("amount_scale", None),
    ("requested_amount", 100000.0), ("requested_amount", "-1"),
])
def test_bad_row_units_and_float_money_hold_even_with_synthetic_policy(field, value):
    source = rows()
    source[1][1][field] = value
    result = evaluate(source, config=synthetic_policy())
    assert result.recommendation_path == "SIMULATED_REVIEW"
    assert result.normalized_amounts_usd["loan_requested_amount_usd"] is None
    assert all(not isinstance(item.value, float) for item in result.source_evidence)


def test_unknown_manifest_currency_without_row_currency_is_rejected():
    result = evaluate(config={"source_currency": None})
    assert result.recommendation_path == "SIMULATED_REVIEW"


def test_row_units_explicit_can_override_unknown_manifest():
    source = rows()
    for _, fields in source:
        fields.update(currency="USD", amount_scale="actual_dollars")
    assert evaluate(source, config={"source_currency": None}).recommendation_path == "SIMULATED_REVIEW"


@pytest.mark.parametrize("score,expected", [(159, "LT_160"), (160, "160_189"), (189, "160_189"),
                                            (190, "190_199"), (199, "190_199"), (200, "GE_200")])
def test_sbss_boundaries_only_classify_not_decide(score, expected):
    assert sbss_bucket(score) == expected
    source = rows()
    source[3][1]["sbss"] = str(score)
    result = evaluate(source)
    assert result.normalized_data["sbss_bucket"] == expected
    assert result.recommendation_path == "SIMULATED_REVIEW"
    assert rule(result, "SBSS_FICO_BRANCH_POLICY").status == "UNKNOWN"


@pytest.mark.parametrize("score,expected", [(649, "LT_650"), (650, "650_759"), (759, "650_759"), (760, "GE_760")])
def test_fico_boundaries(score, expected):
    assert fico_band(score) == expected
    source = rows()
    source[2][1]["personal_fico"] = str(score)
    assert evaluate(source).normalized_data["fico_band"] == expected


@pytest.mark.parametrize("score", [None, -1, "1.1", "NaN", True, "301", "=200"])
def test_invalid_sbss_is_unknown(score):
    assert sbss_bucket(score) is None


@pytest.mark.parametrize("score", [None, 299, 851, "650.5", "=760", True])
def test_invalid_fico_is_unknown(score):
    assert fico_band(score) is None


@pytest.mark.parametrize("field,value,feature,expected", [
    ("new_money", "99999999.99", "new_money_within_diagnostic_limit", True),
    ("new_money", "100000000", "new_money_within_diagnostic_limit", True),
    ("new_money", "100000000.01", "new_money_within_diagnostic_limit", False),
    ("tier_i", "250000000", "tier_i_within_diagnostic_limit", True),
    ("tier_i", "250000000.01", "tier_i_within_diagnostic_limit", False),
    ("requested_amount", "100000", "requested_amount_le_100k", True),
    ("requested_amount", "100000.01", "requested_amount_le_100k", False),
    ("requested_amount", "100000000", "requested_amount_le_100m", True),
    ("requested_amount", "100000000.01", "requested_amount_le_100m", False),
])
def test_usd_threshold_diagnostics(field, value, feature, expected):
    source = rows()
    source[1][1][field] = value
    assert evaluate(source).normalized_data[feature] is expected


@pytest.mark.parametrize("tier1,tier2,expected", [("1", "2", "2"), ("250000", "250000", "250000"), ("250001", "249999", "250001")])
def test_effective_tgc_is_max_not_sum(tier1, tier2, expected):
    source = rows()
    source[1][1].update(tier_i=tier1, tier_ii=tier2)
    assert evaluate(source).normalized_amounts_usd["total_group_credit_usd"] == Decimal(expected)


def test_missing_tier_is_unknown_not_zero():
    source = rows()
    del source[1][1]["tier_ii"]
    assert evaluate(source).normalized_amounts_usd["total_group_credit_usd"] is None


@pytest.mark.parametrize("value,expected", [("1.999", False), ("2", True), ("2.001", True), (None, None), ("-1", None)])
def test_ownership_boundary(value, expected):
    source = rows()
    source[0][1]["ownership_years"] = value
    assert evaluate(source).normalized_data["ownership_at_least_2_years"] is expected


@pytest.mark.parametrize("value,expected", [("-0.01", False), ("0.00", True), ("0.01", True), (None, None)])
def test_negative_ebitda(value, expected):
    source = rows()
    source[0][1]["ebitda"] = value
    assert evaluate(source).normalized_data["ebitda_nonnegative"] is expected


@pytest.mark.parametrize("discharge,asof,expected", [
    ("2021-10-20", "2026-10-19", False), ("2021-10-20", "2026-10-20", True),
    ("2020-02-29", "2025-02-27", False), ("2020-02-29", "2025-02-28", True),
    ("2027-01-01", "2026-10-20", None), (None, "2026-10-20", None),
    ("10/20/2021", "2026-10-20", None),
])
def test_bankruptcy_five_calendar_years(discharge, asof, expected):
    source = rows()
    source[1][1]["as_of_date"] = asof
    source[2][1].update(bankruptcy_count="1", bankruptcy_discharge_date=discharge, report_date=asof)
    assert evaluate(source).normalized_data["bankruptcy_discharge_at_least_5_years"] is expected
    assert calendar_anniversary(date(2020, 2, 29), 5) == date(2025, 2, 28)


@pytest.mark.parametrize("dob,expected", [("2008-10-20", True), ("2008-10-21", False), (None, None), ("2030-01-01", None), ("10/20/2008", None)])
def test_explicit_dob_age_boundary(dob, expected):
    source = rows()
    source[0][1]["dob"] = dob
    assert evaluate(source, config={"minimum_age": 18}).normalized_data["minimum_age_met"] is expected
    assert rule(evaluate(source), "MINIMUM_AGE").status == ("UNKNOWN" if expected is None else "PASS" if expected else "FAIL")


@pytest.mark.parametrize("disposition,alert,freeze,confirmed", [
    ("confirmed_match", True, False, True), ("cleared", False, False, False),
    (None, True, True, None), (None, False, False, None),
])
def test_ofac_alert_never_equals_confirmed_match(disposition, alert, freeze, confirmed):
    source = rows()
    source[2][1].update(ofac_disposition=disposition, ofac_alert=alert, credit_freeze=freeze)
    result = evaluate(source)
    assert result.normalized_data["ofac_confirmed_match"] is confirmed
    assert result.normalized_data["credit_freeze_clear"] is not freeze
    assert result.normalized_data["ofac_alert_clear"] is not alert


def test_empty_naics_list_unresolved_not_clear():
    assert rule(evaluate(config={"high_risk_naics": []}), "HIGH_RISK_NAICS").status == "UNKNOWN"
    assert evaluate(config={"high_risk_naics": ["541100"]}).normalized_data["naics_not_high_risk"] is False


@pytest.mark.parametrize("nfp,status", [(True, "FAIL"), (False, "PASS"), (None, "UNKNOWN")])
def test_quick_app_nfp_diagnostic(nfp, status):
    source = rows()
    source[0][1]["is_nfp"] = nfp
    assert rule(evaluate(source), "QUICKAPP_NFP_DIAGNOSTIC").status == status
    source[1][1]["requested_path"] = "full_underwriting"
    assert rule(evaluate(source), "QUICKAPP_NFP_DIAGNOSTIC").status == "NOT_APPLICABLE"


@pytest.mark.parametrize("product,amount,status", [("express_baloc", "100000000", "PASS"), ("express_baloc", "100000000.01", "FAIL"),
                                                   ("conventional_baloc", "100000000", "FAIL"), ("conventional_baloc", "100000000.01", "PASS"),
                                                   ("bil", "100000", "UNKNOWN")])
def test_product_boundary_diagnostic(product, amount, status):
    source = rows()
    source[1][1].update(product=product, requested_amount=amount)
    assert rule(evaluate(source), "PRODUCT_AMOUNT_DIAGNOSTIC").status == status


@pytest.mark.parametrize("product,amount,status", [("express_baloc", "25000000", "PASS"), ("sba_baloc", "25000000.01", "FAIL"), ("term_loan", "25000000.01", "NOT_APPLICABLE")])
def test_special_baloc_cap_not_term_cap(product, amount, status):
    source = rows()
    source[1][1].update(product=product, requested_amount=amount, is_startup=True)
    assert rule(evaluate(source), "BALOC_SPECIAL_25M_CANDIDATE").status == status


def test_one_account_many_loans_and_many_guarantors():
    source = rows()
    second = copy.deepcopy(source[1][1])
    second["Id"] = "L2"
    source.extend([("Loan", second), ("LCRESPONSE", {"Id": "S2", "loan_id": "L2", "sbss": "159"})])
    for index in (1, 2):
        source.extend([
            ("Account", {"Id": f"G{index}"}),
            ("EntityInvolvement", {"Id": f"J{index}", "account_id": f"G{index}", "loan_id": "L1", "role": "Guarantor"}),
                    ("CreditReport", {"Id": f"GR{index}", "account_id": f"G{index}", "status": "completed", "report_date": "2026-10-19", "personal_fico": "650", "report_role": "guarantor"}),
        ])
    batch = evaluate_underwriting([document(source)])
    assert len(batch.loan_results) == 2
    assert not batch.data_quality_issues
    first, second = batch.loan_results
    assert first.borrower_account_id == second.borrower_account_id == "A1"
    assert first.normalized_data["guarantor_account_ids"] == ["G1", "G2"]
    assert first.normalized_data["fico"] == "686.6666666666666666666666667"
    assert first.normalized_data["fico_band"] == "650_759"
    assert first.normalized_data["sbss_bucket"] == "GE_200"
    assert second.normalized_data["sbss_bucket"] == "LT_160"


def test_screenshot_average_fico_is_unknown_when_any_linked_party_score_is_missing():
    source = rows()
    source.append(("EntityInvolvement", {"Id": "J1", "account_id": "G1", "loan_id": "L1", "role": "guarantor"}))
    source.append(("Account", {"Id": "G1"}))

    result = evaluate(source)

    assert result.normalized_data["fico"] is None
    assert result.normalized_data["fico_band"] is None
    assert any(item.startswith("guarantor_fico_average:G1:") for item in result.missing_inputs)
    assert result.recommendation_path == "SIMULATED_REVIEW"


@pytest.mark.parametrize("role,valid", [("Borrower", True), ("BORROWER", True), ("borrower", True), ("Owner", False), (None, False)])
def test_mandatory_junction_roles(role, valid):
    source = rows()
    del source[1][1]["borrower_account_id"]
    source.append(("EntityInvolvement", {"Id": "J", "loan_id": "L1", "account_id": "A1", "role": role}))
    result = evaluate(source)
    assert result.normalized_data["identity_valid"] is valid
    if not valid:
        assert result.recommendation_path == "SIMULATED_REVIEW"


def test_identity_mapping_validated_flag_is_not_proof():
    source = rows()
    del source[1][1]["borrower_account_id"]
    source[1][1]["Identity_Mapping_Validated"] = True
    result = evaluate(source, config=synthetic_policy())
    assert result.borrower_account_id is None
    assert result.recommendation_path == "SIMULATED_REVIEW"


@pytest.mark.parametrize("missing", ["account_id", "loan_id"])
def test_junction_orphan_holds(missing):
    source = rows()
    fields = {"Id": "J", "loan_id": "L1", "account_id": "A1", "role": "guarantor"}
    fields[missing] = "NONEXISTENT"
    source.append(("EntityInvolvement", fields))
    assert evaluate(source).recommendation_path == "SIMULATED_REVIEW"


def test_multiple_borrowers_conflict():
    source = rows() + [("Account", {"Id": "A2"}), ("EntityInvolvement", {"Id": "J", "loan_id": "L1", "account_id": "A2", "role": "borrower"})]
    assert evaluate(source).recommendation_path == "SIMULATED_REVIEW"


def test_flattened_aliases_and_direct_id_not_guessed():
    fields = {"Loan_Request_Id": "L1", "LLC_BI__Account__c": "A1", "borrower_role": "Borrower",
              "nFUSE__Credit_Report__r.Id": "R1", "report_status": "completed", "report_date": "2026-10-19",
              "SCOC__LCResponse__r.Id": "S1", "SCOC__LCResponse__r.SCOC__SBSS7Score__c": "190",
              "nFUSE__Credit_Score__c": "650", "report_category": "personal", "report_role": "borrower",
              "as_of_date": "2026-10-20", "requested_amount": "100000"}
    doc = document([("Data", fields)], "MTB_Query_1_Sample_Data.xlsx")
    result = evaluate_underwriting([doc]).loan_results[0]
    assert result.borrower_account_id == "A1"
    assert result.normalized_data["sbss_bucket"] == "190_199"
    assert result.normalized_data["fico_band"] == "650_759"
    fields["Id"] = fields.pop("Loan_Request_Id")
    batch = evaluate_underwriting([document([("Data", fields)], "MTB_Query_1.xlsx")])
    assert not batch.loan_results
    assert "NO_EXPLICIT_LOAN_RECORDS" in batch.data_quality_issues


@pytest.mark.parametrize("account_header", [
    "ACCOUNT_ID", "Account ID", "Account-ID", "LLC_BI__Account__c",
    "LLC_BI__Account__r.Id", "nFUSE__Account__c", "AccountId__c",
])
def test_account_reference_header_variants_link_across_query_files(account_header):
    source_documents = [
        document([("Account", {"Id": "A1", "ownership_years": "2", "naics": "541100", "ebitda": "0"})],
                 "MTB_Query_1.xlsx", "q1"),
        document([("Loan", {"Id": "L1", account_header: "A1", "as_of_date": "2026-10-20",
                             "product": "express_baloc", "requested_path": "quick_app"})],
                 "MTB_Query_1.xlsx", "q1-loan"),
        document([("LCRESPONSE", {"Id": "S1", "loan_id": "L1", "sbss": "200", "score_date": "2026-10-19"})],
                 "MTB_Query_2.xlsx", "q2"),
        document([("CreditReport", {"Id": "R1", account_header: "A1", "status": "completed",
                                    "report_date": "2026-10-19", "personal_fico": "760",
                                    "report_category": "personal", "report_role": "borrower"})],
                 "MTB_Query_4.xlsx", "q4"),
    ]

    # Query 1 holds account and loan sheets in the same physical workbook.
    source_documents[0].metadata["records"].extend(source_documents[1].metadata["records"])
    source_documents = [source_documents[0], source_documents[2], source_documents[3]]
    result = evaluate_underwriting(source_documents).loan_results[0]

    assert result.borrower_account_id == "A1"
    assert result.normalized_data["sbss"] == "200"
    assert result.normalized_data["fico_band"] == "GE_760"


def test_conflicting_account_reference_aliases_do_not_choose_preferred_column():
    source = document([("Account", {"Id": "A1"}),
                       ("Loan", {"Id": "L1", "ACCOUNT_ID": "A1", "LLC_BI__Account__c": "A2"})],
                      "MTB_Query_1.xlsx")

    batch = evaluate_underwriting([source])

    assert len(batch.loan_results) == 1
    assert batch.loan_results[0].borrower_account_id is None
    assert batch.loan_results[0].recommendation_path == "DATA_QUALITY_HOLD"
    assert any("CONFLICTING_ALIASES:Loan:borrower_account_id" in issue for issue in batch.data_quality_issues)


@pytest.mark.parametrize("response_id,expected", [("S1", "GE_200"), ({"Id": "S1"}, None), ("{'Id': 'S1'}", None), ("[object Object]", None)])
def test_scalar_response_must_be_id_scalar_not_object_repr(response_id, expected):
    fields = {"LLC_BI__Loan__c": "L1", "LLC_BI__Account__c": "A1", "LLC_BI__Borrower_Type__c": "Borrower",
              "SCOC__LCResponse__r": response_id, "SCOC__LCResponse__r.SCOC__SBSS7Score__c": "200"}
    result = evaluate_underwriting([document([("Data", fields)], "MTB_Query_1.xlsx")]).loan_results[0]
    assert result.normalized_data["sbss_bucket"] == expected


def test_unrelated_workbook_generic_id_never_creates_loan():
    batch = evaluate_underwriting([document([("Data", {"Id": "L1", "borrower_account_id": "A1"})])])
    assert not batch.loan_results


def test_renamed_workbook_header_detection_used():
    fields = {"LLC_BI__Loan__c": "L1", "LLC_BI__Account__c": "A1", "borrower_role": "Borrower"}
    doc = document([("Data", fields)])
    doc.metadata["sheets"] = [{"name": "Data", "headers": ["Behavioral_Underwriting_Program__c", "Estimated_Annual_Revenue__c", "SCOC__CharacteristicKey__c"]}]
    assert evaluate_underwriting([doc]).loan_results[0].borrower_account_id == "A1"


def test_report_latest_valid_completed_not_row_order():
    source = rows() + [
        ("CreditReport", {"Id": "OLD", "account_id": "A1", "status": "completed", "report_date": "2025-10-19", "fico": "649"}),
        ("CreditReport", {"Id": "PENDING", "account_id": "A1", "status": "pending", "report_date": "2026-10-20", "fico": "649"}),
        ("CreditReport", {"Id": "FUTURE", "account_id": "A1", "status": "completed", "report_date": "2027-10-20", "fico": "649"}),
    ]
    result = evaluate(list(reversed(source)))
    assert result.normalized_data["selected_report_id"] == "R1"
    assert result.normalized_data["fico_band"] == "GE_760"


@pytest.mark.parametrize("date_value", [None, "10/19/2026", "2026-13-01", "2026-10-19T01:00:00"])
def test_missing_or_ambiguous_completed_dates_no_recency_inference(date_value):
    source = rows()
    source[2][1]["report_date"] = date_value
    result = evaluate(source)
    assert result.normalized_data["selected_report_id"] is None
    assert result.normalized_data["fico_band"] is None


def test_undated_completed_report_cannot_be_silently_ignored():
    source = rows() + [("CreditReport", {"Id": "R2", "account_id": "A1", "status": "completed", "fico": "650"})]
    assert evaluate(source).normalized_data["selected_report_id"] is None


def test_report_tie_unknown_even_same_score():
    source = rows()
    other = copy.deepcopy(source[2][1])
    other["Id"] = "R2"
    source.append(("CreditReport", other))
    assert evaluate(source).normalized_data["selected_report_id"] is None


def test_missing_request_asof_never_uses_wall_clock():
    source = rows()
    del source[1][1]["as_of_date"]
    assert evaluate(source).normalized_data["selected_report_id"] is None


def test_unrelated_account_bureau_data_never_attached():
    source = rows()
    source[2][1]["account_id"] = "A2"
    source.append(("Account", {"Id": "A2"}))
    result = evaluate(source)
    assert result.normalized_data["fico_band"] is None
    assert not any(item.record_id == "R1" for item in result.source_evidence)


@pytest.mark.parametrize("other_score", ["200", "159"])
def test_multiple_responses_ambiguous_even_same_score(other_score):
    source = rows() + [("LCRESPONSE", {"Id": "S2", "loan_id": "L1", "sbss": other_score})]
    assert evaluate(source).normalized_data["sbss_bucket"] is None
    source[1][1]["response_id"] = "S2"
    assert evaluate(source).normalized_data["sbss_bucket"] == sbss_bucket(other_score)


def test_explicit_response_cannot_point_to_other_loan():
    source = rows()
    source[1][1]["response_id"] = "S2"
    source.extend([("Loan", {"Id": "L2", "borrower_account_id": "A1"}), ("LCRESPONSE", {"Id": "S2", "loan_id": "L2", "sbss": "300"})])
    result = evaluate(source)
    assert result.normalized_data["sbss_bucket"] is None
    assert result.recommendation_path == "SIMULATED_REVIEW"


def observations():
    return [
        ("LCREntity", {"Id": "E1", "account_id": "A1", "response_id": "S1"}),
        ("DataSourceAnalysis", {"Id": "D1", "entity_id": "E1"}),
        ("Indicator", {"Id": "I1", "analysis_id": "D1", "key": "OWNERSHIP", "value": "2"}),
        ("Indicator", {"Id": "I2", "analysis_id": "D1", "key": "OTHER", "value": "yes"}),
        ("Characteristic", {"Id": "C1", "analysis_id": "D1", "key": "FLAG", "value": "false"}),
    ]


def test_full_graph_retains_one_to_many_with_provenance_no_implicit_pivot():
    result = evaluate(rows() + observations())
    assert len(result.normalized_data["indicators"]) == 2
    assert len(result.normalized_data["characteristics"]) == 1
    assert result.normalized_data["ownership_years"] == "2"
    assert {item.record_id for item in result.source_evidence} >= {"E1", "D1", "I1", "I2", "C1"}
    assert all(set(item.evidence_refs) <= {e.id for e in result.source_evidence} for item in result.rule_results)


def test_selected_account_scopes_engine_to_its_loans_and_source_evidence():
    source = rows() + [
        ("Account", {"Id": "A2", "ownership_years": "5", "naics": "541100", "ebitda": "100"}),
        ("Loan", {"Id": "L2", "borrower_account_id": "A2", "as_of_date": "2026-10-20",
                  "product": "bil", "requested_path": "full_underwriting", "requested_amount": "50000"}),
        ("CreditReport", {"Id": "R2", "account_id": "A2", "status": "completed",
                           "report_date": "2026-10-19", "personal_fico": "800"}),
    ]

    batch = evaluate_underwriting([document(source)], selected_account_id="A1")

    assert [loan.loan_id for loan in batch.loan_results] == ["L1"]
    evidence_record_ids = {item.record_id for loan in batch.loan_results for item in loan.source_evidence}
    assert not {"A2", "L2", "R2"} & evidence_record_ids


def test_screenshot_er_diagram_labels_map_characteristics_and_relationships():
    source = [
        ("ACCOUNT", {"ACCOUNT_ID": "A1"}),
        ("LLC_BI_LOAN__C", {"LOAN_ID": "L1", "ACCOUNT_ID": "A1"}),
        ("NFUSE_CREDIT_REPORT__C", {
            "CREDIT_REPORT_ID": "R1", "ACCOUNT_ID": "A1", "CREDIT_SCORE": "700",
            "NO OF JUDGEMENTS": "2",
        }),
        ("SCOC_LCRESPONSE__C", {"LCResponse_ID": "S1", "Loan_ID": "L1", "sbss": "200"}),
        ("SCOC_LCRENTITY__C", {"LCREntity_ID": "E1", "LCResponse_ID": "S1", "Account_ID": "A1"}),
        ("SCOC_LCRDATASOURCEANALYSIS__C", {"LCRDataSourceAnalysis_ID": "D1", "LCREntity_ID": "E1"}),
        ("SCOC_LCCHARACTERISTIC__C", {
            "LCCharacteristic_ID": "C1",
            "LCDataSourceAnalysis_ID": "D1",
            "Characteristic_Key": "FLAG",
            "Characteristic_Value": "false",
        }),
    ]

    graph = build_entity_graph([document(source)], load_underwriting_config())

    assert not graph.issues
    characteristic = graph.table("Characteristic")["C1"]
    assert characteristic.fields == {
        "analysis_id": "D1", "key": "FLAG", "value": "false",
    }
    assert graph.table("DataSourceAnalysis")["D1"].fields["entity_id"] == "E1"
    assert graph.table("LCREntity")["E1"].fields["response_id"] == "S1"
    assert graph.table("CreditReport")["R1"].fields["fico"] == "700"
    assert graph.table("CreditReport")["R1"].fields["judgment_count"] == "2"


def test_salesforce_legal_entity_junction_links_account_and_loan_explicitly():
    source = [
        ("Account", {"Id": "A1", "name": "Synthetic borrower"}),
        ("Loan", {"Id": "L1", "as_of_date": "2026-10-20"}),
        ("LLC_BI__LEGAL_ENTITIES__C", {
            "Id": "J1", "LLC_BI__Loan__c": "L1", "LLC_BI__Account__c": "A1",
            "LLC_BI__Borrower_Type__c": "Borrower",
        }),
    ]

    graph = build_entity_graph([document(source, name="MTB_Query_2.xlsx")], load_underwriting_config())

    assert graph.borrowers("L1") == {"A1"}
    assert not graph.issues


def test_erd_canonical_loan_account_id_links_one_account_to_many_loans():
    source = [
        ("Account", {"Id": "A1"}),
        ("Loan", {"Id": "L1", "ACCOUNT_ID": "A1", "as_of_date": "2026-10-20"}),
        ("Loan", {"Id": "L2", "ACCOUNT_ID": "A1", "as_of_date": "2026-10-20"}),
        ("LCRESPONSE", {"Id": "S1", "loan_id": "L1", "sbss": "200", "score_date": "2026-10-19"}),
        ("LCRESPONSE", {"Id": "S2", "loan_id": "L1", "sbss": "210", "score_date": "2026-10-20"}),
    ]

    batch = evaluate_underwriting([document(source)])

    assert [loan.loan_id for loan in batch.loan_results] == ["L1", "L2"]
    assert all(loan.borrower_account_id == "A1" for loan in batch.loan_results)
    loan_one = next(loan for loan in batch.loan_results if loan.loan_id == "L1")
    assert loan_one.normalized_data["selected_response_id"] == "S2"
    assert loan_one.normalized_data["sbss"] == "210"


def test_three_query_files_link_entities_by_explicit_account_column_variants():
    query1 = document([
        ("Account", {"Id": "A1", "ownership_years": "2", "naics": "541100", "ebitda": "0"}),
        ("Loan", {"Id": "L1", "Account Column": "A1", "as_of_date": "2026-10-20",
                  "product": "express_baloc", "requested_path": "quick_app"}),
    ], "MTB_Query_1.xlsx", "q1")
    query2 = document([
        ("LCRESPONSE", {"Id": "S1", "loan_id": "L1", "sbss": "200", "score_date": "2026-10-19"}),
        ("EntityInvolvement", {"Id": "J1", "loan_id": "L1", "LLC_BI__Account__c": "A1", "role": "borrower"}),
        ("LCREntity", {"Id": "E1", "Account ID": "A1", "response_id": "S1"}),
        ("DataSourceAnalysis", {"Id": "D1", "entity_id": "E1"}),
        ("Characteristic", {"Id": "C1", "analysis_id": "D1", "key": "FLAG", "value": "Y"}),
    ], "MTB_Query_2.xlsx", "q2")
    query4 = document([
        ("CreditReport", {"Id": "R1", "nFUSE__Account__c": "A1", "status": "completed",
                          "report_date": "2026-10-19", "personal_fico": "760",
                          "report_category": "personal", "report_role": "borrower"}),
    ], "MTB_Query_4.xlsx", "q4")

    batch = evaluate_underwriting([query1, query2, query4])

    assert [loan.loan_id for loan in batch.loan_results] == ["L1"]
    loan = batch.loan_results[0]
    assert loan.borrower_account_id == "A1"
    assert loan.normalized_data["sbss"] == "200"
    assert loan.normalized_data["fico_band"] == "GE_760"
    assert loan.normalized_data["characteristics"][0]["key"] == "FLAG"
    assert not any("QUERY_FILE_SET_INCOMPLETE" in issue for issue in batch.data_quality_issues)


def test_validated_revision_soql_maps_query_root_ids_and_explicit_nested_fields():
    junction = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r."
    loan = junction + "LLC_BI__Loan__c"
    account = junction + "LLC_BI__Account__c"
    role = junction + "LLC_BI__Borrower_Type__c"
    q1 = junction + "LLC_BI__Loan__r."
    q1_account = junction + "LLC_BI__Account__r."
    q1_report = junction + "nFUSE__Credit_Report__r."
    q2_response = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__LCResponse__r."
    q1doc = document([("MTB Query 1", {
        "Id": "C1", loan: "L1", account: "A1", role: "Borrower",
        q1 + "LLC_BI__Product__c": "Express BALOC",
        q1 + "LLC_BI__Product_Type__c": "Express BALOC",
        q1 + "LLC_BI__Amount__c": "50000.00",
        q1_account + "Birth_Date__c": "1980-01-01",
        q1_account + "Number_of_Years_as_Current_Owner__c": "3",
        q1_account + "NAICS_Code_Formula__c": "541100",
        q1_report + "Id": "R1",
        q1_report + "nFUSE__Status__c": "Completed",
        q1_report + "nFUSE__Report_Category__c": "Personal",
        q1_report + "nFUSE__Credit_Score__c": "620",
        "SCOC__CharacteristicKey__c": "numCollection",
        "SCOC__CharacteristicValue__c": "0",
    })], "MTB_Query_1_Validated_Revision_SOQL.xlsx", "q1-soql")
    q2doc = document([("MTB Query 2", {
        "Id": "I1", loan: "L1", account: "A1", role: "Borrower",
        q2_response + "SCOC__SBSS7Score__c": "175",
        "SCOC__IndicatorKey__c": "thinFileIndicator",
        "SCOC__IndicatorValue__c": "N",
    })], "MTB_Query_2_Validated_Revision_SOQL.xlsx", "q2-soql")
    q4doc = document([("MTB Query 4", {
        "Id": "R1", "nFUSE__Account__c": "A1",
        "nFUSE__Credit_Score__c": "740", "nFUSE__Status__c": "Completed",
        "nFUSE__Report_Category__c": "Personal",
    })], "MTB_Query_4_Validated_Revision_SOQL.xlsx", "q4-soql")
    for source_doc in (q1doc, q2doc, q4doc):
        record = source_doc.metadata["records"][0]
        source_doc.metadata["sheets"] = [{
            "name": record["sheet"], "headers": list(record["values"]), "is_reference": False,
        }]

    graph = build_entity_graph([q1doc, q2doc, q4doc], load_underwriting_config())

    assert set(graph.table("Loan")) == {"L1"}
    assert set(graph.table("Account")) == {"A1"}
    assert set(graph.table("CreditReport")) == {"R1"}
    assert set(graph.table("EntityInvolvement")) == {"C1"}
    assert len(graph.table("Characteristic")) == 1
    assert next(iter(graph.table("Characteristic"))).startswith("observation:")
    assert set(graph.table("Indicator")) == {"I1"}
    assert graph.borrowers("L1") == {"A1"}
    assert graph.table("EntityInvolvement")["C1"].fields == {
        "loan_id": "L1", "account_id": "A1", "role": "Borrower",
    }
    assert graph.table("Loan")["L1"].fields["product"] == "express_baloc"
    assert graph.table("Loan")["L1"].fields["sbss"] == "175"
    assert graph.table("Account")["A1"].fields["dob"] == "1980-01-01"
    assert graph.table("Account")["A1"].fields["ownership_years"] == "3"
    assert graph.table("Account")["A1"].fields["naics"] == "541100"
    assert graph.table("CreditReport")["R1"].fields["report_role"] == "Borrower"
    assert graph.table("CreditReport")["R1"].fields["report_category"] == "Personal"
    assert graph.table("CreditReport")["R1"].fields["fico"] is None
    assert any(issue.startswith("CONFLICTING_DUPLICATE:CreditReport:fico") for issue in graph.issues)
    assert not any(issue.startswith(("MISSING_OR_INVALID_ID", "ORPHAN_RELATION")) for issue in graph.issues)
    assert "UNMAPPED_RELATION:Indicator:analysis_id" in graph.mapping_warnings
    assert "UNMAPPED_RELATION:Characteristic:analysis_id" in graph.mapping_warnings

    batch = evaluate_underwriting([q1doc, q2doc, q4doc], mode="TEST")
    recommendation, = batch.loan_results
    assert recommendation.normalized_amounts_usd["loan_requested_amount_usd"] == Decimal("50000.00")
    assert recommendation.normalized_data["product"] == "express_baloc"
    assert recommendation.normalized_data["ownership_years"] == "3"
    assert recommendation.normalized_data["sbss"] == "175"
    # Query 4 has no report date, so a mapped generic score is not promoted to
    # selected personal FICO or used for a decision branch.
    assert recommendation.normalized_data["fico"] is None
    assert "report_completion_status_missing_or_unknown" not in recommendation.missing_inputs
    assert "explicit_iso_as_of_date" in recommendation.missing_inputs
    assert "report_selection_requires_explicit_as_of_date" in recommendation.missing_inputs
    assert any(item.field == "nFUSE__Credit_Score__c" for item in recommendation.source_evidence)
    reconciliation = next(step for step in recommendation.normalized_data["decision_flow"]
                          if step["stage"] == "Query 1 / Query 2 / Query 4 source reconciliation")
    assert reconciliation["status"] == "CONFLICT"
    assert "neither score is selected" in reconciliation["summary"]
    fico_rule = rule(recommendation, "FICO_BAND_DIAGNOSTIC")
    assert fico_rule.status == "UNKNOWN"
    assert "same CreditReport ID has conflicting Query 1 nested and Query 4 root scores" in fico_rule.detail
    assert len(fico_rule.evidence_refs) == 2
    assert not any(item.endswith("_POLICY") for item in recommendation.missing_inputs)
    assert rule(recommendation, "FICO_BAND_DIAGNOSTIC").status == "UNKNOWN"
    assert rule(recommendation, "FICO_BAND_DIAGNOSTIC").evidence_refs
    assert recommendation.recommendation_path == "SIMULATED_REVIEW"
    assert "Credit report date" in batch.unavailable_inputs

    # The same CreditReport Id in Query 1's nested relationship and Query 4's
    # root export must agree. Conflicting source scores stay poisoned/held.
    q4doc.metadata["records"][0]["values"]["nFUSE__Credit_Score__c"] = "705"
    conflicting = build_entity_graph([q1doc, q2doc, q4doc], load_underwriting_config())
    assert conflicting.table("CreditReport")["R1"].fields["fico"] is None
    assert any(issue.startswith("CONFLICTING_DUPLICATE:CreditReport:fico") for issue in conflicting.issues)


def test_erd_entity_involvement_link_must_match_lcr_entity_account_and_response_loan():
    source = rows() + [
        ("EntityInvolvement", {"Id": "IV1", "loan_id": "L1", "account_id": "A1", "role": "borrower"}),
        ("LCREntity", {"Id": "E1", "account_id": "A2", "response_id": "S1", "entity_involvement_id": "IV1"}),
        ("Account", {"Id": "A2"}),
    ]

    graph = build_entity_graph([document(source)], load_underwriting_config())

    assert any("MISMATCHED_LCR_ENTITY_INVOLVEMENT" in issue for issue in graph.issues)


def test_screenshot_er_diagram_labels_map_in_flattened_mtb_rows():
    source = [("Data", {
        "Loan_Request_Id": "L1", "borrower_account_id": "A1", "borrower_role": "Borrower",
        "LLC_BI__Account__c": "A1", "LCResponse_ID": "S1", "LOAN_ID": "L1",
        "LCREntity_ID": "E1", "Account_ID": "A1", "LCRDataSourceAnalysis_ID": "D1",
        "LCDataSourceAnalysis_ID": "D1", "LCCharacteristic_ID": "C1",
        "Characteristic_Key": "FLAG", "Characteristic_Value": "false",
    })]

    graph = build_entity_graph(
        [document(source, name="MTB_Query_2.xlsx")], load_underwriting_config(),
    )

    assert not graph.issues
    assert graph.table("Characteristic")["C1"].fields == {
        "analysis_id": "D1", "key": "FLAG", "value": "false",
    }


@pytest.mark.parametrize("table,key,bad", [("CreditReport", "account_id", "MISSING"), ("LoanExtension", "loan_id", "MISSING"),
                                         ("LCREntity", "response_id", "MISSING"), ("LCREntity", "account_id", "MISSING"),
                                         ("DataSourceAnalysis", "entity_id", "MISSING"), ("Indicator", "analysis_id", "MISSING"),
                                         ("Characteristic", "analysis_id", "MISSING")])
def test_all_orphan_relationships_hold(table, key, bad):
    source = rows() + observations() + [("LoanExtension", {"Id": "X1", "loan_id": "L1"})]
    next(fields for name, fields in source if name == table)[key] = bad
    assert evaluate(source).recommendation_path == "SIMULATED_REVIEW"


def test_duplicate_conflicting_indicator_key_not_overwritten():
    source = rows() + observations() + [("Indicator", {"Id": "I3", "analysis_id": "D1", "key": "OWNERSHIP", "value": "3"})]
    result = evaluate(source, config=synthetic_policy())
    assert result.recommendation_path == "SIMULATED_REVIEW"
    assert len(result.normalized_data["indicators"]) == 3
    assert any("CONFLICTING_INDICATOR_KEY" in issue for issue in result.conflicts)


def test_identical_entity_duplicates_merge_provenance_conflicts_poison():
    source = rows()
    duplicate = copy.deepcopy(source[0])
    source.append(duplicate)
    graph = build_entity_graph([document(source)], load_underwriting_config())
    assert len(graph.table("Account")) == 1
    assert not graph.issues
    assert len(graph.table("Account")["A1"].field_refs["ownership_years"]) == 2
    source[-1][1]["ownership_years"] = "3"
    graph = build_entity_graph([document(source)], load_underwriting_config())
    assert graph.table("Account")["A1"].fields["ownership_years"] is None
    assert evaluate(source).recommendation_path == "SIMULATED_REVIEW"


def test_repeated_flattened_entity_across_indicator_rows_merges():
    source = []
    for index in (1, 2):
        source.append(("Data", {"Loan_Request_Id": "L1", "LLC_BI__Account__c": "A1", "borrower_role": "Borrower",
                                "SCOC__LCResponse__r.Id": "S1", "SCOC__LCResponse__r.SCOC__SBSS7Score__c": "200",
                                "SCOC__LCREntity__r.Id": "E1", "SCOC__DataSourceAnalysis__r.Id": "D1",
                                "SCOC__IndicatorKey__c": f"KEY{index}", "SCOC__IndicatorValue__c": str(index)}))
    batch = evaluate_underwriting([document(source, "MTB_Query_2.xlsx")])
    assert not batch.data_quality_issues
    assert len(batch.loan_results) == 1
    result = batch.loan_results[0]
    assert len(result.normalized_data["indicators"]) == 2
    assert all(item["record_id"] is None for item in result.normalized_data["indicators"])


def test_loan_extension_at_most_one():
    source = rows() + [("LoanExtension", {"Id": "X1", "loan_id": "L1"}), ("LoanExtension", {"Id": "X2", "loan_id": "L1"})]
    assert evaluate(source).recommendation_path == "SIMULATED_REVIEW"


def test_synthetic_simulation_is_explicit_and_never_changes_defaults():
    before = load_underwriting_config()
    config = synthetic_policy()
    result = evaluate(config=config)
    assert result.recommendation_path == "SIMULATED_APPROVE_PATH"
    assert result.final_action == "NO_OPERATIONAL_ACTION"
    assert "Synthetic TEST" in result.explanation
    assert not result.conflicts
    source = rows()
    source[3][1]["sbss"] = "199"
    assert evaluate(source, config=config).recommendation_path == "SIMULATED_DECLINE_PATH"
    source[3][1]["sbss"] = None
    assert evaluate(source, config=config).recommendation_path == "SIMULATED_REVIEW"
    assert evaluate(mode="PRODUCTION", config=config).recommendation_path == "POLICY_REVIEW_REQUIRED"
    assert evaluate().recommendation_path == "SIMULATED_REVIEW"
    assert before == load_underwriting_config()


@pytest.mark.parametrize("changes", [
    {"approved": False}, {"synthetic": False}, {"label": ""}, {"criteria": None}, {"criteria": []},
    {"resolved_conflicts": []}, {"resolved_conflicts": ["REQUEST_THRESHOLD_100M_VS_100K"]},
])
def test_incomplete_synthetic_authorization_never_simulates_decision(changes):
    config = synthetic_policy()
    config["test_policy"].update(changes)
    assert evaluate(config=config).recommendation_path == "SIMULATED_REVIEW"


@pytest.mark.parametrize("criterion", [
    {"rule_id": "TEST-X", "feature": "missing", "op": "eq", "value": True},
    {"rule_id": "TEST-X", "feature": "sbss", "op": "eval", "value": "200"},
    {"rule_id": "APPROVE", "feature": "sbss", "op": "ge", "value": "200"},
    {"rule_id": "TEST-X", "feature": "sbss", "op": "ge", "value": 200.0},
    {"rule_id": "TEST-X", "feature": "sbss", "op": "ge", "value": "200", "extra": True},
    "__import__('os')",
])
def test_invalid_test_criteria_unknown_not_executable(criterion):
    config = synthetic_policy()
    config["test_policy"]["criteria"] = [criterion]
    assert evaluate(config=config).recommendation_path == "SIMULATED_REVIEW"


def test_source_claim_or_external_approval_never_unlocks_production():
    config = synthetic_policy()
    config.update(source_available=True, approved=True, source_sha256="0" * 64, unresolved_conflicts=[])
    batch = evaluate_underwriting([document(rows())], policy_config=config)
    assert batch.source_available is False
    assert batch.loan_results[0].recommendation_path == "POLICY_REVIEW_REQUIRED"


def test_logs_only_rule_status_and_opaque_provenance(caplog):
    source = rows()
    source[0][1]["Id"] = "PRIVATE_BORROWER"
    source[1][1]["borrower_account_id"] = "PRIVATE_BORROWER"
    source[2][1]["account_id"] = "PRIVATE_BORROWER"
    with caplog.at_level(logging.INFO, logger="backend.underwriting_engine"):
        evaluate(source)
    assert "rule_id=" in caplog.text and "status=" in caplog.text and "provenance=" in caplog.text
    assert "PRIVATE_BORROWER" not in caplog.text
    assert "100000" not in caplog.text


def test_empty_reference_and_malformed_data_safe():
    batch = evaluate_underwriting([document([], "MTB_Query_1.xlsx")])
    assert not batch.loan_results
    assert "EMPTY_RECOGNIZED_WORKBOOK" in batch.data_quality_issues
    doc = document(rows())
    doc.metadata["sheets"] = [{"name": "Loan", "is_reference": True}]
    assert not evaluate_underwriting([doc]).loan_results
    doc.metadata = {"records": [None, {"sheet": "Loan", "row": "two", "values": {}}]}
    assert evaluate_underwriting([doc]).data_quality_issues


def test_evidence_rejects_float_and_no_extra_keys():
    with pytest.raises(ValidationError):
        SourceEvidence(id="e", workbook="test", sheet="Loan", row=2, field="requested_amount", value=0.1)
    with pytest.raises(ValidationError):
        UnderwritingBatch(mode="TEST", policy_version="synthetic", operational_action="APPROVE")