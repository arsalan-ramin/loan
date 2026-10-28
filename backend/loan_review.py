"""Explicitly requested, report-bound, selected-loan Claude commentary."""

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from fastapi import HTTPException

from .assistant_workflow import require_loan
from .llm import AnthropicLLM, LLMError
from .llm_context import demo_explanation_context, report_context
from .models import LoanAIReview, LoanReviewOutput, Session
from .prompts import (
    DEMO_SELECTED_LOAN_CONTEXT_NOTE,
    DEMO_SELECTED_LOAN_INSTRUCTIONS,
    DEMO_SYSTEM,
    REVIEW_REASONING_GUIDANCE,
    SELECTED_LOAN_CONTEXT_NOTE as CONTEXT_NOTE,
    SELECTED_LOAN_EVIDENCE_PREFERENCE_INSTRUCTIONS,
    SELECTED_LOAN_RECONCILIATION_NOTE,
    SELECTED_LOAN_REVIEW_INSTRUCTIONS,
    V4_SYSTEM,
)


class LoanReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}
    loan_id: str = Field(min_length=1, max_length=128)
    report_id: str = Field(min_length=1, max_length=128)
    expected_updated_at: str
    confirmed: Literal[True]
    preferred_evidence_ref: str | None = Field(default=None, min_length=1, max_length=128)


def review_selected_loan(session: Session, loan_id: str, preferred_evidence_ref: str | None = None) -> LoanAIReview:
    loan = require_loan(session, loan_id)
    assert session.report is not None and session.report.underwriting is not None
    # Narrow BEFORE truncating so a loan after the first 20 is not lost.
    batch = session.report.underwriting.model_copy(update={"loan_results": [loan]})
    report = session.report.model_copy(update={"underwriting": batch})
    demo_context = demo_explanation_context(report)
    context = demo_context if demo_context is not None else report_context(report)
    context_note = DEMO_SELECTED_LOAN_CONTEXT_NOTE if demo_context is not None else CONTEXT_NOTE
    context["review_scope"] = context_note
    trace = context["underwriting"]
    if preferred_evidence_ref is not None:
        if preferred_evidence_ref not in trace["evidence_refs"]:
            raise HTTPException(422, "Choose evidence from this loan's bounded review trace. No provider request was made.")
        context["user_review_preference"] = {
            "evidence_ref": preferred_evidence_ref,
            "status": "UNVERIFIED_USER_PREFERENCE_NOT_SOURCE_CORRECTION",
            "instruction": SELECTED_LOAN_EVIDENCE_PREFERENCE_INSTRUCTIONS,
        }
    unresolved = {rule["rule_id"]: rule for rule in trace["loan_results"][0].get("rule_results", [])
                  if rule["status"] == "UNKNOWN"}
    context["checks_to_reconcile"] = [
        {"rule_id": rule["rule_id"], "diagnostic": rule["detail"],
         "mapped_evidence_available": bool(rule["evidence_refs"]),
         "evidence_refs": rule["evidence_refs"]}
        for rule in unresolved.values()
    ]
    if demo_context is None:
        context["reconciliation_scope"] = {
            "included_unknown_checks": len(unresolved),
            "total_unknown_checks": sum(rule.status == "UNKNOWN" for rule in loan.rule_results),
            "note": SELECTED_LOAN_RECONCILIATION_NOTE,
        }
    llm = AnthropicLLM()
    text = llm.complete(
        DEMO_SYSTEM + DEMO_SELECTED_LOAN_INSTRUCTIONS if demo_context is not None else
        V4_SYSTEM + REVIEW_REASONING_GUIDANCE + SELECTED_LOAN_REVIEW_INSTRUCTIONS,
        json.dumps(context, ensure_ascii=False),
    )
    try:
        output = LoanReviewOutput.model_validate_json(text)
    except ValidationError:
        raise LLMError("Claude returned invalid selected-loan commentary. It was discarded; your report is unchanged.") from None
    allowed_rules = {r["rule_id"] for r in trace["loan_results"][0].get("rule_results", []) if r["status"] == "FAIL"}
    if (output.loan_ids != [loan_id] or not set(output.triggered_rules) <= allowed_rules
            or not set(output.evidence_refs) <= set(trace["evidence_refs"])):
        raise LLMError("Claude cited unsupported loans, failed rules or evidence. The review was discarded; your report is unchanged.")
    reconciled_ids = [item.rule_id for item in output.check_reconciliation]
    if len(reconciled_ids) != len(set(reconciled_ids)) or set(reconciled_ids) != set(unresolved):
        raise LLMError("Claude did not reconcile the supplied unresolved checks exactly once. Output was discarded; your report is unchanged.")
    for item in output.check_reconciliation:
        if (not set(item.evidence_refs) <= set(unresolved[item.rule_id]["evidence_refs"])
                or (item.evidence_state == "MAPPED_EVIDENCE_BLOCKED" and not item.evidence_refs)):
            raise LLMError("Claude supplied unsupported per-check evidence. Output was discarded; your report is unchanged.")
    return LoanAIReview(report_id=session.report.id, loan_id=loan_id, model=llm.model,
                        analysis=output, context_note=context_note)