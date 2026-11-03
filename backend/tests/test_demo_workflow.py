"""TEST demo regressions: no provider calls, no replacement of strict results."""

import copy
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest

from backend.demo_workflow import (AGE_AGGREGATION_BASIS, FICO_AGGREGATION_BASIS, SELECTION_BASIS,
                                   bounded_demo_context, evaluate_demo)
from backend.entity_graph import build_entity_graph
from backend import commentary, prompts
from backend.expanded_spec import REASONS
from backend.llm import AnthropicLLM, LLMError
from backend.llm_context import demo_explanation_context, report_context
from backend.loan_review import review_selected_loan
from backend.models import Report, Session
from backend.tests.test_underwriting import document, rows as base_rows
from backend.underwriting_engine import evaluate_underwriting, load_underwriting_config


def rows():
    # TEST FICO applies only to Individual accounts; the shared fixture's
    # borrower supplies FICO here unless a test overrides its type.
    source = base_rows()
    source[0][1]["account_type"] = "Individual"
    return source


@pytest.fixture(autouse=True)
def demo_clock(monkeypatch):
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 20, tzinfo=timezone.utc)
    monkeypatch.setattr("backend.demo_workflow.datetime", clock)
    return clock


def evaluate(source=None, mode="TEST"):
    return evaluate_underwriting([document(rows() if source is None else source)], mode=mode)


def demo(source=None):
    return evaluate(source).loan_results[0].normalized_data["demo_decision"]


def criterion(result, key):
    return next(item for item in result["criteria"] if item["criterion"] == key)


def linked_party(source, account_id="G1", score="650", role="Guarantor", account_type="Individual", **report_fields):
    source.extend([
        ("Account", {"Id": account_id, "dob": "1980-01-01", "ebitda": "100", "account_type": account_type}),
        ("EntityInvolvement", {"Id": "J" + account_id, "loan_id": "L1",
                               "account_id": account_id, "role": role}),
    ])
    if score is not None:
        source.append(("CreditReport", {"Id": "R" + account_id, "account_id": account_id,
                                       "personal_fico": score, **report_fields}))
    return source


def test_linked_borrower_guarantor_average_and_per_account_first_report_provenance():
    source = linked_party(rows(), score="651", report_date="2030-01-01", status="pending")
    source.append(("CreditReport", {"Id": "LATER", "account_id": "G1", "personal_fico": "850"}))
    view = demo(source)
    assert view["normalized_facts"]["fico"] == "705.5"
    assert view["fico_aggregation"] == {"basis": FICO_AGGREGATION_BASIS, "account_count": 2}
    assert [(item["account_id"], item["party_roles"], item["selected_report_id"], item["score"])
            for item in view["fico_account_audit"]] == [
                ("A1", ["borrower"], "R1", "760"), ("G1", ["guarantor"], "RG1", "651")]
    assert all(item["score_status"] == "VALID_PERSONAL_FICO" for item in view["fico_account_audit"])
    refs = set(criterion(view, "fico")["evidence_refs"])
    evidence = {item["id"]: item for item in view["source_evidence"]}
    assert refs <= evidence.keys()
    assert {evidence[ref]["record_id"] for ref in refs} >= {"R1", "RG1", "JG1", "L1"}
    assert "LATER" not in {evidence[ref]["record_id"] for ref in refs}
    assert not view["report_date_checked"] and not view["report_completion_checked"]


@pytest.mark.parametrize("scores,expected,status,label", [
    (("649", "650"), "649.5", "FAIL", "Decline"),
    (("650", "651"), "650.5", "PASS", "Refer"),
])
def test_multiple_guarantor_only_loans_keep_roles_and_other_borrower_checks_unknown(scores, expected, status, label):
    source = rows()
    source[1][1].pop("borrower_account_id")
    linked_party(source, "G1", scores[0])
    linked_party(source, "G2", scores[1])
    batch = evaluate(source)
    loan = batch.loan_results[0]
    view = loan.normalized_data["demo_decision"]
    assert loan.borrower_account_id is None
    assert view["selected_report_id"] is None
    assert view["normalized_facts"]["fico"] == expected
    assert criterion(view, "fico")["status"] == status
    assert view["label"] == label
    assert view["fico_aggregation"]["account_count"] == 2
    assert all(item["party_roles"] == ["guarantor"] for item in view["fico_account_audit"])
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    for key in ("public_record_code", "ebitda_usd"):
        assert criterion(view, key)["status"] == "UNKNOWN"
    assert criterion(view, "bankruptcy_clear")["status"] == "PASS"
    assert view["bankruptcy_count_basis"] == "ASSUMED_ZERO_WHEN_NOT_PROVIDED"
    assert "explicit_unique_borrower_account_relationship" in view["missing_inputs"]


def test_duplicate_associations_count_each_account_once_and_exclude_unknown_unrelated_roles():
    source = linked_party(rows(), score="640")
    source.extend([
        ("EntityInvolvement", {"Id": "JG1-duplicate", "loan_id": "L1", "account_id": "G1", "role": "guarantor"}),
        ("EntityInvolvement", {"Id": "JA1", "loan_id": "L1", "account_id": "A1", "role": "borrower"}),
        ("Account", {"Id": "UNRELATED"}),
        ("CreditReport", {"Id": "UNRELATED-R", "account_id": "UNRELATED", "personal_fico": "300"}),
    ])
    # Non-Individual accounts never supply FICO, whatever their role label.
    linked_party(source, "OWNER", "300", role="Owner", account_type="Business")
    linked_party(source, "UNTYPED", "300", role=None, account_type="Business")
    view = demo(source)
    assert view["normalized_facts"]["fico"] == "700"
    assert view["fico_aggregation"]["account_count"] == 2
    assert {item["account_id"] for item in view["fico_account_audit"]} == {"A1", "G1"}
    assert not {"UNRELATED-R", "ROWNER", "RUNTYPED"} & {item["record_id"] for item in view["source_evidence"]}


@pytest.mark.parametrize("score", [None, "", "   "])
def test_missing_linked_personal_scores_are_excluded_from_average(score):
    source = linked_party(rows(), score=score)
    linked_party(source, "G2", "650")
    view = demo(source)
    assert view["normalized_facts"]["fico"] == "705"
    assert criterion(view, "fico")["status"] == "PASS"
    assert view["fico_account_audit"][1]["score"] is None
    assert "fico" not in view["missing_inputs"]


def test_missing_borrower_score_uses_only_available_guarantor_scores():
    source = rows()
    source[2][1].pop("personal_fico")
    linked_party(source, "G1", "640")
    view = demo(source)
    assert view["normalized_facts"]["fico"] == "640"
    assert view["label"] == "Decline"


def test_all_missing_personal_scores_keep_average_unknown():
    source = rows()
    source[2][1].pop("personal_fico")
    linked_party(source, score=None)
    assert demo(source)["normalized_facts"]["fico"] is None


@pytest.mark.parametrize("account_type", ["Business", None, " "])
def test_fico_averages_only_individual_accounts(account_type):
    source = rows()
    if account_type is None:
        source[0][1].pop("account_type")
    else:
        source[0][1]["account_type"] = account_type
    linked_party(source, "G1", "640")
    linked_party(source, "G2", "300", account_type="Business")
    view = demo(source)
    assert view["normalized_facts"]["fico"] == "640"
    assert view["fico_aggregation"]["account_count"] == 1
    audit = {item["account_id"]: item for item in view["fico_account_audit"]}
    assert audit["A1"]["score_status"] == audit["G2"]["score_status"] == "EXCLUDED_NON_INDIVIDUAL_ACCOUNT"
    assert audit["A1"]["selected_report_id"] is None and audit["A1"]["score"] is None
    assert audit["G1"]["account_type"] == "Individual" and audit["G1"]["score"] == "640"


def test_fico_individual_type_is_case_insensitive_and_no_individual_keeps_unknown():
    source = rows()
    source[0][1]["account_type"] = "INDIVIDUAL"
    assert demo(source)["normalized_facts"]["fico"] == "760"
    source[0][1]["account_type"] = "Business"
    view = demo(source)
    assert view["normalized_facts"]["fico"] is None
    assert criterion(view, "fico")["status"] == "UNKNOWN"
    assert view["fico_aggregation"]["account_count"] == 0


SOQL_JUNCTION = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r."


def soql_business_with_individuals(individuals, q4_scores=None):
    """Query 1/2 SOQL rows: one Business borrower plus Individual rows by loan ID."""
    report = SOQL_JUNCTION + "nFUSE__Credit_Report__r."

    def q1_row(row_id, loan_id, account_id, role, account_type, score):
        return {"Id": row_id, SOQL_JUNCTION + "LLC_BI__Loan__c": loan_id,
                SOQL_JUNCTION + "LLC_BI__Account__c": account_id,
                SOQL_JUNCTION + "LLC_BI__Borrower_Type__c": role,
                SOQL_JUNCTION + "LLC_BI__Account__r.Type": account_type,
                report + "Id": "R" + account_id, report + "nFUSE__Credit_Score__c": score,
                "SCOC__CharacteristicKey__c": "uninterpreted", "SCOC__CharacteristicValue__c": "raw"}

    q1_rows = [q1_row("J0", "L1", "B1", "Borrower", "Business", "800")]
    q1_rows += [q1_row(f"J{index}", loan_id, account_id, role, "Individual", score)
                for index, (loan_id, account_id, role, score) in enumerate(individuals, 1)]
    q1 = document([("Data", row) for row in q1_rows], "MTB_Query_1_SOQL_Only.xlsx", "q1")
    q1.metadata["sheets"] = [{"name": "Data", "headers": list(q1_rows[0])}]
    q2_row = {"Id": "I1", SOQL_JUNCTION + "LLC_BI__Loan__c": "L1",
              SOQL_JUNCTION + "LLC_BI__Account__c": "B1",
              SOQL_JUNCTION + "LLC_BI__Borrower_Type__c": "Borrower",
              "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__LCResponse__r.SCOC__SBSS7Score__c": "200",
              "SCOC__IndicatorKey__c": "uninterpreted", "SCOC__IndicatorValue__c": "raw"}
    q2 = document([("Data", q2_row)], "MTB_Query_2_SOQL_Only.xlsx", "q2")
    q2.metadata["sheets"] = [{"name": "Data", "headers": list(q2_row)}]
    documents = [q1, q2]
    if q4_scores:
        q4_rows = [{"Id": f"Q4{account_id}", "nFUSE__Account__c": account_id, "nFUSE__Credit_Score__c": score}
                   for account_id, score in q4_scores]
        q4 = document([("Data", row) for row in q4_rows], "MTB_Query_4_SOQL_Only.xlsx", "q4")
        q4.metadata["sheets"] = [{"name": "Data", "headers": list(q4_rows[0])}]
        documents.append(q4)
    loan = next(item for item in evaluate_underwriting(documents, mode="TEST").loan_results
                if item.loan_id == "L1")
    return loan.normalized_data["demo_decision"]


