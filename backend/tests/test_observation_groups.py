"""Synthetic parent-scoped observation fixtures; no provider or persisted data."""

import copy
import json
from decimal import Decimal

import pytest

from backend.entity_graph import build_entity_graph
from backend.llm import report_context
from backend.models import Report
from backend.underwriting_engine import evaluate_underwriting, load_underwriting_config
from backend.tests.test_underwriting import document, evaluate, observations, rows


def grouped_rows():
    source = rows() + observations()
    source[1][1]["response_id"] = "S1"
    next(fields for table, fields in source if table == "DataSourceAnalysis").update(
        response_id="S1", report_id="R1")
    source.extend([
        ("CreditReport", {"Id": "R2", "account_id": "A1", "status": "completed",
                          "report_date": "2026-10-18", "personal_fico": "650"}),
        ("LCRESPONSE", {"Id": "S2", "loan_id": "L1", "sbss": "100"}),
        ("LCREntity", {"Id": "E2", "account_id": "A1", "response_id": "S2"}),
        ("DataSourceAnalysis", {"Id": "D2", "entity_id": "E2", "response_id": "S2", "report_id": "R2"}),
        ("Characteristic", {"Id": "C2", "analysis_id": "D2", "key": "FLAG", "value": "N"}),
    ])
    return source


def test_parent_groups_isolate_same_key_responses_reports_and_keep_selected_arrays():
    result = evaluate(grouped_rows())
    groups = result.normalized_data["observation_groups"]
    assert [(g["response_id"], g["report_id"], g["analysis_id"]) for g in groups] == [
        ("S1", "R1", "D1"), ("S2", "R2", "D2")]
    assert [[v["value"] for v in g["characteristics"]] for g in groups] == [["false"], ["N"]]
    assert [v["record_id"] for v in result.normalized_data["characteristics"]] == ["C1"]
    assert result.normalized_data["sbss"] == "200"
    assert result.normalized_data["fico"] == "760"
    assert not any("CONFLICTING_CHARACTERISTIC" in issue for issue in result.conflicts)
    evidence = {e.id: e for e in result.source_evidence}
    for group in groups:
        assert group["evidence_refs"] and set(group["evidence_refs"]) <= evidence.keys()
        for item in group["characteristics"]:
            assert {key: item[key] for key in ("account_id", "response_id", "entity_id", "analysis_id", "report_id")} == {
                key: group[key] for key in ("account_id", "response_id", "entity_id", "analysis_id", "report_id")}
            assert any(evidence[ref].field == "value" and evidence[ref].value == item["value"]
                       for ref in item["evidence_refs"])


def test_same_response_different_reports_stay_separate_and_unselected_report_cannot_pivot():
    source = grouped_rows()
    next(fields for table, fields in source if table == "LCREntity" and fields["Id"] == "E2")["response_id"] = "S1"
    next(fields for table, fields in source if table == "DataSourceAnalysis" and fields["Id"] == "D2")["response_id"] = "S1"
    next(fields for table, fields in source if table == "Characteristic" and fields["Id"] == "C2").update(key="OLD", value="0")
    result = evaluate(source, config={"mapping_manifest": {"characteristic_feature_mapping": {"OLD": "ownership_years"}}})
    assert len(result.normalized_data["observation_groups"]) == 2
    assert len(result.normalized_data["characteristics"]) == 1
    assert result.normalized_data["ownership_years"] == "2"


def test_ambiguous_responses_keep_groups_but_never_supply_selected_observations():
    source = grouped_rows()
    source[1][1].pop("response_id")
    result = evaluate(source)
    assert len(result.normalized_data["observation_groups"]) == 2
    assert result.normalized_data["selected_response_id"] is None
    assert result.normalized_data["characteristics"] == []
    assert result.normalized_data["indicators"] == []


def test_same_account_other_loan_response_never_enters_groups():
    source = grouped_rows() + [("Loan", {"Id": "L2", "borrower_account_id": "A1"})]
    next(fields for table, fields in source if table == "LCRESPONSE" and fields["Id"] == "S2")["loan_id"] = "L2"
    result = evaluate(source)
    assert [g["response_id"] for g in result.normalized_data["observation_groups"]] == ["S1"]
    assert all(e.record_id != "C2" for e in result.source_evidence)


