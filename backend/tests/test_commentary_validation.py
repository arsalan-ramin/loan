"""Mocked failure reproductions, strict parsing and value-free diagnostics only."""

import json
from copy import deepcopy
from unittest.mock import Mock
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend import commentary, loan_review, prompts
from backend.commentary_validation import CommentaryValidation
from backend.llm import LLMError
from backend.models import LoanReviewOutput, PolicyEvaluation, PolicyExplanation, Report, Session, UnderwritingExplanation
from backend.tests.test_underwriting import document, rows
from backend.underwriting_engine import evaluate_underwriting


SECRET = "PRIVATE_UNKNOWN_KEY_INPUT_CONFIG_SENTINEL"
MODELS = [("aggregate", UnderwritingExplanation), ("selected", LoanReviewOutput), ("policy", PolicyExplanation)]


def response(workflow="aggregate"):
    result = dict(recommended_route="MANUAL_REVIEW", decision_recommendation=None, confidence=None,
                  positive_factors=[], risk_factors=[], missing_information=[], triggered_rules=[],
                  explanation="Human review remains required.", requires_human_approval=True)
    if workflow != "policy":
        result.update(loan_ids=["L1"], evidence_refs=[])
    if workflow == "selected":
        result["check_reconciliation"] = []
    return result


def events(caplog):
    return [json.loads(record.getMessage()) for record in caplog.records
            if record.name in {"backend.commentary", "backend.loan_review"}]


def assert_diagnostic(caplog, error, stage, workflow):
    logged = events(caplog)
    assert len(logged) == 1
    event = logged[0]
    assert event["event"] == "commentary_validation_failed"
    assert str(UUID(event["diagnostic_id"])) == event["diagnostic_id"]
    assert event["diagnostic_id"] in str(error)
    assert error.status_code == 502
    assert event["stage"] == stage and event["workflow"] == workflow
    assert event["duration_ms"] >= 0 and event["response_length"] > 0
    assert event["error_count"] >= 1
    assert SECRET not in caplog.text and SECRET not in str(error)
    assert set(event) == {"event", "diagnostic_id", "workflow", "stage", "duration_ms",
                          "response_length", "error_count", "errors"}
    return event


@pytest.mark.parametrize("workflow,model", MODELS)
@pytest.mark.parametrize("fenced", [False, True])
def test_strict_json_or_one_entire_json_fence(workflow, model, fenced):
    text = json.dumps(response(workflow))
    if fenced:
        text = " \n```json\n" + text + "\n```\n "
    assert CommentaryValidation(workflow, model).parse(text, model).explanation == response()["explanation"]


@pytest.mark.parametrize("wrap", [
    lambda text: "Here is JSON: " + text,
    lambda text: text + " trailing prose",
    lambda text: "prefix\n```json\n" + text + "\n```",
    lambda text: "```json\n" + text + "\n```\nsuffix",
    lambda text: "```json\n" + text + "\n```\n```json\n" + text + "\n```",
    lambda text: "```\n" + text + "\n```",
    lambda text: "```python\n" + text + "\n```",
    lambda text: text + text,
    lambda text: text.replace('"confidence": null', '"confidence": NaN'),
    lambda text: text.replace('"confidence": null', '"confidence": Infinity'),
    lambda text: text[:-1] + ', "explanation": "duplicate"}',
    lambda text: "[" + text + "]",
    lambda text: "null",
    lambda text: '{"unfinished":',
])
def test_never_extract_or_repair_json(wrap, caplog):
    text = wrap(json.dumps(response()))
    with pytest.raises(LLMError) as caught:
        CommentaryValidation("aggregate", UnderwritingExplanation).parse(text, UnderwritingExplanation)
    assert_diagnostic(caplog, caught.value, "schema", "aggregate")


@pytest.mark.parametrize("workflow,model", MODELS)
@pytest.mark.parametrize("field", ["recommended_route", "requires_human_approval"])
def test_reproduce_missing_required_fields_without_guessing_provider_response(workflow, model, field, caplog):
    data = response(workflow)
    del data[field]
    with pytest.raises(LLMError) as caught:
        CommentaryValidation(workflow, model).parse(json.dumps(data), model)
    event = assert_diagnostic(caplog, caught.value, "schema", workflow)
    assert event["errors"] == [{"path": field, "type": "missing"}]