def test_query4_fico_is_fallback_only_when_query1_has_no_score():
    view = soql_business_with_individuals(
        [("L1", "G1", "Guarantor", "700"), ("L1", "G2", "Guarantor", None)],
        q4_scores=[("G1", "300"), ("G2", "640"), ("B1", "100")])
    audit = {item["account_id"]: item for item in view["fico_account_audit"]}
    assert (audit["G1"]["score"], audit["G1"]["score_source"]) == ("700", "QUERY_1")
    assert (audit["G2"]["score"], audit["G2"]["score_source"]) == ("640", "QUERY_4")
    assert audit["G2"]["score_status"] == "VALID_QUERY4_FICO"
    assert audit["G2"]["selected_report_id"] == "Q4G2"
    assert audit["B1"]["score_status"] == "EXCLUDED_NON_INDIVIDUAL_ACCOUNT"
    assert view["normalized_facts"]["fico"] == "670"
    assert criterion(view, "fico")["status"] == "PASS"
    refs = set(criterion(view, "fico")["evidence_refs"])
    q4_values = {item["value"] for item in view["source_evidence"]
                 if item["id"] in refs and item["field"] == "nFUSE__Credit_Score__c"}
    assert q4_values == {"640"}


@pytest.mark.parametrize("q4_score,status,fico", [
    (None, "MISSING_PERSONAL_FICO", None),
    ("abc", "MISSING_OR_INVALID_PERSONAL_FICO", None),
    ("620", "VALID_QUERY4_FICO", "620"),
])
def test_query4_fico_fallback_missing_or_invalid(q4_score, status, fico):
    view = soql_business_with_individuals(
        [("L1", "G1", "Guarantor", None)],
        q4_scores=[("G1", q4_score)] if q4_score is not None else None)
    audit = {item["account_id"]: item for item in view["fico_account_audit"]}
    assert audit["G1"]["score_status"] == status
    assert view["normalized_facts"]["fico"] == fico


SBSS_HEADER = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__LCResponse__r.SCOC__SBSS7Score__c"


def soql_client_shape(borrower_role, q2_parties, q1_sbss=None, *, guarantor_role="Guarantor",
                      business_type="Business", guarantor_type="Individual"):
    """One Business account plus one Individual guarantor (FICO 696) on loan L1."""
    q1_sbss = q1_sbss or {}

    def q1_row(row_id, account_id, role, account_type, score):
        return {"Id": row_id, SOQL_JUNCTION + "LLC_BI__Loan__c": "L1", SBSS_HEADER: q1_sbss.get(account_id),
                SOQL_JUNCTION + "LLC_BI__Account__c": account_id,
                SOQL_JUNCTION + "LLC_BI__Borrower_Type__c": role,
                SOQL_JUNCTION + "LLC_BI__Account__r.Type": account_type,
                SOQL_JUNCTION + "nFUSE__Credit_Report__r.Id": "R" + account_id,
                SOQL_JUNCTION + "nFUSE__Credit_Report__r.nFUSE__Credit_Score__c": score,
                "SCOC__CharacteristicKey__c": "k", "SCOC__CharacteristicValue__c": "v"}

    q1_rows = [q1_row("J0", "B1", borrower_role, business_type, None),
               q1_row("J1", "G1", guarantor_role, guarantor_type, "696")]
    q2_rows = [{"Id": f"I{index}", SOQL_JUNCTION + "LLC_BI__Loan__c": "L1",
                SOQL_JUNCTION + "LLC_BI__Account__c": account_id,
                SOQL_JUNCTION + "LLC_BI__Borrower_Type__c": role, SBSS_HEADER: sbss,
                "SCOC__IndicatorKey__c": "thinFileIndicator", "SCOC__IndicatorValue__c": "N"}
               for index, (account_id, role, sbss) in enumerate(q2_parties)]
    q1 = document([("Data", row) for row in q1_rows], "MTB_Query_1.xlsx", "q1")
    q1.metadata["sheets"] = [{"name": "Data", "headers": list(q1_rows[0])}]
    q2 = document([("Data", row) for row in q2_rows], "MTB_Query_2.xlsx", "q2")
    q2.metadata["sheets"] = [{"name": "Data", "headers": list(q2_rows[0])}]
    loan = next(item for item in evaluate_underwriting([q1, q2], mode="TEST").loan_results
                if item.loan_id == "L1")
    return loan.normalized_data["demo_decision"]


@pytest.mark.parametrize("role", ["Primary Borrower", "Co-Borrower", None])
def test_business_account_linked_by_loan_id_is_borrower_without_borrower_role(role):
    view = soql_client_shape(role, [("G1", "Guarantor", "198")])
    assert view["borrower_link_basis"] == "BUSINESS_ACCOUNT_LINKED_BY_LOAN_ID"
    assert "explicit_unique_borrower_account_relationship" not in view["missing_inputs"]
    assert criterion(view, "bankruptcy_clear")["status"] == "PASS"
    assert view["normalized_facts"]["sbss"] == "198"
    assert view["label"] == "Approve"
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert bounded["borrower_link_basis"] == "BUSINESS_ACCOUNT_LINKED_BY_LOAN_ID"


@pytest.mark.parametrize("borrower_role,guarantor_role,business_type,basis", [
    # Individual co-borrower shares the Borrower role with the business.
    ("Borrower", "Borrower", "Business", "BUSINESS_ACCOUNT_AMONG_BORROWER_ROLES"),
    ("Borrower", "Borrower", "Limited Liability Company", "BUSINESS_ACCOUNT_AMONG_BORROWER_ROLES"),
    # Business entity Type other than the literal "Business".
    ("Co-Borrower", "Guarantor", "Corporation", "BUSINESS_ACCOUNT_LINKED_BY_LOAN_ID"),
])
def test_business_entity_is_borrower_when_borrower_role_is_shared_or_type_differs(
        borrower_role, guarantor_role, business_type, basis):
    view = soql_client_shape(borrower_role, [("G1", "Guarantor", "198")], guarantor_role=guarantor_role,
                             business_type=business_type)
    assert view["borrower_link_basis"] == basis
    assert "explicit_unique_borrower_account_relationship" not in view["missing_inputs"]
    assert criterion(view, "bankruptcy_clear")["status"] == "PASS"
    assert view["normalized_facts"]["fico"] == "696"
    assert view["label"] == "Approve"
    assert bounded_demo_context(view, mode="TEST", allowed_refs=set())["borrower_link_basis"] == basis


def test_two_individual_borrowers_without_business_stay_unresolved():
    view = soql_client_shape("Borrower", [("B1", "Borrower", "174")], guarantor_role="Borrower",
                             business_type="Individual")
    assert view["borrower_link_basis"] is None
    assert "explicit_unique_borrower_account_relationship" in view["missing_inputs"]
    assert view["label"] == "Refer"


def test_explicit_borrower_role_still_wins():
    view = soql_client_shape("Borrower", [("G1", "Guarantor", "198")])
    assert view["borrower_link_basis"] == "EXPLICIT_BORROWER_ROLE"
    assert view["sbss_basis"] == "SINGLE_SUPPLIED_SBSS"


@pytest.mark.parametrize("q1_sbss,q2_parties,sbss,basis", [
    # Query 1 borrower-row SBSS wins over Query 2.
    ({"B1": "150"}, [("B1", "Borrower", "198")], "150", "QUERY1_BORROWER_ROW_SBSS"),
    ({"B1": "205"}, [("G1", "Guarantor", "161")], "205", "QUERY1_BORROWER_ROW_SBSS"),
    # No Query 1 SBSS on the borrower's row: Query 2 applies.
    ({}, [("B1", "Borrower", "198")], "198", "SINGLE_SUPPLIED_SBSS"),
    ({"B1": "abc"}, [("B1", "Borrower", "198")], "198", "SINGLE_SUPPLIED_SBSS"),
    # An Individual's Query 1 SBSS is not the business borrower's score.
    ({"G1": "120"}, [("B1", "Borrower", "198")], "198", "SINGLE_SUPPLIED_SBSS"),
])
def test_sbss_uses_query1_borrower_row_first_then_query2(q1_sbss, q2_parties, sbss, basis):
    view = soql_client_shape("Borrower", q2_parties, q1_sbss=q1_sbss)
    assert view["normalized_facts"]["sbss"] == sbss
    assert view["sbss_basis"] == basis
    refs = set(criterion(view, "sbss")["evidence_refs"])
    evidence = {(item["workbook"], item["value"]) for item in view["source_evidence"] if item["id"] in refs}
    workbook = "MTB_Query_1.xlsx" if basis == "QUERY1_BORROWER_ROW_SBSS" else "MTB_Query_2.xlsx"
    assert evidence == {(workbook, sbss)}
    assert bounded_demo_context(view, mode="TEST", allowed_refs=set())["sbss_basis"] == basis


@pytest.mark.parametrize("parties,sbss,basis,label", [
    ([("B1", "Borrower", "190"), ("G1", "Guarantor", "161")], "190", "BORROWER_ROW_SBSS", "Approve"),
    ([("B1", "Borrower", "155"), ("G1", "Guarantor", "198")], "155", "BORROWER_ROW_SBSS", "Decline"),
    ([("G1", "Guarantor", "198"), ("G1", "Guarantor", "161")], "161", "LOWEST_OF_MULTIPLE_SBSS", "Refer"),
    ([("G1", "Guarantor", "205"), ("G1", "Guarantor", "198")], "198", "LOWEST_OF_MULTIPLE_SBSS", "Approve"),
])
def test_multiple_sbss_values_use_borrower_row_then_lowest(parties, sbss, basis, label):
    view = soql_client_shape("Borrower", parties)
    assert view["normalized_facts"]["sbss"] == sbss
    assert view["sbss_basis"] == basis
    assert "sbss" not in view["missing_inputs"]
    assert view["label"] == label
    refs = set(criterion(view, "sbss")["evidence_refs"])
    assert {item["value"] for item in view["source_evidence"] if item["id"] in refs} == {sbss}
    assert bounded_demo_context(view, mode="TEST", allowed_refs=set())["sbss_basis"] == basis


@pytest.mark.parametrize("role", ["Guarantor", "Personal Guarantor", "Co-Borrower", "Owner", None])
def test_individual_guarantors_connect_to_business_loan_by_loan_id(role):
    view = soql_business_with_individuals([("L1", "G1", role, "10.32")])
    assert view["normalized_facts"]["fico"] == "10.32"
    assert criterion(view, "fico")["status"] == "FAIL"
    assert view["label"] == "Decline"
    audit = {item["account_id"]: item for item in view["fico_account_audit"]}
    assert audit["B1"]["score_status"] == "EXCLUDED_NON_INDIVIDUAL_ACCOUNT"
    assert audit["G1"]["party_roles"] == ["guarantor"]
    assert audit["G1"]["account_type"] == "Individual"
    assert audit["G1"]["selected_report_id"] == "RG1"
    assert audit["G1"]["score_status"] == "VALID_PERSONAL_FICO"
    assert view["fico_aggregation"]["account_count"] == 1
    refs = set(criterion(view, "fico")["evidence_refs"])
    fields = {item["field"] for item in view["source_evidence"] if item["id"] in refs}
    assert SOQL_JUNCTION + "LLC_BI__Account__r.Type" in fields
    assert SOQL_JUNCTION + "nFUSE__Credit_Report__r.nFUSE__Credit_Score__c" in fields


def test_multiple_individual_guarantors_average_and_other_loans_are_ignored():
    view = soql_business_with_individuals([
        ("L1", "G1", "Guarantor", "700"),
        ("L1", "G2", "Personal Guarantor", "640"),
        ("L1", "G3", "Guarantor", None),
        ("L2", "G4", "Guarantor", "100"),
    ])
    assert view["normalized_facts"]["fico"] == "670"
    assert criterion(view, "fico")["status"] == "PASS"
    audit = {item["account_id"]: item["score_status"] for item in view["fico_account_audit"]}
    assert audit == {"B1": "EXCLUDED_NON_INDIVIDUAL_ACCOUNT", "G1": "VALID_PERSONAL_FICO",
                     "G2": "VALID_PERSONAL_FICO", "G3": "MISSING_PERSONAL_FICO"}
    assert view["fico_aggregation"]["account_count"] == 3