def test_historical_response_cannot_override_feature_mapping():
    source = grouped_rows()
    next(fields for table, fields in source if table == "Characteristic" and fields["Id"] == "C2").update(key="OLD", value="0")
    result = evaluate(source, config={"mapping_manifest": {"characteristic_feature_mapping": {"OLD": "ownership_years"}}})
    assert result.normalized_data["ownership_years"] == "2"
    assert result.normalized_data["ownership_at_least_2_years"] is True


@pytest.mark.parametrize("raw,expected", [
    ("N", "N"), ("19960700", "19960700"), ("false", "false"), ("0005", "0005"),
    ("  0005  ", "  0005  "), (19960700, "19960700"), (5.0, "5.0"),
    (0.125, "0.125"), (Decimal("5.000"), "5.000"), (False, "false"),
])
def test_unknown_values_remain_lexical_with_source_provenance(raw, expected):
    source = rows() + observations()
    next(fields for table, fields in source if table == "Characteristic").update(
        key="UNRECOGNIZED", value=raw)
    result = evaluate(source)
    group = result.normalized_data["observation_groups"][0]
    value = group["characteristics"][0]
    assert value["value"] == expected
    assert isinstance(value["value"], str)
    assert group["report_id"] is None  # R1 exists on A1; it is NOT an explicit analysis FK.
    assert any(e.field == "value" and e.value == expected and e.id in value["evidence_refs"]
               and e.record_id == "C1" and e.workbook == "canonical.xlsx"
               and e.sheet == "Characteristic" and e.row > 1 for e in result.source_evidence)
    assert "UNRECOGNIZED" not in result.normalized_data
    baseline = evaluate(rows())
    assert result.normalized_amounts_usd == baseline.normalized_amounts_usd
    assert [(r.rule_id, r.status) for r in result.rule_results] == [(r.rule_id, r.status) for r in baseline.rule_results]
    assert json.loads(result.model_dump_json())["normalized_data"]["observation_groups"][0]["characteristics"][0]["value"] == expected


@pytest.mark.parametrize("field,value,issue", [
    ("response_id", "S2", "MISMATCHED_ANALYSIS_PARENT"),
    ("response_id", "missing", "ORPHAN_RELATION:DataSourceAnalysis:response_id"),
    ("report_id", "missing", "ORPHAN_RELATION:DataSourceAnalysis:report_id"),
    ("report_id", 123, "INVALID_RELATION_ID:DataSourceAnalysis:report_id"),
    ("report_id", {"Id": "R1"}, "INVALID_RELATION_ID:DataSourceAnalysis:report_id"),
])
def test_invalid_explicit_parents_hold_and_never_fall_back(field, value, issue):
    source = grouped_rows()
    next(fields for table, fields in source if table == "DataSourceAnalysis" and fields["Id"] == "D1")[field] = value
    result = evaluate(source, mode="PRODUCTION")
    assert any(issue in item for item in result.conflicts)
    assert result.recommendation_path == "DATA_QUALITY_HOLD"
    assert not result.normalized_data["characteristics"]
    assert all(g["analysis_id"] != "D1" for g in result.normalized_data["observation_groups"])


def test_analysis_report_must_match_entity_account_not_just_exist():
    source = grouped_rows() + [("Account", {"Id": "A2"})]
    next(fields for table, fields in source if table == "CreditReport" and fields["Id"] == "R1")["account_id"] = "A2"
    result = evaluate(source, mode="PRODUCTION")
    assert any("MISMATCHED_ANALYSIS_PARENT" in issue for issue in result.conflicts)
    assert not result.normalized_data["characteristics"]


@pytest.mark.parametrize("field,alias,other", [("report_id", "CREDIT_REPORT_ID", "R2"),
                                               ("response_id", "LCResponse_ID", "S2")])
def test_conflicting_parent_aliases_poison_even_with_previous_valid_copy(field, alias, other):
    source = grouped_rows()
    duplicate = copy.deepcopy(next(fields for table, fields in source if table == "DataSourceAnalysis" and fields["Id"] == "D1"))
    duplicate[alias] = other
    source.append(("DataSourceAnalysis", duplicate))
    result = evaluate(source, mode="PRODUCTION")
    assert any(f"CONFLICTING_ALIASES:DataSourceAnalysis:{field}" in issue for issue in result.conflicts)
    assert result.recommendation_path == "DATA_QUALITY_HOLD"
    assert not result.normalized_data["characteristics"]