@pytest.mark.parametrize("workflow,model", MODELS)
@pytest.mark.parametrize("changes,path,kind", [
    ({SECRET: SECRET}, "<extra>", "extra_forbidden"),
    ({"requires_human_approval": SECRET}, "requires_human_approval", "bool_type"),
    ({"requires_human_approval": 1}, "requires_human_approval", "bool_type"),
    ({"requires_human_approval": False}, "requires_human_approval", "literal_error"),
    ({"decision_recommendation": SECRET}, "decision_recommendation", "none_required"),
    ({"positive_factors": SECRET}, "positive_factors", "list_type"),
    ({"positive_factors": [123]}, "positive_factors.[]", "string_type"),
    ({"explanation": 123}, "explanation", "string_type"),
    ({"confidence": "0.5"}, "confidence", None),
])
def test_schema_diagnostics_never_include_unknown_fields_inputs_or_values(workflow, model, changes, path, kind, caplog):
    with pytest.raises(LLMError) as caught:
        CommentaryValidation(workflow, model).parse(json.dumps({**response(workflow), **changes}), model)
    event = assert_diagnostic(caplog, caught.value, "schema", workflow)
    assert event["errors"][0]["path"] == path
    if kind:
        assert event["errors"][0]["type"] == kind


@pytest.mark.parametrize("loc,expected", [
    ((SECRET,), "<unknown>"),
    (("check_reconciliation", 12, SECRET), "check_reconciliation.[].<unknown>"),
    (("check_reconciliation", SECRET, "rationale"), "check_reconciliation.<unknown>"),
    (("check_reconciliation", 12, "rationale", SECRET), "check_reconciliation.[].rationale.<unknown>"),
])
def test_untrusted_pydantic_location_message_context_and_error_type_are_sanitized(monkeypatch, caplog, loc, expected):
    error = ValidationError.from_exception_data("Untrusted " + SECRET, [{
        "type": "value_error", "loc": loc, "input": SECRET, "ctx": {"error": ValueError(SECRET)},
    }])
    monkeypatch.setattr(LoanReviewOutput, "model_validate", Mock(side_effect=error))
    with pytest.raises(LLMError) as caught:
        CommentaryValidation("selected", LoanReviewOutput).parse(json.dumps(response("selected")), LoanReviewOutput)
    event = assert_diagnostic(caplog, caught.value, "schema", "selected")
    assert event["errors"] == [{"path": expected, "type": "validation_error"}]


def test_nested_extra_name_is_not_logged_even_if_it_is_another_known_field(caplog):
    data = response("selected")
    data["check_reconciliation"] = [dict(rule_id=SECRET, evidence_state="POLICY_UNRESOLVED",
                                         rationale=SECRET, next_step=SECRET, evidence_refs=[], loan_ids=SECRET)]
    with pytest.raises(LLMError) as caught:
        CommentaryValidation("selected", LoanReviewOutput).parse(json.dumps(data), LoanReviewOutput)
    event = assert_diagnostic(caplog, caught.value, "schema", "selected")
    assert event["errors"] == [{"path": "check_reconciliation.[].<extra>", "type": "extra_forbidden"}]


@pytest.fixture
def invocation(monkeypatch):
    batch = evaluate_underwriting([document(rows())], mode="TEST")
    report = Report(decision="needs_review", summary="Fixture", findings=[], document_count=1, underwriting=batch)
    session = Session(status="completed", report=report)
    context = {"underwriting": {"loan_results": [{"loan_id": "L1", "rule_results": [
        {"rule_id": "failed", "status": "FAIL", "detail": SECRET, "evidence_refs": ["E1"]},
        {"rule_id": "unknown", "status": "UNKNOWN", "detail": SECRET, "evidence_refs": ["E1"]},
    ]}], "evidence_refs": ["E1", "E2"]}}
    for module in (commentary, loan_review):
        monkeypatch.setattr(module, "demo_explanation_context", lambda report: None)
        monkeypatch.setattr(module, "report_context", lambda report: deepcopy(context))
    provider = Mock(model=SECRET)
    monkeypatch.setattr(loan_review, "AnthropicLLM", Mock(return_value=provider))

    def invoke(workflow, data):
        provider.complete.return_value = json.dumps(data)
        if workflow == "policy":
            report.underwriting = None
            report.policy_evaluation = PolicyEvaluation(policy_version="fixture", source_document=SECRET,
                                                       recommended_route="COMPLIANCE_HOLD")
        before = report.model_dump_json()
        try:
            if workflow == "selected":
                return loan_review.review_selected_loan(session, "L1")
            return commentary.explain(provider, report)
        finally:
            provider.complete.assert_called_once()
            assert report.model_dump_json() == before
            assert session.loan_ai_reviews == []
    return invoke, provider


