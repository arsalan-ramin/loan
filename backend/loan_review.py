"""Explicitly requested, report-bound, selected-loan Claude commentary."""

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .assistant_workflow import require_loan
from .llm import AnthropicLLM, LLMError, REVIEW_REASONING_GUIDANCE, V4_SYSTEM, report_context
from .models import LoanAIReview, LoanReviewOutput, Session


class LoanReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}
    loan_id: str = Field(min_length=1, max_length=128)
    report_id: str = Field(min_length=1, max_length=128)
    expected_updated_at: str
    confirmed: Literal[True]


CONTEXT_NOTE = (
    "Selected loan only: up to 40 prioritized rules and 40 evidence IDs, normalized amounts and features. "
    "No raw workbook rows, source values, chat history or retrieved memory. "
    "Unmapped attributes cannot be assessed; no final lending decision or new evidence validation."
)


def review_selected_loan(session: Session, loan_id: str) -> LoanAIReview:
    loan = require_loan(session, loan_id)
    assert session.report is not None and session.report.underwriting is not None
    # Narrow BEFORE truncating so a loan after the first 20 is not lost.
    batch = session.report.underwriting.model_copy(update={"loan_results": [loan]})
    report = session.report.model_copy(update={"underwriting": batch})
    context = report_context(report)
    context["review_scope"] = CONTEXT_NOTE
    trace = context["underwriting"]
    unresolved = {rule["rule_id"]: rule for rule in trace["loan_results"][0]["rule_results"]
                  if rule["status"] == "UNKNOWN"}
    context["checks_to_reconcile"] = [
        {"rule_id": rule["rule_id"], "diagnostic": rule["detail"],
         "mapped_evidence_available": bool(rule["evidence_refs"]),
         "evidence_refs": rule["evidence_refs"]}
        for rule in unresolved.values()
    ]
    context["reconciliation_scope"] = {
        "included_unknown_checks": len(unresolved),
        "total_unknown_checks": sum(rule.status == "UNKNOWN" for rule in loan.rule_results),
        "note": "Mapped evidence references indicate observations, not selected/validated input values. Never infer a field is absent solely from UNKNOWN status.",
    }
    llm = AnthropicLLM()
    text = llm.complete(
        V4_SYSTEM + REVIEW_REASONING_GUIDANCE + "\nPrepare a loan-focused review for the underwriter using only this selected loan. "
        "Explain evidence sufficiency, the supplied decision-flow logic, supported factors, contradictions, "
        "and the most important next evidence to obtain. Do not merely repeat all rule statuses. "
        "Do not promote an unselected report observation to a verified input. "
        "Treat missing, conflicting, unavailable and unmapped inputs distinctly. "
        "Do not resolve the USD100K/USD100M, USD250K/group-credit-vs-USD250M/Tier-I, or 6/12-month source conflicts. "
        "Preserve all engine paths, actions and explanations. Never issue an approval, denial or adverse action. "
        "Return ONLY compact JSON: recommended_route=MANUAL_REVIEW, decision_recommendation=null, confidence=null, "
        "requires_human_approval=true, loan_ids=[the selected loan ID], positive_factors, risk_factors, "
        "missing_information (each a string array of at most 3 short items), explanation (at most 200 words), "
        "triggered_rules (at most 3 supplied FAIL rule IDs), evidence_refs (at most 3 supplied evidence IDs). "
        "Also return check_reconciliation, exactly one entry per checks_to_reconcile rule: "
        "rule_id, evidence_state (MAPPED_EVIDENCE_BLOCKED, MISSING_OR_UNVERIFIED_INPUT, CONFLICTING_EVIDENCE, "
        "or POLICY_UNRESOLVED), rationale (at most 40 words), next_step (at most 25 words), "
        "evidence_refs (at most 3 evidence IDs supplied for that particular rule). "
        "Explain why each rule remains unresolved rather than copying the rule ID as missing data. "
        "Use MAPPED_EVIDENCE_BLOCKED only with a supplied rule evidence reference; "
        "a zero bankruptcy count needs no discharge date only AFTER valid account-scoped report selection. "
        "A count is not a discharge date; a freeze flag is not an OFAC disposition; EBIT is not EBITDA. "
        "These entries explain blockers, never clear rules or change their statuses. "
        "Put citations only in their arrays; no extra fields. Use an empty citation array if none are supplied.",
        json.dumps(context, ensure_ascii=False),
    )
    try:
        output = LoanReviewOutput.model_validate_json(text)
    except ValidationError:
        raise LLMError("Claude returned invalid selected-loan commentary. It was discarded; your report is unchanged.") from None
    allowed_rules = {r["rule_id"] for r in trace["loan_results"][0]["rule_results"] if r["status"] == "FAIL"}
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
                        analysis=output, context_note=CONTEXT_NOTE)