def test_unscoped_observations_stay_in_graph_without_fabricated_group():
    doc = document(rows() + [("Characteristic", {"Id": "C0", "key": "FLAG", "value": 5.0})])
    graph = build_entity_graph([doc], load_underwriting_config())
    node = graph.table("Characteristic")["C0"]
    assert node.fields["value"] == 5.0
    assert any(e.field == "value" and e.value == "5.0" for e in node.evidence)
    result = evaluate_underwriting([doc]).loan_results[0]
    assert result.normalized_data["observation_groups"] == []
    assert result.normalized_data["characteristics"] == []


def test_canonical_salesforce_characteristic_spellings_and_selected_account_scope():
    source = rows() + observations()
    value = next(fields for table, fields in source if table == "Characteristic")
    value["SCOC__CharacteristicKey__c"] = value.pop("key")
    value["SCOC__CharacteristicValue__c"] = "0005"
    value.pop("value")
    result = evaluate_underwriting([document(source)], selected_account_id="A1").loan_results[0]
    assert result.normalized_data["observation_groups"][0]["characteristics"][0]["value"] == "0005"


def test_query1_characteristic_key_values_are_raw_and_explicitly_scoped():
    junction = "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r."
    loan_fk = junction + "LLC_BI__Loan__c"
    account_fk = junction + "LLC_BI__Account__c"
    role_fk = junction + "LLC_BI__Borrower_Type__c"
    values = ["19960700", "19960715"]
    source = [("MTB Query 1", {
        "Id": f"INV-{index}", loan_fk: "L1", account_fk: "A1", role_fk: "Borrower",
        "SCOC__CharacteristicKey__c": "FileEstablishedDate",
        "SCOC__CharacteristicValue__c": value,
    }) for index, value in enumerate(values, 1)]

    doc = document(source, "MTB_Query_1_Validated_Revision_SOQL.xlsx")
    doc.metadata["sheets"] = [{"name": source[0][0], "headers": list(source[0][1]), "is_reference": False}]
    result = evaluate_underwriting([doc]).loan_results[0]

    inventory = result.normalized_data["characteristic_observations"]
    assert len(inventory["groups"]) == 1
    group = inventory["groups"][0]
    assert (group["account_id"], group["loan_id"], group["party_role"]) == ("A1", "L1", "borrower")
    assert group["response_id"] is None
    assert group["report_id"] is None
    assert [(item["key"], item["value"]) for item in group["observations"]] == [
        ("FileEstablishedDate", value) for value in values]
    serialized = result.model_dump(mode="json")["normalized_data"]["characteristic_observations"]
    assert [item["value"] for item in serialized["groups"][0]["observations"]] == values
    assert all("date_interpretation" not in item and "earliest_date" not in item
               for item in serialized["groups"][0]["observations"])
    assert result.normalized_data["as_of_date"] is None
    assert result.normalized_data["selected_report_id"] is None
    assert "report_selection_requires_explicit_as_of_date" in result.missing_inputs


def test_raw_groups_and_values_never_enter_llm_report_context():
    source = grouped_rows()
    next(fields for table, fields in source if table == "Characteristic").update(
        key="PRIVATE_OBSERVATION_KEY", value="PRIVATE_OBSERVATION_VALUE")
    batch = evaluate_underwriting([document(source)])
    report = Report(decision="needs_review", summary="Synthetic test", document_count=1, findings=[], underwriting=batch)
    context = json.dumps(report_context(report))
    for forbidden in ("observation_groups", "characteristic_observations", "FileEstablishedDate",
                      "characteristics", "indicators",
                      "PRIVATE_OBSERVATION_KEY", "PRIVATE_OBSERVATION_VALUE"):
        assert forbidden not in context


def test_float_observation_does_not_relax_money_contract():
    source = rows() + observations()
    source[1][1]["requested_amount"] = 100000.0
    result = evaluate(source)
    assert result.normalized_amounts_usd["loan_requested_amount_usd"] is None
    assert any("INVALID_USD_AMOUNT" in issue for issue in result.conflicts)