def test_individual_with_unrecognized_role_still_links_by_loan_id_in_canonical_rows():
    source = rows()
    source[0][1]["account_type"] = "Business"
    linked_party(source, "G1", "640", role="Personal Guarantor")
    view = demo(source)
    assert view["normalized_facts"]["fico"] == "640"
    assert view["label"] == "Decline"
    assert {item["account_id"]: item["party_roles"] for item in view["fico_account_audit"]} == {
        "A1": ["borrower"], "G1": ["guarantor"]}


def test_fico_skips_blank_reports_and_keeps_first_populated_report_per_account():
    source = rows()
    source[2][1].pop("personal_fico")
    source.extend([
        ("CreditReport", {"Id": "BLANK", "account_id": "A1", "personal_fico": " "}),
        ("CreditReport", {"Id": "SCORED", "account_id": "A1", "personal_fico": "94.57"}),
        ("CreditReport", {"Id": "LATER", "account_id": "A1", "personal_fico": "800"}),
    ])
    view = demo(source)
    assert view["normalized_facts"]["fico"] == "94.57"
    assert view["fico_account_audit"][0]["selected_report_id"] == "SCORED"
    assert criterion(view, "fico")["status"] == "FAIL"
    assert view["label"] == "Decline"
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert bounded["normalized_facts"]["fico"] == "94.57"


@pytest.mark.parametrize("score", ["0", "94.57", "299", "650.5"])
def test_supplied_numeric_fico_scores_use_existing_threshold_without_conversion(score):
    source = rows()
    source[2][1]["personal_fico"] = score
    view = demo(source)
    assert view["normalized_facts"]["fico"] == score
    assert criterion(view, "fico")["status"] == ("FAIL" if Decimal(score) < 650 else "PASS")


@pytest.mark.parametrize("score", ["bad", "-1", "851", True, "NaN", "Infinity"])
def test_invalid_linked_personal_score_blocks_partial_average_with_refs(score):
    view = demo(linked_party(rows(), score=score))
    assert view["normalized_facts"]["fico"] is None
    assert criterion(view, "fico")["status"] == "UNKNOWN"
    assert view["fico_aggregation"]["account_count"] == 2
    audit = view["fico_account_audit"]
    assert audit[0]["score"] == "760" and audit[1]["score"] is None
    refs = set(criterion(view, "fico")["evidence_refs"])
    assert set(audit[0]["evidence_refs"]) | set(audit[1]["evidence_refs"]) == refs
    evidence = {item["id"]: item for item in view["source_evidence"]}
    assert refs <= evidence.keys()
    assert {"R1", "JG1"} <= {evidence[ref]["record_id"] for ref in refs}


def test_conflicting_guarantor_score_no_fallback_to_generic_or_later_report():
    source = linked_party(rows(), score="650", fico="800", report_category="personal", report_role="guarantor")
    source.append(("CreditReport", {"Id": "RG1", "account_id": "G1", "personal_fico": "651"}))
    source.append(("CreditReport", {"Id": "LATER", "account_id": "G1", "personal_fico": "850"}))
    view = demo(source)
    assert criterion(view, "fico")["status"] == "UNKNOWN"
    assert view["fico_account_audit"][1]["score_status"] == "CONFLICTING_REPORT"
    assert len([item for item in view["source_evidence"]
                if item["record_id"] == "RG1" and item["field"] == "personal_fico"]) == 2


@pytest.mark.parametrize("category,role,expected", [
    ("personal", "guarantor", "705"), ("consumer", "GUARANTOR", "705"),
    ("personal_fico", "guarantor", "705"), ("business", "guarantor", None),
    ("personal", "borrower", None), ("personal", None, None), (None, "guarantor", None),
])
def test_generic_guarantor_score_requires_personal_category_and_matching_explicit_role(category, role, expected):
    source = linked_party(rows(), score="650", fico="650", report_category=category, report_role=role)
    source[-1][1].pop("personal_fico")
    view = demo(source)
    assert view["normalized_facts"]["fico"] == expected
    assert criterion(view, "fico")["status"] == ("PASS" if expected else "UNKNOWN")


def test_canonical_personal_score_is_explicitly_typed_without_role_or_category_inference():
    view = demo(linked_party(rows(), score="650", report_category="business", report_role="borrower"))
    assert view["normalized_facts"]["fico"] == "705"
    assert view["fico_account_audit"][1]["party_roles"] == ["guarantor"]


@pytest.mark.parametrize("field,value", [("report_category", "business"), ("report_role", "borrower")])
def test_conflicting_generic_report_typing_makes_whole_average_unknown(field, value):
    source = linked_party(rows(), score="650", fico="650", report_category="personal", report_role="guarantor")
    source[-1][1].pop("personal_fico")
    source.append(("CreditReport", {"Id": "RG1", "account_id": "G1", field: value}))
    view = demo(source)
    assert view["normalized_facts"]["fico"] is None
    assert view["fico_account_audit"][1]["score_status"] == "CONFLICTING_REPORT"
    refs = set(criterion(view, "fico")["evidence_refs"])
    assert len([item for item in view["source_evidence"] if item["id"] in refs
                and item["record_id"] == "RG1" and item["field"] == field]) == 2


def test_no_valid_linked_parties_has_no_invented_average():
    source = rows()
    source[1][1].pop("borrower_account_id")
    view = demo(source)
    assert view["normalized_facts"]["fico"] is None
    assert view["fico_account_audit"] == []
    assert view["fico_aggregation"]["account_count"] == 0
    assert criterion(view, "fico")["status"] == "UNKNOWN"
    assert view["age_account_audit"] == []
    assert view["age_aggregation"]["account_count"] == 0
    assert criterion(view, "age_years")["status"] == "UNKNOWN"


@pytest.mark.parametrize("underage_account", ["A1", "G1"])
def test_linked_underage_borrower_or_guarantor_is_nonblocking_diagnostic(underage_account):
    source = linked_party(rows())
    next(values for table, values in source
         if table == "Account" and values["Id"] == underage_account)["dob"] = "2008-10-21"
    view = demo(source)
    assert view["normalized_facts"]["age_years"] == 17
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    assert criterion(view, "age_years")["knockout"] is False
    assert view["label"] == "Approve"
    assert "below 18" not in view["rationale"]
    assert view["matched_reasons"] == []


@pytest.mark.parametrize("score,route", [("190", "SBA"), ("200", "CONVENTIONAL")])
@pytest.mark.parametrize("account_id", ["A1", "G1"])
@pytest.mark.parametrize("kind,status", [
    ("missing", "UNKNOWN"), ("invalid", "UNKNOWN"), ("under18", "OBSERVED"),
    ("conflicting_dob", "UNKNOWN"), ("conflicting_account", "UNKNOWN"),
])
def test_dob_diagnostics_never_block_either_route(score, route, account_id, kind, status, monkeypatch):
    provider = Mock(side_effect=AssertionError("DOB diagnostics must stay local"))
    monkeypatch.setattr(AnthropicLLM, "complete", provider)
    source = linked_party(rows())
    person = next(values for table, values in source if table == "Account" and values["Id"] == account_id)
    source[3][1]["sbss"] = score
    if kind == "missing":
        person.pop("dob")
    elif kind == "invalid":
        person["dob"] = "DOB_PRIVATE_SENTINEL"
    elif kind == "under18":
        person["dob"] = "2008-10-21"
    elif kind == "conflicting_dob":
        source.append(("Account", {"Id": account_id, "dob": "2008-10-21"}))
    else:
        person["name"] = "Original"
        source.append(("Account", {"Id": account_id, "name": "Conflicting"}))
    before = copy.deepcopy(source)
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert (view["label"], view["candidate_route"]) == ("Approve", route)
    assert criterion(view, "age_years")["status"] == status
    assert criterion(view, "age_years")["knockout"] is False
    assert view["missing_inputs"] == [] and view["matched_reasons"] == []
    assert "below 18" not in view["rationale"]
    assert {item["criterion"] for item in view["criteria"] if item["knockout"]} == {
        "bankruptcy_clear", "fico", "sbss"}
    audit = next(item for item in view["age_account_audit"] if item["account_id"] == account_id)
    assert audit["status"] == status and audit["knockout"] is False
    assert all(item["value"] is None for item in view["source_evidence"] if item["field"] == "dob")
    assert "DOB_PRIVATE_SENTINEL" not in json.dumps(view)
    assert "2008-10-21" not in json.dumps(view)
    saved = report.model_dump_json()
    for context in (demo_explanation_context(report), report_context(report)):
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert criterion(bounded, "age_years")["status"] == status
        assert criterion(bounded, "age_years")["knockout"] is False
        assert bounded["missing_inputs"] == [] and bounded["matched_reasons"] == []
        assert "DOB_PRIVATE_SENTINEL" not in json.dumps(context)
        assert "2008-10-21" not in json.dumps(context)
    assert source == before and report.model_dump_json() == saved
    provider.assert_not_called()


@pytest.mark.parametrize("mode", ["TEST", "PRODUCTION"])
@pytest.mark.parametrize("dob", [None, "invalid", "2008-10-21", "1980-01-01"])
def test_age_demo_exclusion_never_changes_strict_results(mode, dob, monkeypatch):
    source = rows()
    source[0][1]["dob"] = dob
    doc = document(source)
    before = copy.deepcopy(doc)
    actual = evaluate_underwriting([doc], mode=mode).model_dump()
    actual["loan_results"][0]["normalized_data"].pop("demo_decision", None)
    monkeypatch.setattr("backend.underwriting_engine.evaluate_demo", lambda *a, **k: None)
    strict = evaluate_underwriting([doc], mode=mode).model_dump()
    strict["loan_results"][0]["normalized_data"].pop("demo_decision", None)
    assert actual == strict and doc == before


@pytest.mark.parametrize("knockout", [None, False, True])
@pytest.mark.parametrize("status", ["FAIL", "PASS", "UNKNOWN", "OBSERVED"])
@pytest.mark.parametrize("age", [None, "invalid", True, 151, 17, 46])
def test_legacy_age_is_diagnostic_in_all_bounded_contexts_without_rewriting(knockout, status, age):
    report = demo_report()
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    view["normalized_facts"].update(age_years=age, dob="DOB_PRIVATE_SENTINEL", fico="620")
    check = criterion(view, "age_years")
    check.update(status=status, value="DOB_PRIVATE_SENTINEL")
    if knockout is None:
        check.pop("knockout")
    else:
        check["knockout"] = knockout
    view["label"] = "Decline"
    view["branch"] = "KNOWN_KNOCKOUT"
    view["rationale"] = "Age below 18: DOB_PRIVATE_SENTINEL"
    view["missing_inputs"] = ["age_years", "fico", "public_record_code", "ebitda_usd"]
    view["matched_reasons"] = [{"criterion": "age_years", "label": "Age below 18"}]
    before = report.model_dump_json()
    contexts = [bounded_demo_context(view, mode="TEST", allowed_refs=set())]
    contexts.extend(context["underwriting"]["loan_results"][0]["demo_decision"]
                    for context in (demo_explanation_context(report), report_context(report)))
    for bounded in contexts:
        age_check = criterion(bounded, "age_years")
        known = type(age) is int and 0 <= age <= 150
        assert age_check["status"] == ("OBSERVED" if known else "UNKNOWN")
        assert age_check["value"] == (age if known else None)
        assert age_check["knockout"] is False
        assert bounded["missing_inputs"] == ["fico"]
        assert {item["criterion"] for item in bounded["matched_reasons"]} == {"fico"}
        assert bounded["label"] == "Decline" and bounded["branch"] == "KNOWN_KNOCKOUT"
        assert "nonblocking diagnostics" in bounded["rationale"]
        assert "Historical labels are preserved" in bounded["rationale"]
        assert "Age below 18" not in json.dumps(bounded)
        assert "DOB_PRIVATE_SENTINEL" not in json.dumps(bounded)
    assert report.model_dump_json() == before


