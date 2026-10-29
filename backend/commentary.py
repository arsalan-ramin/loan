"""Chat and report commentary orchestration and deterministic output guards."""

import json

from .commentary_validation import CommentaryValidation
from .llm import AnthropicLLM
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
        validation = CommentaryValidation("aggregate", UnderwritingExplanation)
        text = llm.complete(
            DEMO_SYSTEM + DEMO_AGGREGATE_INSTRUCTIONS if demo_context is not None else
            V4_SYSTEM + REVIEW_REASONING_GUIDANCE + AGGREGATE_UNDERWRITING_INSTRUCTIONS,
            json.dumps(context, ensure_ascii=False),
        )
        output = validation.parse(text, UnderwritingExplanation)
        trace = context["underwriting"]
        loan_ids = {loan["loan_id"] for loan in trace["loan_results"]}
        rule_ids = {rule["rule_id"] for loan in trace["loan_results"]
                    for rule in loan.get("rule_results", []) if rule["status"] == "FAIL"}
        if output.recommended_route != "MANUAL_REVIEW":
            validation.fail("scope", "Claude commentary contradicted the fixed route. It was discarded.",
                            [(("recommended_route",), "route_mismatch")])
        if output.confidence is not None:
            validation.fail("scope", "Claude commentary included unsupported confidence. It was discarded.",
                            [(("confidence",), "confidence_not_null")])
        if not set(output.loan_ids) <= loan_ids:
            validation.fail("scope", "Claude commentary cited unsupported loan IDs. It was discarded.",
                            [(("loan_ids",), "unsupported_reference")])
        if not set(output.triggered_rules) <= rule_ids:
            validation.fail("rules", "Claude commentary cited unsupported rule IDs. It was discarded.",
                            [(("triggered_rules",), "unsupported_reference")])
        if not set(output.evidence_refs) <= set(trace["evidence_refs"]):
            validation.fail("evidence", "Claude commentary cited unsupported evidence. It was discarded.",
                            [(("evidence_refs",), "unsupported_reference")])
        return output.explanation
    if report.policy_evaluation:
        validation = CommentaryValidation("policy", PolicyExplanation)
        text = llm.complete(
            SYSTEM + LEGACY_POLICY_EXPLANATION_INSTRUCTIONS,
            json.dumps(report_context(report), ensure_ascii=False),
        )
        output = validation.parse(text, PolicyExplanation)
        valid_ids = {r.rule_id for r in report.policy_evaluation.rule_results if r.status == "triggered"}
        if output.recommended_route != report.policy_evaluation.recommended_route:
            validation.fail("scope", "Claude commentary contradicted the fixed route. It was discarded.",
                            [(("recommended_route",), "route_mismatch")])
        if not set(output.triggered_rules) <= valid_ids:
            validation.fail("rules", "Claude commentary cited unsupported rule IDs. It was discarded.",
                            [(("triggered_rules",), "unsupported_reference")])
        # A model confidence signal is not sufficiently calibrated for this draft workflow.
        if output.confidence is not None:
            validation.fail("scope", "Claude commentary included an unsupported confidence score. It was discarded.",
                            [(("confidence",), "confidence_not_null")])
        report.llm_policy_output = output
        return output.explanation
    return llm.complete(
        SYSTEM + GENERIC_EXPLANATION_INSTRUCTIONS,
        json.dumps(report_context(report), ensure_ascii=False),
    )