@pytest.mark.parametrize("workflow,changes,stage,path", [
    ("aggregate", {"recommended_route": SECRET}, "scope", "recommended_route"),
    ("aggregate", {"confidence": 0.9}, "scope", "confidence"),
    ("aggregate", {"loan_ids": [SECRET]}, "scope", "loan_ids"),
    ("aggregate", {"triggered_rules": [SECRET]}, "rules", "triggered_rules"),
    ("aggregate", {"evidence_refs": [SECRET]}, "evidence", "evidence_refs"),
    ("selected", {"recommended_route": SECRET}, "schema", "recommended_route"),
    ("selected", {"confidence": 0.9}, "schema", "confidence"),
    ("selected", {"loan_ids": [SECRET]}, "scope", "loan_ids"),
    ("selected", {"triggered_rules": [SECRET]}, "rules", "triggered_rules"),
    ("selected", {"evidence_refs": [SECRET]}, "evidence", "evidence_refs"),
    ("selected", {"check_reconciliation": []}, "reconciliation", "check_reconciliation"),
    ("policy", {"recommended_route": SECRET}, "scope", "recommended_route"),
    ("policy", {"recommended_route": "COMPLIANCE_HOLD", "confidence": 0.9}, "scope", "confidence"),
    ("policy", {"recommended_route": "COMPLIANCE_HOLD", "triggered_rules": [SECRET]}, "rules", "triggered_rules"),
])
def test_each_runtime_failure_stage_is_correlated_safe_and_never_retried(invocation, caplog, workflow, changes, stage, path):
    invoke, _provider = invocation
    with pytest.raises(LLMError) as caught:
        invoke(workflow, {**response(workflow), **changes})
    event = assert_diagnostic(caplog, caught.value, stage, workflow)
    assert event["errors"][0]["path"] == path


def check(**changes):
    return dict(rule_id="unknown", evidence_state="POLICY_UNRESOLVED", rationale=SECRET,
                next_step=SECRET, evidence_refs=["E1"], **changes)


@pytest.mark.parametrize("defect,stage", [
    ("duplicate", "reconciliation"), ("foreign_rule", "reconciliation"),
    ("foreign_evidence", "evidence"), ("other_check_evidence", "evidence"),
    ("mapped_empty", "evidence"), ("wrong_state", "schema"),
])
def test_reconciliation_exactness_and_per_check_evidence_preserved(invocation, caplog, defect, stage):
    invoke, _ = invocation
    item = check()
    items = [item]
    if defect == "duplicate":
        items.append(item.copy())
    elif defect == "foreign_rule":
        item["rule_id"] = SECRET
    elif defect == "foreign_evidence":
        item["evidence_refs"] = [SECRET]
    elif defect == "other_check_evidence":
        item["evidence_refs"] = ["E2"]  # In the loan trace, not supplied for this check.
    elif defect == "mapped_empty":
        item.update(evidence_state="MAPPED_EVIDENCE_BLOCKED", evidence_refs=[])
    else:
        item["evidence_state"] = SECRET
    with pytest.raises(LLMError) as caught:
        invoke("selected", {**response("selected"), "check_reconciliation": items})
    event = assert_diagnostic(caplog, caught.value, stage, "selected")
    if stage == "evidence":
        assert event["errors"][0]["path"] == "check_reconciliation.[].evidence_refs"