def test_guarantor_only_completed_ages_are_minimum_not_fractional_average():
    source = rows()
    source[1][1].pop("borrower_account_id")
    linked_party(source, "G1")
    linked_party(source, "G2")
    source[4][1]["dob"] = "2008-10-20"  # exactly 18
    source[7][1]["dob"] = "2007-10-20"  # 19; no 18.5 average
    loan = evaluate(source).loan_results[0]
    view = loan.normalized_data["demo_decision"]
    assert loan.borrower_account_id is None and view["selected_report_id"] is None
    assert view["normalized_facts"]["age_years"] == 18
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    assert [item["age_years"] for item in view["age_account_audit"]] == [18, 19]
    assert all(item["party_roles"] == ["guarantor"] for item in view["age_account_audit"])
    assert view["label"] == "Refer"
    assert all(criterion(view, key)["status"] == "UNKNOWN"
               for key in ("public_record_code", "ebitda_usd"))
    assert criterion(view, "bankruptcy_clear")["status"] == "PASS"


@pytest.mark.parametrize("dob,expected", [
    (None, "MISSING_OR_INVALID_DOB"), ("", "MISSING_OR_INVALID_DOB"),
    ("DOB_PRIVATE_SENTINEL", "MISSING_OR_INVALID_DOB"),
    (True, "MISSING_OR_INVALID_DOB"), ("2008-02-30", "MISSING_OR_INVALID_DOB"),
    ("1980-01-01T00:00:00Z", "MISSING_OR_INVALID_DOB"),
    ("2027-01-01", "FUTURE_DOB"), ("1875-10-20", "AGE_OUT_OF_RANGE"),
])
def test_unresolved_account_age_never_borrows_another_dob(dob, expected):
    source = linked_party(rows())
    source[4][1]["dob"] = dob
    source[1][1]["dob"] = "1980-01-01"  # Loan DOB is never a fallback.
    source[2][1]["dob"] = "1980-01-01"
    view = demo(source)
    assert view["normalized_facts"]["age_years"] is None
    assert criterion(view, "age_years")["status"] == "UNKNOWN"
    assert view["age_account_audit"][1]["age_status"] == expected
    assert view["age_account_audit"][0]["age_years"] == 46
    assert "age_years" not in view["missing_inputs"] and view["label"] == "Approve"
    assert "DOB_PRIVATE_SENTINEL" not in json.dumps(view)


@pytest.mark.parametrize("as_of", [" ", "invalid", "2026-02-30", "2026-10-20T12:00:00Z", True])
def test_invalid_supplied_loan_date_never_uses_today_or_report_timestamp(as_of, demo_clock):
    source = linked_party(rows())
    source[1][1]["as_of_date"] = as_of
    source[0][1]["as_of_date"] = "2026-10-20"
    source[2][1].update(report_date="2026-10-20", CreatedDate="2026-10-20T12:00:00Z")
    view = demo(source)
    assert view["normalized_facts"]["age_years"] is None
    assert criterion(view, "age_years")["status"] == "UNKNOWN"
    assert all(item["age_status"] == "MISSING_OR_INVALID_AS_OF_DATE"
               for item in view["age_account_audit"])
    assert view["age_reference_date"] is None
    assert view["age_reference_date_basis"] == "UNAVAILABLE"
    demo_clock.now.assert_not_called()


@pytest.mark.parametrize("absent", ["omitted", "null", "blank"])
def test_absent_date_uses_recorded_utc_date_for_all_linked_ages_only(absent, demo_clock):
    source = linked_party(rows())
    if absent == "omitted":
        source[1][1].pop("as_of_date")
    else:
        source[1][1]["as_of_date"] = None if absent == "null" else ""
    source[0][1]["as_of_date"] = "2000-01-01"  # not the loan date
    source[4][1]["dob"] = "2008-10-20"
    source[2][1].update(report_date="2030-01-01", status="pending")
    view = demo(source)
    assert view["label"] == "Approve"
    assert view["normalized_facts"]["age_years"] == 18
    assert [item["age_years"] for item in view["age_account_audit"]] == [46, 18]
    assert view["reference_date"] is None and view["reference_date_basis"] == "UNAVAILABLE"
    assert view["age_reference_date"] == "2026-10-20"
    assert view["age_reference_date_basis"] == "CURRENT_UTC_DATE_FOR_AGE"
    assert view["selected_report_id"] == "R1"
    assert not view["report_date_checked"] and not view["report_completion_checked"]
    demo_clock.now.assert_called_once_with(timezone.utc)


def test_explicit_date_wins_over_injection_and_clock(demo_clock):
    config = load_underwriting_config()
    graph = build_entity_graph([document(rows())], config)
    view = evaluate_demo(graph, graph.table("Loan")["L1"], config, mode="TEST",
                         response=graph.table("LCRESPONSE")["S1"], age_reference_date=date(2040, 1, 1))
    assert view["age_reference_date"] == view["reference_date"] == "2026-10-20"
    assert view["age_reference_date_basis"] == "EXPLICIT_APPLICATION_AS_OF_DATE"
    assert view["normalized_facts"]["age_years"] == 46
    demo_clock.now.assert_not_called()


def test_age_date_is_injectable_without_clock_or_graph_mutation(demo_clock):
    source = rows()
    source[1][1].pop("as_of_date")
    source[0][1]["dob"] = "2008-10-21"
    config = load_underwriting_config()
    graph = build_entity_graph([document(source)], config)
    before = copy.deepcopy(graph)
    view = evaluate_demo(graph, graph.table("Loan")["L1"], config, mode="TEST",
                         response=graph.table("LCRESPONSE")["S1"], age_reference_date=date(2026, 10, 20))
    assert view["normalized_facts"]["age_years"] == 17
    assert view["label"] == "Approve"
    assert view["age_reference_date"] == "2026-10-20"
    assert view["age_reference_date_basis"] == "CURRENT_UTC_DATE_FOR_AGE"
    assert graph == before
    demo_clock.now.assert_not_called()


def test_utc_midnight_boundary_and_saved_fallback_remain_stable(demo_clock):
    source = rows()
    source[1][1].pop("as_of_date")
    source[0][1]["dob"] = "2008-10-21"
    # Local date is still October 20; UTC has reached the eighteenth birthday.
    instant = datetime(2026, 10, 20, 17, 30, tzinfo=timezone(timedelta(hours=-7)))
    demo_clock.now.side_effect = lambda tz: instant.astimezone(tz)
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert view["normalized_facts"]["age_years"] == 18 and view["label"] == "Approve"
    assert view["age_reference_date"] == "2026-10-21"
    demo_clock.now.assert_called_once_with(timezone.utc)
    saved = report.model_dump_json()
    restored = Report.model_validate_json(saved)
    demo_clock.now.side_effect = AssertionError("Saved context must not consult today's clock")
    for context in (demo_explanation_context(restored), report_context(restored)):
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert bounded["age_reference_date"] == "2026-10-21"
        assert bounded["age_reference_date_basis"] == "CURRENT_UTC_DATE_FOR_AGE"
        assert bounded["reference_date"] is None
        assert bounded["normalized_facts"]["age_years"] == 18
    assert restored.model_dump_json() == saved


def test_absent_date_partial_age_keeps_known_underage_and_unresolved_party():
    source = linked_party(rows())
    source[1][1].pop("as_of_date")
    source[0][1]["dob"] = "2008-10-21"
    source[4][1]["dob"] = None
    view = demo(source)
    assert view["normalized_facts"]["age_years"] == 17
    assert view["label"] == "Approve"
    assert "age_years" not in view["missing_inputs"]
    assert {item["status"] for item in view["age_account_audit"]} == {"OBSERVED", "UNKNOWN"}


@pytest.mark.parametrize("unknown_first", [False, True])
def test_underage_and_missing_party_stay_diagnostic_in_bounded_context_with_complete_audit(unknown_first):
    source = linked_party(rows())
    source[0][1]["dob"] = None if unknown_first else "2008-10-21"
    source[4][1]["dob"] = "2008-10-21" if unknown_first else None
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert view["normalized_facts"]["age_years"] == 17
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    assert {item["status"] for item in view["age_account_audit"]} == {"OBSERVED", "UNKNOWN"}
    assert "age_years" not in view["missing_inputs"]
    refs = set(criterion(view, "age_years")["evidence_refs"])
    assert refs == {ref for item in view["age_account_audit"] for ref in item["evidence_refs"]}
    evidence = {item["id"]: item for item in view["source_evidence"]}
    assert refs <= evidence.keys()
    assert {evidence[ref]["record_id"] for ref in refs} >= {"A1", "G1", "L1", "JG1"}
    assert all(item["value"] is None for item in evidence.values() if item["field"] == "dob")
    criterion(view, "age_years")["status"] = "UNKNOWN"  # Known ages remain observations, not failures.
    view["age_aggregation"]["raw"] = "DOB_PRIVATE_SENTINEL"
    view["age_account_audit"][0]["dob"] = "DOB_PRIVATE_SENTINEL"
    before = report.model_dump_json()
    for context in (demo_explanation_context(report), report_context(report)):
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert criterion(bounded, "age_years")["status"] == "OBSERVED"
        assert criterion(bounded, "age_years")["knockout"] is False
        assert "age_years" not in bounded["missing_inputs"]
        assert bounded["age_aggregation"] == {"basis": AGE_AGGREGATION_BASIS, "account_count": 2}
        assert bounded["matched_reasons"] == []
        assert "age_account_audit" not in bounded and "source_evidence" not in bounded
        assert "DOB_PRIVATE_SENTINEL" not in json.dumps(context)
        assert "2008-10-21" not in json.dumps(context)
    assert before == report.model_dump_json()


def test_duplicate_accounts_roles_and_unrelated_children_do_not_change_age_scope():
    source = linked_party(rows())
    source.extend([
        ("Account", {"Id": "G1", "dob": "1980-01-01"}),
        ("EntityInvolvement", {"Id": "JG1-copy", "loan_id": "L1", "account_id": "G1", "role": "guarantor"}),
        ("EntityInvolvement", {"Id": "JA1", "loan_id": "L1", "account_id": "A1", "role": "guarantor"}),
        ("Account", {"Id": "CHILD", "dob": "2020-01-01"}),
        ("Loan", {"Id": "L2", "borrower_account_id": "CHILD"}),
    ])
    linked_party(source, "OWNER", role="Owner", account_type="Business")
    linked_party(source, "UNTYPED", role=None, account_type="Business")
    view = demo(source)
    assert view["normalized_facts"]["age_years"] == 46
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    assert view["age_aggregation"]["account_count"] == 2
    assert [(item["account_id"], item["party_roles"]) for item in view["age_account_audit"]] == [
        ("A1", ["borrower", "guarantor"]), ("G1", ["guarantor"])]
    assert not {"CHILD", "OWNER", "UNTYPED"} & {item["record_id"] for item in view["source_evidence"]}


