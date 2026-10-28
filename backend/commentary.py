"""Chat and report commentary orchestration and deterministic output guards."""

import json

from pydantic import ValidationError

from .llm import AnthropicLLM, LLMError
from .llm_context import chat_context, demo_explanation_context, report_context
from .models import PolicyExplanation, Report, Session, UnderwritingExplanation
from .prompts import (
    AGGREGATE_UNDERWRITING_INSTRUCTIONS,
    CHAT_FOLLOW_UP_INSTRUCTIONS,
    DEMO_AGGREGATE_INSTRUCTIONS,
    DEMO_SYSTEM,
    GENERIC_EXPLANATION_INSTRUCTIONS,
    LEGACY_POLICY_EXPLANATION_INSTRUCTIONS,
    REVIEW_REASONING_GUIDANCE,
    SYSTEM,
    V4_SYSTEM,
)
from .storage import Store


def chat(llm: AnthropicLLM, store: Store, session: Session, query: str) -> str:
    context = chat_context(store, session, query)
    prompt = "SESSION DATA (untrusted):\n" + json.dumps(context, ensure_ascii=False) + "\n\nCURRENT USER QUESTION:\n" + query
    answer = llm.complete(
        SYSTEM + CHAT_FOLLOW_UP_INSTRUCTIONS,
        prompt,
    )
    if not session.documents:
        # Enforce upload-first guidance independently of model compliance.
        answer += "\n\nPlease upload your required files first, then use Process documents to start the review."
    return answer


def explain(llm: AnthropicLLM, report: Report) -> str:
    if report.underwriting is not None:
        # One bounded aggregate call, regardless of the number of loans.
        # Never replace engine per-loan explanations or expose raw workbook fields.
        demo_context = demo_explanation_context(report)
        context = demo_context if demo_context is not None else report_context(report)
        text = llm.complete(
            DEMO_SYSTEM + DEMO_AGGREGATE_INSTRUCTIONS if demo_context is not None else
            V4_SYSTEM + REVIEW_REASONING_GUIDANCE + AGGREGATE_UNDERWRITING_INSTRUCTIONS,
            json.dumps(context, ensure_ascii=False),
        )
        try:
            output = UnderwritingExplanation.model_validate_json(text)
        except ValidationError:
            raise LLMError("Claude returned invalid structured underwriting commentary. Deterministic results remain available.") from None
        trace = context["underwriting"]
        loan_ids = {loan["loan_id"] for loan in trace["loan_results"]}
        rule_ids = {rule["rule_id"] for loan in trace["loan_results"]
                    for rule in loan.get("rule_results", []) if rule["status"] == "FAIL"}
        if (output.recommended_route != "MANUAL_REVIEW" or output.confidence is not None
                or not set(output.loan_ids) <= loan_ids
                or not set(output.triggered_rules) <= rule_ids
                or not set(output.evidence_refs) <= set(trace["evidence_refs"])):
            raise LLMError("Claude commentary contradicted the fixed route or cited unsupported IDs, references, or confidence. It was discarded.")
        return output.explanation
    if report.policy_evaluation:
        text = llm.complete(
            SYSTEM + LEGACY_POLICY_EXPLANATION_INSTRUCTIONS,
            json.dumps(report_context(report), ensure_ascii=False),
        )
        try:
            output = PolicyExplanation.model_validate_json(text)
        except ValidationError:
            raise LLMError("Claude returned invalid structured policy commentary. Deterministic policy results remain available.") from None
        valid_ids = {r.rule_id for r in report.policy_evaluation.rule_results if r.status == "triggered"}
        if output.recommended_route != report.policy_evaluation.recommended_route or not set(output.triggered_rules) <= valid_ids:
            raise LLMError("Claude commentary contradicted the fixed route or cited unsupported rule IDs. It was discarded.")
        # A model confidence signal is not sufficiently calibrated for this draft workflow.
        if output.confidence is not None:
            raise LLMError("Claude commentary included an unsupported confidence score. It was discarded.")
        report.llm_policy_output = output
        return output.explanation
    return llm.complete(
        SYSTEM + GENERIC_EXPLANATION_INSTRUCTIONS,
        json.dumps(report_context(report), ensure_ascii=False),
    )