def test_valid_selected_reconciliation_is_accepted_without_logging(invocation, caplog):
    invoke, _ = invocation
    review = invoke("selected", {**response("selected"), "check_reconciliation": [check()]})
    assert review.analysis.check_reconciliation[0].rule_id == "unknown"
    assert events(caplog) == []


@pytest.mark.parametrize("workflow,model", MODELS)
def test_generated_schema_is_in_actual_provider_system_not_data(invocation, caplog, workflow, model):
    invoke, provider = invocation
    data = response(workflow)
    if workflow == "selected":
        data["check_reconciliation"] = [check()]
    elif workflow == "policy":
        data["recommended_route"] = "COMPLIANCE_HOLD"
    # Fail after capturing the call so the report must remain entirely unchanged.
    del data["requires_human_approval"]
    with pytest.raises(LLMError):
        invoke(workflow, data)
    system, payload = provider.complete.call_args.args
    schema = json.dumps(model.model_json_schema(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    assert schema in system and schema not in payload
    assert SECRET not in system  # No provider config or source data in instructions.
    assert "OUTPUT JSON SCHEMA" not in payload
    assert json.loads(payload)["underwriting"]["loan_results"][0]["loan_id"] == "L1"


@pytest.mark.parametrize("workflow,model", MODELS)
def test_human_review_alias_cannot_replace_required_model_field(workflow, model, caplog):
    data = response(workflow)
    data["human_review_required"] = data.pop("requires_human_approval")
    with pytest.raises(LLMError) as caught:
        CommentaryValidation(workflow, model).parse(json.dumps(data), model)
    event = assert_diagnostic(caplog, caught.value, "schema", workflow)
    assert {("requires_human_approval", "missing"), ("<extra>", "extra_forbidden")} == {
        (error["path"], error["type"]) for error in event["errors"]}
    assert "human_review_required" not in caplog.text


def test_transport_errors_are_not_reclassified_or_logged(invocation, caplog):
    invoke, provider = invocation
    error = LLMError("Transport unavailable", 503)
    provider.complete.side_effect = error
    with pytest.raises(LLMError) as caught:
        invoke("selected", response("selected"))
    assert caught.value is error and caught.value.status_code == 503
    assert events(caplog) == []


@pytest.mark.parametrize("name,model", [
    ("AGGREGATE_UNDERWRITING_INSTRUCTIONS", UnderwritingExplanation),
    ("DEMO_AGGREGATE_INSTRUCTIONS", UnderwritingExplanation),
    ("SELECTED_LOAN_REVIEW_INSTRUCTIONS", LoanReviewOutput),
    ("DEMO_SELECTED_LOAN_INSTRUCTIONS", LoanReviewOutput),
    ("LEGACY_POLICY_EXPLANATION_INSTRUCTIONS", PolicyExplanation),
])
def test_prompt_contains_exact_pydantic_schema_not_just_prose_field_names(name, model):
    instruction = getattr(prompts, name)
    schema_text = instruction.split("OUTPUT JSON SCHEMA (authoritative field names, required fields and types):\n")[1].split("\n", 1)[0]
    schema = json.loads(schema_text)
    assert schema == model.model_json_schema()
    assert {"recommended_route", "requires_human_approval"} <= set(schema["required"])
    assert schema["additionalProperties"] is False
    assert "human_review_required" not in schema["properties"]
    if model is LoanReviewOutput:
        assert schema["$defs"]["CheckReconciliation"]["additionalProperties"] is False
        assert schema["properties"]["recommended_route"]["const"] == "MANUAL_REVIEW"


def test_failure_ids_are_unique_and_error_detail_list_is_bounded(caplog):
    ids = set()
    for _ in range(2):
        with pytest.raises(LLMError):
            CommentaryValidation("aggregate", UnderwritingExplanation).parse(
                json.dumps({**response(), **{SECRET + str(i): SECRET for i in range(50)}}), UnderwritingExplanation)
        event = events(caplog)[-1]
        ids.add(event["diagnostic_id"])
        assert event["error_count"] == 50 and len(event["errors"]) == 20
    assert len(ids) == 2 and SECRET not in caplog.text