@pytest.mark.parametrize("field,value", [("dob", "2000-01-01"), ("name", "Other")])
def test_conflicting_account_or_dob_is_unknown_and_keeps_conflict_evidence(field, value):
    source = linked_party(rows())
    if field == "name":
        source[4][1]["name"] = "Original"
    source.append(("Account", {"Id": "G1", field: value}))
    view = demo(source)
    assert criterion(view, "age_years")["status"] == "UNKNOWN"
    assert view["age_account_audit"][1]["age_status"] == "CONFLICTING_ACCOUNT"
    refs = set(view["age_account_audit"][1]["evidence_refs"])
    assert len([item for item in view["source_evidence"]
                if item["id"] in refs and item["record_id"] == "G1" and item["field"] == field]) == 2


def test_conflicting_loan_asof_does_not_choose_a_convenient_date(demo_clock):
    source = linked_party(rows())
    source.append(("Loan", {"Id": "L1", "as_of_date": "2026-10-21"}))
    view = demo(source)
    assert criterion(view, "age_years")["status"] == "UNKNOWN"
    assert view["reference_date"] is None
    assert view["age_reference_date"] is None
    demo_clock.now.assert_not_called()


def test_unresolved_date_alias_evidence_never_allows_injected_fallback(demo_clock):
    config = load_underwriting_config()
    graph = build_entity_graph([document(rows())], config)
    loan = graph.table("Loan")["L1"]
    # Conflicting aliases can leave field evidence without a resolved value.
    loan.fields.pop("as_of_date")
    view = evaluate_demo(graph, loan, config, mode="TEST", age_reference_date=date(2040, 1, 1))
    assert criterion(view, "age_years")["status"] == "UNKNOWN"
    assert view["age_reference_date_basis"] == "UNAVAILABLE"
    demo_clock.now.assert_not_called()


def test_missing_borrower_dob_never_uses_loan_extension_or_guarantor_dob():
    source = linked_party(rows())
    source[0][1].pop("dob")
    source[1][1]["dob"] = "1980-01-01"
    source.append(("LoanExtension", {"Id": "X1", "loan_id": "L1", "dob": "1980-01-01"}))
    view = demo(source)
    assert view["normalized_facts"]["age_years"] is None
    assert view["age_account_audit"][0]["age_status"] == "MISSING_OR_INVALID_DOB"
    assert view["age_account_audit"][1]["status"] == "OBSERVED"
    assert criterion(view, "age_years")["status"] == "UNKNOWN"


def test_minimum_known_underage_survives_multiple_unknown_accounts():
    source = linked_party(rows())
    source[0][1]["dob"] = None
    source[4][1]["dob"] = "2009-10-20"  # 17
    linked_party(source, "G2")
    source[7][1]["dob"] = "2010-10-20"  # 16
    linked_party(source, "G3")
    source[10][1]["dob"] = "invalid"
    view = demo(source)
    assert view["normalized_facts"]["age_years"] == 16
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    assert len(view["age_account_audit"]) == 4
    assert sum(item["status"] == "UNKNOWN" for item in view["age_account_audit"]) == 2
    assert "age_years" not in view["missing_inputs"]


def test_completed_age_upper_bound_150_is_valid_and_context_reconciles_stale_fail():
    source = rows()
    source[0][1]["dob"] = "1876-10-20"
    view = demo(source)
    assert view["normalized_facts"]["age_years"] == 150
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    criterion(view, "age_years")["status"] = "FAIL"
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert criterion(bounded, "age_years")["status"] == "OBSERVED"


@pytest.mark.parametrize("field,value", [("role", "Owner"), ("account_id", "OTHER")])
def test_conflicting_junction_cannot_link_unrelated_child(field, value):
    source = rows() + [
        ("Account", {"Id": "CHILD", "dob": "2020-01-01"}),
        ("Account", {"Id": "OTHER", "dob": "2020-01-01"}),
        ("EntityInvolvement", {"Id": "JC", "loan_id": "L1", "account_id": "CHILD", "role": "guarantor"}),
        ("EntityInvolvement", {"Id": "JC", field: value}),
    ]
    view = demo(source)
    assert [item["account_id"] for item in view["age_account_audit"]] == ["A1"]
    assert criterion(view, "age_years")["status"] == "OBSERVED"


@pytest.mark.parametrize("as_of,age", [
    ("2026-02-27", 17), ("2026-02-28", 18),
    ("2024-02-28", 15), ("2024-02-29", 16),
])
def test_leap_birthday_completed_calendar_age(as_of, age):
    source = rows()
    source[0][1]["dob"] = "2008-02-29"
    source[1][1]["as_of_date"] = as_of
    view = demo(source)
    assert view["normalized_facts"]["age_years"] == age
    assert criterion(view, "age_years")["status"] == "OBSERVED"


def test_linked_soql_account_dob_is_used_with_normalized_provenance_and_no_raw_dob():
    prefix = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r."
    dob_field = prefix + "LLC_BI__Account__r.Birth_Date__c"
    values = {"Id": "JG1", prefix + "LLC_BI__Loan__c": "L1",
              prefix + "LLC_BI__Account__c": "G1", prefix + "LLC_BI__Borrower_Type__c": "Guarantor",
              dob_field: "2008-10-21", "SCOC__CharacteristicKey__c": "uninterpreted",
              "SCOC__CharacteristicValue__c": "not a DOB"}
    q1 = document([("Data", values)], "MTB_Query_1_SOQL_Only.xlsx", "soql")
    q1.metadata["sheets"] = [{"name": "Data", "headers": list(values)}]
    originals = copy.deepcopy(q1.metadata)
    loan = evaluate_underwriting([document(rows()), q1], mode="TEST").loan_results[0]
    view = loan.normalized_data["demo_decision"]
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    assert view["normalized_facts"]["age_years"] == 17
    assert any(item["field"] == dob_field and item["value"] is None
               for item in view["source_evidence"])
    assert "2008-10-21" not in json.dumps(view)
    assert q1.metadata == originals


def test_age_bounded_metadata_legacy_invalid_values_and_known_observations():
    view = demo()
    view.pop("age_aggregation")
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert "age_aggregation" not in bounded and bounded["normalized_facts"]["age_years"] == 46
    for aggregate in ({"basis": "UNTRUSTED", "account_count": 1},
                      *({"basis": AGE_AGGREGATION_BASIS, "account_count": count}
                        for count in (True, -1, 1_000_001, "2"))):
        view["age_aggregation"] = aggregate
        assert "age_aggregation" not in bounded_demo_context(view, mode="TEST", allowed_refs=set())
    for invalid in (151, -1, 18.5, True, "18"):
        view["normalized_facts"]["age_years"] = invalid
        bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
        assert "age_years" not in bounded["normalized_facts"]
        assert criterion(bounded, "age_years")["status"] == "UNKNOWN"
    view["normalized_facts"]["age_years"] = 46
    criterion(view, "age_years")["status"] = "UNKNOWN"
    assert criterion(bounded_demo_context(view, mode="TEST", allowed_refs=set()), "age_years")["status"] == "OBSERVED"


@pytest.mark.parametrize("scores,expected,status,branch", [
    (("649", "650"), "649.5", "FAIL", "KNOWN_KNOCKOUT"),
    (("650", "651"), "650.5", "PASS", "SBSS_160_189_AMOUNT_LE_100K"),
    (("759", "760"), "759.5", "PASS", "SBSS_160_189_AMOUNT_LE_100K"),
    (("760", "761"), "760.5", "PASS", "SBSS_160_189_OWNERSHIP_LT_2_FICO_GE_760"),
])
def test_fractional_average_thresholds_and_bounded_context(scores, expected, status, branch):
    source = linked_party(rows(), score=scores[1])
    source[0][1]["ownership_years"] = "1"
    source[2][1]["personal_fico"] = scores[0]
    source[3][1]["sbss"] = "175"
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert view["normalized_facts"]["fico"] == expected
    assert view["branch"] == branch
    # Stale saved statuses are reconciled against the fractional aggregate.
    criterion(view, "fico")["status"] = "FAIL" if status == "PASS" else "PASS"
    view["fico_aggregation"]["raw"] = "RAW_PRIVATE_SENTINEL"
    view["fico_account_audit"][0]["account_id"] = "PRIVATE_ACCOUNT_SENTINEL"
    for context in (demo_explanation_context(report), report_context(report)):
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert bounded["normalized_facts"]["fico"] == expected
        assert criterion(bounded, "fico")["status"] == status
        assert bounded["fico_aggregation"] == {"basis": FICO_AGGREGATION_BASIS, "account_count": 2}
        serialized = json.dumps(bounded)
        assert "PRIVATE_ACCOUNT_SENTINEL" not in serialized and "RAW_PRIVATE_SENTINEL" not in serialized
        assert "fico_account_audit" not in bounded and "source_evidence" not in bounded


def test_legacy_context_does_not_guess_aggregation_and_metadata_is_allowlisted():
    view = demo()
    view.pop("fico_aggregation")
    view.pop("fico_account_audit")
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert bounded["normalized_facts"]["fico"] == "760"
    assert "fico_aggregation" not in bounded
    for aggregate in ({"basis": "UNTRUSTED", "account_count": 2},
                      {"basis": FICO_AGGREGATION_BASIS, "account_count": True},
                      {"basis": FICO_AGGREGATION_BASIS, "account_count": -1},
                      {"basis": FICO_AGGREGATION_BASIS, "account_count": 1_000_001}):
        view["fico_aggregation"] = aggregate
        assert "fico_aggregation" not in bounded_demo_context(view, mode="TEST", allowed_refs=set())
    view["normalized_facts"]["fico"] = "900.5"
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert "fico" not in bounded["normalized_facts"]
    assert criterion(bounded, "fico")["status"] == "UNKNOWN"


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
    assert criterion(view, "age_years")["status"] == "OBSERVED"
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
                     "naics": "541100", "ebitda": "1", "account_type": "Individual"}),
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


def test_missing_asof_uses_age_fallback_but_not_for_positive_bankruptcy_or_selection():
    source = rows()
    source[1][1].pop("as_of_date")
    source[2][1].pop("report_date")
    view = demo(source)
    assert view["label"] == "Approve"
    assert view["selected_report_id"] == "R1"
    assert criterion(view, "bankruptcy_clear")["status"] == "PASS"
    assert criterion(view, "fico")["status"] == "PASS"
    assert criterion(view, "age_years")["status"] == "OBSERVED"
    assert "as-of" in criterion(view, "age_years")["detail"]
    for discharge in (None, "2021-10-20", "2025-01-01", "invalid"):
        source[2][1].update(bankruptcy_count="1", bankruptcy_discharge_date=discharge)
        positive = demo(source)
        assert criterion(positive, "bankruptcy_clear")["status"] == "UNKNOWN"
        assert criterion(positive, "age_years")["status"] == "OBSERVED"
        assert positive["label"] == "Refer"
        assert "bankruptcy_clear" in positive["missing_inputs"]
        assert positive["matched_reasons"] == []


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


@pytest.mark.parametrize("value,canonical,high_risk", [
    (541100, "541100", False), (541100.0, "541100", False),
    (Decimal("541100.00"), "541100", False), (" 541100 ", "541100", False),
    ("99999999", "99999999", False), ("not-a-code", "not-a-code", False),
    ("=not-a-code", "=not-a-code", False), ("1", "1", False),
    ("23", "23", True), (236220, "236220", True),
    (445110.0, "445110", True), (" 4451 ", "4451", True),
    ("45anything", "45anything", True), ("451110", "451110", True),
    ("x" * 200, "x" * 128, False),
])
def test_demo_naics_uses_only_supplied_high_risk_prefixes(value, canonical, high_risk, monkeypatch):
    provider = Mock(side_effect=AssertionError("NAICS evaluation must stay local"))
    monkeypatch.setattr(AnthropicLLM, "complete", provider)
    source = rows()
    source[0][1]["naics"] = value
    original = copy.deepcopy(source)
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert view["normalized_facts"]["naics"] == canonical
    assert view["normalized_facts"]["high_risk_naics"] is high_risk
    assert view["label"] == "Approve"
    assert view["candidate_route"] == ("SBA" if high_risk else "CONVENTIONAL")
    assert view["branch"].endswith("HIGH_RISK_NAICS" if high_risk else "OTHER_NAICS")
    assert criterion(view, "naics")["status"] == "OBSERVED"
    assert "naics_or_listed_high_risk_prefixes" not in view["missing_inputs"]
    refs = criterion(view, "naics")["evidence_refs"]
    assert any(item["id"] in refs and item["value"] == str(value) for item in view["source_evidence"])
    for context in (demo_explanation_context(report), report_context(report)):
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert bounded["normalized_facts"]["high_risk_naics"] is high_risk
        assert bounded["naics_match_basis"] == "SUPPLIED_HIGH_RISK_PREFIX_LIST_ONLY"
        assert criterion(bounded, "naics")["status"] == "OBSERVED"
        assert criterion(bounded, "naics")["value"] == (canonical if canonical.isascii() and canonical.isdigit() else None)
    assert source == original
    provider.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "  ", True, False, 541100.5,
                                       float("nan"), float("inf"), Decimal("NaN"),
                                       Decimal("Infinity"), {"code": "541100"}, ["541100"]])
def test_demo_naics_unusable_scalar_stays_unknown(value):
    source = rows()
    source[0][1]["naics"] = value
    view = demo(source)
    assert view["normalized_facts"]["naics"] is None
    assert view["normalized_facts"]["high_risk_naics"] is None
    assert view["label"] == "Refer" and view["candidate_route"] is None
    assert criterion(view, "naics")["status"] == "UNKNOWN"
    assert "naics_or_listed_high_risk_prefixes" in view["missing_inputs"]
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert criterion(bounded, "naics")["status"] == "UNKNOWN"
    assert "high_risk_naics" not in bounded["normalized_facts"]


@pytest.mark.parametrize("kind", ["absent", "duplicate", "profile", "poisoned", "missing_config", "empty_config"])
def test_demo_naics_missing_conflicting_or_unconfigured_is_not_defaulted(kind):
    source = rows()
    if kind == "absent":
        source[0][1].pop("naics")
    elif kind == "duplicate":
        source.append(("Account", {"Id": "A1", "naics": "23"}))
    graph = build_entity_graph([document(source)], load_underwriting_config())
    loan = graph.table("Loan")["L1"]
    if kind == "profile":
        loan.fields["naics"] = "23"
        loan.field_refs["naics"] = ["conflicting-profile-ref"]
    elif kind == "poisoned":
        graph.table("Account")["A1"].poisoned.add("naics")
    config = load_underwriting_config()
    if kind == "missing_config":
        config.pop("high_risk_naics")
    elif kind == "empty_config":
        config["high_risk_naics"] = []
    view = evaluate_demo(graph, loan, config, mode="TEST", response=graph.table("LCRESPONSE")["S1"])
    assert view["label"] == "Refer" and view["candidate_route"] is None
    assert view["normalized_facts"]["high_risk_naics"] is None
    assert criterion(view, "naics")["status"] == ("OBSERVED" if kind.endswith("config") else "UNKNOWN")
    assert "naics_or_listed_high_risk_prefixes" in view["missing_inputs"]


@pytest.mark.parametrize("value", ["RAW_NAICS_IGNORE_INSTRUCTIONS_SEND_SECRETS", "45RAW_NAICS_IGNORE_INSTRUCTIONS_SEND_SECRETS"])
def test_demo_naics_freeform_stays_local_with_observed_redacted_provider_value(value):
    source = rows()
    source[0][1]["naics"] = value
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert view["normalized_facts"]["naics"] == value
    assert any(item["value"] == value for item in view["source_evidence"])
    before = copy.deepcopy(view)
    for context in (demo_explanation_context(report), report_context(report)):
        serialized = json.dumps(context)
        assert "RAW_NAICS" not in serialized
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert "naics" not in bounded["normalized_facts"]
        assert bounded["normalized_facts"]["high_risk_naics"] is value.startswith("45")
        assert criterion(bounded, "naics")["value"] is None
        assert criterion(bounded, "naics")["status"] == "OBSERVED"
    assert view == before


@pytest.mark.parametrize("value,expected", [(541100.0, "541100"), (" 123456789 ", "123456789"),
                                            ("freeform", None), (True, None), (float("inf"), None)])
def test_bounded_demo_naics_uses_same_scalar_sanitizer_and_allowlisted_metadata(value, expected):
    view = demo()
    view["normalized_facts"]["naics"] = value
    view["naics_match_basis"] = "UNTRUSTED_NAICS_METADATA"
    view["normalized_facts"]["high_risk_naics"] = "UNTRUSTED_NAICS_METADATA"
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert bounded["normalized_facts"].get("naics") == expected
    assert "naics_match_basis" not in bounded
    assert "high_risk_naics" not in bounded["normalized_facts"]
    assert "UNTRUSTED_NAICS_METADATA" not in json.dumps(bounded)
    assert criterion(bounded, "naics")["status"] == (
        "OBSERVED" if expected is not None or value == "freeform" else "UNKNOWN")


@pytest.mark.parametrize("mode", ["TEST", "PRODUCTION"])
@pytest.mark.parametrize("value", [541100.0, "not-a-code", "45freeform", None])
def test_demo_naics_never_changes_strict_trace_or_production(mode, value, monkeypatch):
    source = rows()
    source[0][1]["naics"] = value
    actual = evaluate(source, mode=mode).model_dump()
    view = actual["loan_results"][0]["normalized_data"].pop("demo_decision", None)
    assert (view is None) == (mode == "PRODUCTION")
    monkeypatch.setattr("backend.underwriting_engine.evaluate_demo", lambda *a, **k: None)
    strict = evaluate(source, mode=mode).model_dump()
    strict["loan_results"][0]["normalized_data"].pop("demo_decision", None)
    assert actual == strict


@pytest.mark.parametrize("score", ["160", "189", "200", "250"])
@pytest.mark.parametrize("value,basis", [
    ("absent", "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED"),
    (None, "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED"),
    ("", "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED"),
    ("0", "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS"),
    ("1", "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS"),
    ("2", "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS"),
])
def test_demo_ownership_absence_zero_and_boundaries(score, value, basis, monkeypatch):
    provider = Mock(side_effect=AssertionError("Ownership evaluation must stay local"))
    monkeypatch.setattr(AnthropicLLM, "complete", provider)
    source = rows()
    source[0][1].pop("ownership_years")
    if value != "absent":
        source[0][1]["ownership_years"] = value
    source[3][1]["sbss"] = score
    original = copy.deepcopy(source)
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assumed = basis == "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED"
    expected = "0" if assumed else value
    assert view["normalized_facts"]["ownership_years"] == expected
    assert view["ownership_years_basis"] == basis
    assert criterion(view, "ownership_years")["status"] == "OBSERVED"
    assert "ownership_years" not in view["missing_inputs"]
    assert view["label"] == "Approve"
    assert view["candidate_route"] == ("CONVENTIONAL" if int(score) >= 200 and value == "2" else "SBA")
    assert ("OWNERSHIP_GE_2" in view["branch"]) == (value == "2")
    refs = criterion(view, "ownership_years")["evidence_refs"]
    assert bool(refs) is not assumed
    if assumed:
        assert not any(item["field"] == "ownership_years" for item in view["source_evidence"])
        assert "User-defined" in criterion(view, "ownership_years")["detail"]
    for context in (demo_explanation_context(report), report_context(report)):
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert bounded["normalized_facts"]["ownership_years"] == expected
        assert bounded["ownership_years_basis"] == basis
        assert "ownership_years" not in bounded["missing_inputs"]
    assert source == original
    provider.assert_not_called()


@pytest.mark.parametrize("score", ["175", "200"])
@pytest.mark.parametrize("value", ["-1", True, False, "bad", "NaN", "Infinity", "   "])
def test_supplied_invalid_ownership_never_defaults(score, value):
    source = rows()
    source[0][1]["ownership_years"] = value
    source[3][1]["sbss"] = score
    view = demo(source)
    assert view["label"] == "Refer"
    assert view["normalized_facts"]["ownership_years"] is None
    assert view["ownership_years_basis"] == "UNAVAILABLE_OR_INVALID"
    assert criterion(view, "ownership_years")["status"] == "UNKNOWN"
    assert criterion(view, "ownership_years")["evidence_refs"]
    assert "ownership_years" in view["missing_inputs"]


@pytest.mark.parametrize("kind", ["same_account_conflict", "profile_conflict", "poisoned_absent", "supplied_null"])
def test_unusable_ownership_graph_evidence_never_defaults(kind):
    source = rows()
    if kind == "same_account_conflict":
        source.append(("Account", {"Id": "A1", "ownership_years": "0"}))
    elif kind == "profile_conflict":
        source.append(("LoanExtension", {"Id": "X1", "loan_id": "L1", "ownership_years": "0"}))
    else:
        source[0][1].pop("ownership_years")
    config = load_underwriting_config()
    graph = build_entity_graph([document(source)], config)
    account = graph.table("Account")["A1"]
    if kind == "poisoned_absent":
        account.poisoned.add("ownership_years")
    elif kind == "supplied_null":
        account.fields["ownership_years"] = None
        account.field_refs["ownership_years"] = ["supplied-null-formula"]
    before = copy.deepcopy(graph)
    view = evaluate_demo(graph, graph.table("Loan")["L1"], config, mode="TEST",
                         response=graph.table("LCRESPONSE")["S1"])
    assert view["normalized_facts"]["ownership_years"] is None
    assert view["ownership_years_basis"] == "UNAVAILABLE_OR_INVALID"
    assert criterion(view, "ownership_years")["status"] == "UNKNOWN"
    assert "ownership_years" in view["missing_inputs"]
    assert graph == before


@pytest.mark.parametrize("scope", ["Loan", "LoanExtension"])
def test_ownership_keeps_existing_profile_scopes(scope):
    source = rows()
    source[0][1].pop("ownership_years")
    if scope == "LoanExtension":
        source.append((scope, {"Id": "X1", "loan_id": "L1", "ownership_years": "2"}))
    config = load_underwriting_config()
    graph = build_entity_graph([document(source)], config)
    if scope == "Loan":
        # Loan is an existing evaluator scope, not a canonical workbook alias.
        graph.table("Loan")["L1"].fields["ownership_years"] = "2"
        graph.table("Loan")["L1"].field_refs["ownership_years"] = ["loan-ownership"]
    view = evaluate_demo(graph, graph.table("Loan")["L1"], config, mode="TEST",
                         response=graph.table("LCRESPONSE")["S1"])
    assert view["normalized_facts"]["ownership_years"] == "2"
    assert view["ownership_years_basis"] == "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS"
    assert view["candidate_route"] == "CONVENTIONAL"


def test_ownership_fallback_never_substitutes_guarantor_or_clears_identity():
    source = linked_party(rows())
    source[0][1].pop("ownership_years")
    source[1][1].pop("borrower_account_id")
    next(values for table, values in source if table == "Account" and values["Id"] == "G1")["ownership_years"] = "10"
    view = demo(source)
    assert view["normalized_facts"]["ownership_years"] == "0"
    assert view["ownership_years_basis"] == "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED"
    assert criterion(view, "ownership_years")["evidence_refs"] == []
    assert view["label"] == "Refer" and view["candidate_route"] is None
    assert "explicit_unique_borrower_account_relationship" in view["missing_inputs"]
    assert criterion(view, "bankruptcy_clear")["status"] == "PASS"
    assert view["bankruptcy_count_basis"] == "ASSUMED_ZERO_WHEN_NOT_PROVIDED"


@pytest.mark.parametrize("value", [None, "0", "1", "2", "bad", True, "-1"])
@pytest.mark.parametrize("mode", ["TEST", "PRODUCTION"])
def test_ownership_demo_never_changes_documents_graph_or_strict_result(value, mode, monkeypatch):
    source = rows()
    source[0][1]["ownership_years"] = value
    doc = document(source)
    before_doc = copy.deepcopy(doc)
    config = load_underwriting_config()
    graph = build_entity_graph([doc], config)
    before_graph = copy.deepcopy(graph)
    evaluate_demo(graph, graph.table("Loan")["L1"], config, mode=mode)
    assert graph == before_graph and doc == before_doc
    actual = evaluate_underwriting([doc], mode=mode).model_dump()
    monkeypatch.setattr("backend.underwriting_engine.evaluate_demo", lambda *a, **k: None)
    strict = evaluate_underwriting([doc], mode=mode).model_dump()
    for payload in (actual, strict):
        payload["loan_results"][0]["normalized_data"].pop("demo_decision", None)
    assert actual == strict and doc == before_doc


@pytest.mark.parametrize("score,fico,amount,label,branch", [
    ("160", "759", "100000", "Approve", "SBSS_160_189_AMOUNT_LE_100K"),
    ("189", "759", "100001", "Refer", "REQUEST_THRESHOLD_CONFLICT"),
    ("175", "759", "100000001", "Decline", "SBSS_160_189_AMOUNT_GT_100M"),
    ("200", "650", "100000", "Approve", "SBSS_GE_200_OWNERSHIP_LT_2"),
])
def test_assumed_ownership_uses_existing_fico_and_amount_branches(score, fico, amount, label, branch):
    source = rows()
    source[0][1].pop("ownership_years")
    source[1][1]["requested_amount"] = amount
    source[2][1]["personal_fico"] = fico
    source[3][1]["sbss"] = score
    view = demo(source)
    assert (view["label"], view["branch"]) == (label, branch)
    assert "ownership_years" not in view["missing_inputs"]


@pytest.mark.parametrize("value,basis,expected_basis", [
    ("0", "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED", "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED"),
    ("2", "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED", None),
    ("0", "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS", "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS"),
    (None, "UNAVAILABLE_OR_INVALID", "UNAVAILABLE_OR_INVALID"),
    ("-1", "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS", None),
    (True, "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS", None),
    ("0", "RAW_BASIS_SENTINEL", None),
    ("0", None, None),
])
def test_bounded_ownership_basis_is_allowlisted_consistent_and_not_inferred(value, basis, expected_basis):
    view = demo()
    view["normalized_facts"]["ownership_years"] = value
    if basis is None:
        view.pop("ownership_years_basis")
    else:
        view["ownership_years_basis"] = basis
    before = copy.deepcopy(view)
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert bounded.get("ownership_years_basis") == expected_basis
    expected_value = value if type(value) is str and value in {"0", "2"} else None
    assert bounded["normalized_facts"].get("ownership_years") == expected_value
    assert "RAW_BASIS_SENTINEL" not in json.dumps(bounded)
    assert view == before


def test_vehicle_flag_defaults_false_when_not_provided_and_no_outside_tree_gates():
    source = rows()
    for field in ("requested_path", "tier_i", "tier_ii", "is_investment_re", "vehicle_only"):
        source[1][1].pop(field)
    assert demo(source)["label"] == "Approve"  # conventional, outside gates irrelevant
    source[3][1]["sbss"] = "190"
    view = demo(source)
    assert view["label"] == "Approve"
    assert view["candidate_route"] == "SBA" and view["branch"] == "SBSS_190_199"
    assert view["normalized_facts"]["vehicle_only"] is False
    assert view["vehicle_only_basis"] == "ASSUMED_FALSE_WHEN_NOT_PROVIDED"
    assert criterion(view, "vehicle_only")["evidence_refs"] == []
    assert "vehicle_only" not in view["missing_inputs"]
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert bounded["normalized_facts"]["vehicle_only"] is False
    assert bounded["vehicle_only_basis"] == "ASSUMED_FALSE_WHEN_NOT_PROVIDED"


@pytest.mark.parametrize("value,label,branch,basis", [
    ("", "Approve", "SBSS_190_199", "ASSUMED_FALSE_WHEN_NOT_PROVIDED"),
    ("  ", "Approve", "SBSS_190_199", "ASSUMED_FALSE_WHEN_NOT_PROVIDED"),
    ("N", "Approve", "SBSS_190_199", "EXPLICIT_VEHICLE_ONLY_FLAG"),
    ("Y", "Approve", "SBSS_190_199_VEHICLE_ONLY_OVERRIDE", "EXPLICIT_VEHICLE_ONLY_FLAG"),
    ("maybe", "Refer", "SBSS_190_199", "INVALID_OR_CONFLICTING"),
])
def test_vehicle_flag_blank_defaults_false_but_invalid_stays_unknown(value, label, branch, basis):
    source = rows()
    source[1][1]["vehicle_only"] = value
    source[3][1]["sbss"] = "190"
    view = demo(source)
    assert (view["label"], view["branch"], view["vehicle_only_basis"]) == (label, branch, basis)
    assert ("vehicle_only" in view["missing_inputs"]) is (label == "Refer")


@pytest.mark.parametrize("value,status,basis", [
    (None, "PASS", "ASSUMED_ZERO_WHEN_NOT_PROVIDED"),
    ("", "PASS", "ASSUMED_ZERO_WHEN_NOT_PROVIDED"),
    ("0", "PASS", "EXPLICIT_BANKRUPTCY_COUNT"),
    ("1", "UNKNOWN", "EXPLICIT_BANKRUPTCY_COUNT"),
    ("abc", "UNKNOWN", "INVALID_OR_CONFLICTING"),
])
def test_bankruptcy_count_defaults_to_zero_when_not_provided(value, status, basis):
    source = rows()
    if value is None:
        source[2][1].pop("bankruptcy_count")
    else:
        source[2][1]["bankruptcy_count"] = value
    view = demo(source)
    assert criterion(view, "bankruptcy_clear")["status"] == status
    assert view["bankruptcy_count_basis"] == basis
    assert ("bankruptcy_clear" in view["missing_inputs"]) is (status == "UNKNOWN")
    if basis == "ASSUMED_ZERO_WHEN_NOT_PROVIDED":
        assert view["normalized_facts"]["bankruptcy_count"] == "0"
        assert view["label"] == "Approve"
        bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
        assert bounded["bankruptcy_count_basis"] == basis
        assert bounded["normalized_facts"]["bankruptcy_clear"] is True


@pytest.mark.parametrize("field,value", [("dob", None)])
def test_missing_dob_is_not_a_knockout_input(field, value):
    source = rows()
    source[0][1][field] = value
    source[0][1]["SBSS7_earn_before_int_and_taxes__c"] = "100"
    assert demo(source)["label"] == "Approve"


@pytest.mark.parametrize("field,value", [("ebitda", "-0.01"), ("dob", "2008-10-21")])
def test_account_diagnostics_never_shortcircuit_missing_report(field, value):
    source = [row for row in rows() if row[0] != "CreditReport"]
    source[0][1][field] = value
    assert demo(source)["label"] == "Refer"


@pytest.mark.parametrize("score,route", [("190", "SBA"), ("200", "CONVENTIONAL")])
@pytest.mark.parametrize("ebitda,status", [
    (None, "UNKNOWN"), ("", "UNKNOWN"), ("invalid", "UNKNOWN"),
    ("NaN", "UNKNOWN"), ("Infinity", "UNKNOWN"), (True, "UNKNOWN"),
    ("-0.01", "OBSERVED"), ("0.00", "OBSERVED"), ("100", "OBSERVED"),
])
def test_ebitda_diagnostic_never_blocks_either_demo_route(score, route, ebitda, status, monkeypatch):
    provider = Mock(side_effect=AssertionError("Deterministic demo must not call a provider"))
    monkeypatch.setattr(AnthropicLLM, "complete", provider)
    source = rows()
    source[0][1]["ebitda"] = ebitda
    source[0][1].update(EBIT__c="100", Business_Net_Income__c="100")
    source[3][1]["sbss"] = score
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    assert (view["label"], view["candidate_route"]) == ("Approve", route)
    assert criterion(view, "ebitda_usd")["status"] == status
    assert criterion(view, "ebitda_usd")["knockout"] is False
    assert view["normalized_facts"]["ebitda_usd"] == (ebitda if status == "OBSERVED" else None)
    assert "ebitda_usd" not in view["missing_inputs"]
    assert view["matched_reasons"] == []
    assert {item["criterion"] for item in view["criteria"] if item["knockout"]} == {
        "bankruptcy_clear", "fico", "sbss"}
    assert "three knockout checks passed" in view["rationale"]
    for context in (demo_explanation_context(report), report_context(report)):
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert criterion(bounded, "ebitda_usd")["status"] == status
        assert criterion(bounded, "ebitda_usd")["knockout"] is False
        assert bounded["matched_reasons"] == []
        assert "ebitda_usd" not in bounded["missing_inputs"]
    provider.assert_not_called()


@pytest.mark.parametrize("score,route", [("190", "SBA"), ("200", "CONVENTIONAL")])
def test_omitted_ebitda_and_public_records_allow_otherwise_complete_demo(score, route):
    source = rows()
    source[0][1].pop("ebitda")
    source[2][1].pop("max_delinquency_public_records")
    source[3][1]["sbss"] = score
    view = demo(source)
    assert (view["label"], view["candidate_route"]) == ("Approve", route)
    assert view["missing_inputs"] == [] and view["matched_reasons"] == []
    for key in ("ebitda_usd", "public_record_code"):
        assert criterion(view, key)["status"] == "UNKNOWN"
        assert criterion(view, key)["knockout"] is False


def test_conflicting_explicit_ebitda_is_unknown_nonblocking_with_evidence():
    source = rows() + [("LoanExtension", {"Id": "X1", "loan_id": "L1", "ebitda": "-1"})]
    view = demo(source)
    assert view["label"] == "Approve"
    check = criterion(view, "ebitda_usd")
    assert check["status"] == "UNKNOWN" and check["knockout"] is False
    assert len(check["evidence_refs"]) == 2
    assert set(check["evidence_refs"]) <= {item["id"] for item in view["source_evidence"]}
    assert "ebitda_usd" not in view["missing_inputs"] and view["matched_reasons"] == []


@pytest.mark.parametrize("knockout", [None, False, True])
@pytest.mark.parametrize("ebitda", [None, "bad", "-1", "0", "100"])
def test_legacy_ebitda_fail_is_diagnostic_in_prompt_context_without_rewriting(knockout, ebitda):
    report = demo_report()
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    view["normalized_facts"]["ebitda_usd"] = ebitda
    check = criterion(view, "ebitda_usd")
    check["status"] = "FAIL"
    if knockout is None:
        check.pop("knockout")
    else:
        check["knockout"] = knockout
    view["label"] = "Decline"
    view["missing_inputs"] = ["ebitda_usd", "age_years"]
    view["matched_reasons"] = [{"criterion": "ebitda_usd", "code": "EXPENSES_EXCEED_INCOME"}]
    before = report.model_dump_json()
    contexts = [bounded_demo_context(view, mode="TEST", allowed_refs=set())]
    contexts.extend(context["underwriting"]["loan_results"][0]["demo_decision"]
                    for context in (demo_explanation_context(report), report_context(report)))
    for bounded in contexts:
        assert criterion(bounded, "ebitda_usd")["status"] == (
            "UNKNOWN" if ebitda in {None, "bad"} else "OBSERVED")
        assert criterion(bounded, "ebitda_usd")["knockout"] is False
        assert bounded["matched_reasons"] == []
        assert bounded["missing_inputs"] == []
        assert bounded["label"] == "Decline"  # Reprocessing, not explanation, changes saved labels.
        assert "EXPENSES_EXCEED_INCOME" not in json.dumps(bounded)
    assert report.model_dump_json() == before


@pytest.mark.parametrize("code", [None, "", "bad", "4", "0", "1", "2", "3"])
def test_public_records_are_nonblocking_diagnostics_never_matched_reasons(code):
    source = rows()
    source[2][1]["max_delinquency_public_records"] = code
    report = demo_report(source)
    view = report.underwriting.loan_results[0].normalized_data["demo_decision"]
    expected = "OBSERVED" if code in {"0", "1", "2", "3"} else "UNKNOWN"
    assert criterion(view, "public_record_code")["status"] == expected
    assert criterion(view, "public_record_code")["knockout"] is False
    assert "public_record_code" not in view["missing_inputs"]
    assert view["matched_reasons"] == [] and view["label"] == "Approve"
    assert "three knockout checks passed" in view["rationale"]
    for context in (demo_explanation_context(report), report_context(report)):
        bounded = context["underwriting"]["loan_results"][0]["demo_decision"]
        assert criterion(bounded, "public_record_code")["status"] == expected
        assert criterion(bounded, "public_record_code")["knockout"] is False
        assert bounded["matched_reasons"] == [] and bounded["label"] == "Approve"
    source[0][1].pop("dob")
    assert demo(source)["label"] == "Approve"


@pytest.mark.parametrize("knockout", [None, False, True])
@pytest.mark.parametrize("code", [None, "0", "1", "2", "3"])
def test_stale_saved_public_status_is_diagnostic_in_bounded_context_without_rewriting(knockout, code):
    view = demo()
    view["normalized_facts"]["public_record_code"] = code
    check = criterion(view, "public_record_code")
    check["status"] = "FAIL"
    if knockout is None:
        check.pop("knockout")
    else:
        check["knockout"] = knockout
    view["label"] = "Decline"  # Historical labels are not retroactively recomputed.
    view["missing_inputs"] = ["public_record_code", "age_years"]
    view["matched_reasons"] = [{"criterion": "public_record_code", "status": "MATCHED"}]
    before = copy.deepcopy(view)
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert criterion(bounded, "public_record_code")["status"] == ("OBSERVED" if code else "UNKNOWN")
    assert criterion(bounded, "public_record_code")["knockout"] is False
    assert bounded["matched_reasons"] == []
    assert bounded["missing_inputs"] == []
    assert bounded["label"] == "Decline"
    assert view == before


def test_bounded_context_respects_explicit_nonknockout_when_matching_reasons():
    view = demo()
    view["normalized_facts"]["sbss"] = "155"
    check = criterion(view, "sbss")
    check.update(knockout=False, status="OBSERVED")
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert criterion(bounded, "sbss")["status"] == "OBSERVED"
    assert bounded["matched_reasons"] == []
    check["status"] = "FAIL"
    bounded = bounded_demo_context(view, mode="TEST", allowed_refs=set())
    assert criterion(bounded, "sbss")["status"] == "OBSERVED"
    assert bounded["matched_reasons"] == []


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


@pytest.mark.parametrize("ownership", ["absent", None, "", "0", "1", "2"])
@pytest.mark.parametrize("score", ["160", "189", "190", "200"])
def test_actual_source_shaped_soql_without_ebitda_approves_locally(score, ownership, monkeypatch):
    """Synthetic cells with actual source headers; no uploads, main app or provider needed."""
    provider = Mock(side_effect=AssertionError("Source fixture evaluation must stay local"))
    monkeypatch.setattr(AnthropicLLM, "complete", provider)
    junction = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r."
    sbss_field = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__LCResponse__r.SCOC__SBSS7Score__c"
    relationship = {junction + "LLC_BI__Loan__c": "L1",
                    junction + "LLC_BI__Account__c": "A1",
                    junction + "LLC_BI__Borrower_Type__c": "Borrower"}
    values = {
        1: {"Id": "J1", **relationship,
            junction + "LLC_BI__Account__r.Birth_Date__c": "1980-10-20",
            junction + "LLC_BI__Account__r.Type": "Individual",
            junction + "nFUSE__Credit_Report__r.Id": "R1",
            junction + "nFUSE__Credit_Report__r.nFUSE__Credit_Score__c": "740",
            junction + "LLC_BI__Account__r.Number_of_Years_as_Current_Owner__c": "2",
            junction + "LLC_BI__Account__r.NAICS_Code_Formula__c": "541100",
            junction + "LLC_BI__Account__r.Business_Net_Income__c": "-100",
            junction + "LLC_BI__Account__r.SBSS7_earn_before_int_and_taxes__c": "100",
            "SCOC__CharacteristicKey__c": "uninterpreted", "SCOC__CharacteristicValue__c": "raw"},
        2: {"Id": "I1", **relationship, sbss_field: score,
            "SCOC__IndicatorKey__c": "uninterpreted", "SCOC__IndicatorValue__c": "raw"},
        # Query 4's score is never a FICO source; this low decoy is ignored.
        4: {"Id": "R1", "nFUSE__Account__c": "A1", "nFUSE__Credit_Score__c": "300",
            "nFUSE__Report_Category__c": "personal", "LLC_BI__Borrower_Type__c": "Borrower",
            "nFUSE__Number_Of_Bankruptcies__c": "0"},
    }
    ownership_field = junction + "LLC_BI__Account__r.Number_of_Years_as_Current_Owner__c"
    if ownership == "absent":
        values[1].pop(ownership_field)
    else:
        values[1][ownership_field] = ownership
    documents = []
    for query, cells in values.items():
        doc = document([("Data", cells)], f"MTB_Query_{query}_SOQL_Only.xlsx", f"q{query}")
        doc.metadata["sheets"] = [{"name": "Data", "headers": list(cells)}]
        documents.append(doc)
    # Explicit branch flag supplements exports; no synthetic EBITDA or dates.
    documents.append(document([("Loan", {"Id": "L1", "vehicle_only": False,
                                         "requested_amount": "100000"})], document_id="branch"))
    before = copy.deepcopy(documents)
    loan = evaluate_underwriting(documents, mode="TEST").loan_results[0]
    view = loan.normalized_data["demo_decision"]
    route = "CONVENTIONAL" if score == "200" and ownership == "2" else "SBA"
    assert (view["label"], view["candidate_route"]) == ("Approve", route)
    assumed = ownership in {"absent", None, ""}
    assert view["normalized_facts"]["ownership_years"] == ("0" if assumed else ownership)
    assert view["ownership_years_basis"] == (
        "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED" if assumed else "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS")
    ownership_refs = criterion(view, "ownership_years")["evidence_refs"]
    assert bool(ownership_refs) is not assumed
    assert {item["field"] for item in view["source_evidence"] if item["id"] in ownership_refs} == (
        set() if assumed else {ownership_field})
    assert view["missing_inputs"] == [] and view["matched_reasons"] == []
    assert view["normalized_facts"]["ebitda_usd"] is None
    assert criterion(view, "ebitda_usd")["status"] == "UNKNOWN"
    assert criterion(view, "ebitda_usd")["knockout"] is False
    assert str(loan.normalized_amounts_usd["business_net_income_usd"]) == "-100"
    assert loan.normalized_amounts_usd["ebitda_usd"] is None
    assert view["age_reference_date_basis"] == "CURRENT_UTC_DATE_FOR_AGE"
    assert view["selected_report_id"] == "R1" and view["reference_date"] is None
    assert documents == before
    provider.assert_not_called()


@pytest.mark.parametrize("parties", ["borrower", "borrower_guarantor", "guarantors_only"])
@pytest.mark.parametrize("as_of", ["2026-10-20", None, "invalid"])
@pytest.mark.parametrize("public_code", [None, "2"])
def test_graph_documents_and_strict_result_are_immutable(monkeypatch, parties, as_of, public_code):
    source_rows = rows()
    source_rows[1][1]["as_of_date"] = as_of
    source_rows[2][1]["max_delinquency_public_records"] = public_code
    if parties != "borrower":
        linked_party(source_rows, "G1", "650")
    if parties == "guarantors_only":
        source_rows[1][1].pop("borrower_account_id")
        linked_party(source_rows, "G2", "651")
    source = document(source_rows)
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


@pytest.mark.parametrize("ebitda,status", [(None, "UNKNOWN"), ("-1", "FAIL"), ("bad", "UNKNOWN")])
@pytest.mark.parametrize("mode", ["TEST", "PRODUCTION"])
def test_ebitda_demo_exclusion_never_changes_strict_results(ebitda, status, mode, monkeypatch):
    source = rows()
    source[0][1]["ebitda"] = ebitda
    actual = evaluate(source, mode=mode).model_dump()
    loan = actual["loan_results"][0]
    assert loan["normalized_data"]["ebitda_nonnegative"] is (False if ebitda == "-1" else None)
    assert next(item for item in loan["rule_results"]
                if item["rule_id"] == "EBITDA_NONNEGATIVE_DIAGNOSTIC")["status"] == (
                    status if actual["source_available"] else "UNKNOWN")
    view = loan["normalized_data"].pop("demo_decision", None)
    assert (view is None) == (mode == "PRODUCTION")
    if view is not None:
        assert view["label"] == "Approve" and view["matched_reasons"] == []
    monkeypatch.setattr("backend.underwriting_engine.evaluate_demo", lambda *a, **k: None)
    strict = evaluate(source, mode=mode).model_dump()
    strict["loan_results"][0]["normalized_data"].pop("demo_decision", None)
    assert actual == strict


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
    refs = set(criterion(view, "fico")["evidence_refs"])
    assert len([item for item in view["source_evidence"]
                if item["id"] in refs and item["field"] == "personal_fico"]) == 2


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
    ("fico", "personal_fico", "620", "Average personal FICO below 650", "SUPPLIED_STRATEGY"),
    ("sbss", "sbss", "155", "SBSS below 160", "SUPPLIED_STRATEGY"),
])
def test_demo_matched_reason_labels_and_provenance(criterion_key, field, value, label, provenance):
    source = rows()
    node = 3 if field == "sbss" else 2
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
        ("Account", {"Id": "A2", "account_type": "Individual"}),
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