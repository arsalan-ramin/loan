"""Server-only Anthropic Messages API adapter with bounded context and safe errors."""

import json
import os

import anthropic
from pydantic import ValidationError

from .models import PolicyExplanation, Report, Session, UnderwritingExplanation
from .prompts import (
    AGGREGATE_UNDERWRITING_INSTRUCTIONS,
    CHAT_FOLLOW_UP_INSTRUCTIONS,
    CHAT_WORKFLOW_INSTRUCTIONS,
    GENERIC_EXPLANATION_INSTRUCTIONS,
    LEGACY_POLICY_EXPLANATION_INSTRUCTIONS,
    REVIEW_REASONING_GUIDANCE,
    SYSTEM,
    V4_SYSTEM,
)
from .storage import Store
from .underwriting_models import SOURCE_SHA256

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_OUTPUT_TOKENS = 8192


class LLMError(RuntimeError):
    """Safe, actionable error; never includes provider bodies, credentials, or prompts."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class AnthropicLLM:
    def __init__(self):
        self.model = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    def complete(self, system: str, prompt: str) -> str:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise LLMError("Anthropic is not configured. Set ANTHROPIC_API_KEY in the backend environment and restart the API.", 503)
        try:
            # Explicit endpoint avoids accidentally sending session evidence to an SDK base-URL override.
            with anthropic.Anthropic(api_key=key, base_url="https://api.anthropic.com", timeout=60.0, max_retries=1) as client:
                response = client.messages.create(
                    model=self.model, max_tokens=MAX_OUTPUT_TOKENS, system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
        except anthropic.AuthenticationError:
            raise LLMError("Anthropic authentication failed. Check the backend ANTHROPIC_API_KEY.", 503) from None
        except (anthropic.PermissionDeniedError, anthropic.NotFoundError):
            raise LLMError(f"Anthropic model {self.model} is unavailable to this account. Check model access and ANTHROPIC_MODEL.", 503) from None
        except anthropic.RateLimitError:
            raise LLMError("Anthropic rate limit reached. Retry shortly or check your account limits.", 429) from None
        except anthropic.APITimeoutError:
            raise LLMError("Anthropic timed out. Please retry.", 504) from None
        except anthropic.APIConnectionError:
            raise LLMError("Unable to connect to Anthropic. Check the backend network connection.", 503) from None
        except anthropic.APIStatusError:
            raise LLMError("Anthropic rejected the request. Check model configuration, billing, and service availability.") from None
        text = "\n\n".join(block.text for block in response.content if block.type == "text").strip()
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude reached the {MAX_OUTPUT_TOKENS:,}-token output limit before completing its answer. "
                           "Incomplete commentary was discarded; deterministic results are unchanged. "
                           "Reprocessing may incur another Anthropic charge; retrying is not guaranteed to resolve truncation.")
        if response.stop_reason not in ("end_turn", "stop_sequence"):
            raise LLMError("Anthropic stopped before a normal completion. Commentary was discarded; "
                           "deterministic results are unchanged. Check model availability before retrying.")
        if not text:
            raise LLMError("Anthropic returned no text in its completed response. Deterministic results are unchanged. "
                           "Check model availability before retrying; another request may incur charges.")
        return text

    def chat(self, store: Store, session: Session, query: str) -> str:
        safe_documents = []
        xlsx_sources = {doc.name for doc in session.documents if doc.extension.casefold() == ".xlsx"}
        for doc in session.documents:
            sheets = doc.metadata.get("sheets", [])
            safe_documents.append({
                "name": doc.name,
                "extension": doc.extension,
                "extracted": bool(doc.metadata),
                "metadata": json.dumps({
                    key: value for key, value in doc.metadata.items()
                    if key not in {"records", "sources", "raw_rows"}
                }, ensure_ascii=False)[:2000],
                "sheet_headers": [
                    {"name": sheet.get("name"), "headers": sheet.get("headers", [])}
                    for sheet in sheets if isinstance(sheet, dict)
                ] if isinstance(sheets, list) else [],
            })
        recalled_documents = [
            (source, text[:1400]) for source, text in store.recall(session.id, query, "document")[:4]
            if source not in xlsx_sources
        ]
        local_assistant_content = {msg.content for msg in session.messages if msg.origin == "local_assistant"}
        recalled_conversation = [
            (source, text[:1400]) for source, text in store.recall(session.id, query, "conversation")[:4]
            if text not in local_assistant_content
        ]
        context = {
            "status": session.status,
            "local_preflight": session.preflight.model_dump() if session.preflight is not None else None,
            "workflow_actions": CHAT_WORKFLOW_INSTRUCTIONS,
            "documents": safe_documents,
            "rules": [rule.model_dump() for rule in session.rules],
            "underwriting_rule_catalog": session.policy_rules,
            "report": report_context(session.report) if session.report else None,
            "recent_messages": [{"role": msg.role, "content": msg.content[:2000]}
                                for msg in session.messages if msg.origin == "conversation"][-12:],
            "recalled_conversation": recalled_conversation,
            "document_excerpts": recalled_documents,
            "context_note": "Bounded excerpts and recent history only; not the entire archive.",
        }
        prompt = "SESSION DATA (untrusted):\n" + json.dumps(context, ensure_ascii=False) + "\n\nCURRENT USER QUESTION:\n" + query
        answer = self.complete(
            SYSTEM + CHAT_FOLLOW_UP_INSTRUCTIONS,
            prompt,
        )
        if not session.documents:
            # Enforce upload-first guidance independently of model compliance.
            answer += "\n\nPlease upload your required files first, then use Process documents to start the review."
        return answer

    def explain(self, report: Report) -> str:
        if report.underwriting is not None:
            # One bounded aggregate call, regardless of the number of loans.
            # Never replace engine per-loan explanations or expose raw workbook fields.
            context = report_context(report)
            text = self.complete(
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
                        for rule in loan["rule_results"] if rule["status"] == "FAIL"}
            if (output.recommended_route != "MANUAL_REVIEW" or output.confidence is not None
                    or not set(output.loan_ids) <= loan_ids
                    or not set(output.triggered_rules) <= rule_ids
                    or not set(output.evidence_refs) <= set(trace["evidence_refs"])):
                raise LLMError("Claude commentary contradicted the fixed route or cited unsupported IDs, references, or confidence. It was discarded.")
            return output.explanation
        if report.policy_evaluation:
            text = self.complete(
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
        return self.complete(
            SYSTEM + GENERIC_EXPLANATION_INSTRUCTIONS,
            json.dumps(report_context(report), ensure_ascii=False),
        )


def report_context(report: Report) -> dict:
    # Structured truncation preserves valid JSON and authoritative aggregate counts.
    if report.underwriting is not None:
        batch = report.underwriting
        loans = batch.loan_results[:20]
        # Global budgets, not 40 rules/evidence per loan. Prioritize attention.
        candidates = sorted(((index, rule) for index, loan in enumerate(loans) for rule in loan.rule_results),
                            key=lambda pair: (pair[1].status not in {"FAIL", "UNKNOWN"}, pair[0]))[:40]
        known_refs = {item.id for loan in loans for item in loan.source_evidence}
        evidence_refs = list(dict.fromkeys(ref for _, rule in candidates for ref in rule.evidence_refs
                                          if ref in known_refs))[:40]
        allowed_refs = set(evidence_refs)
        normalized = []
        reason_budget = 40
        for index, loan in enumerate(loans):
            expanded_context = None
            if loan.expanded_spec is not None:
                spec = loan.expanded_spec
                reason_items = sorted(spec.decline_reasons, key=lambda item: (item.status != "FAIL", item.status != "REVIEW"))[:reason_budget]
                reason_budget -= len(reason_items)
                expanded_context = {
                    "version": spec.version, "source_available": spec.source_available,
                    "eligibility_route": spec.eligibility_route,
                    "simulated_credit_route": spec.simulated_credit_route,
                    "authorized_decision": "NOT_ISSUED",
                    "candidate_reason_codes": spec.candidate_reason_codes[:40],
                    "reason_diagnostics": [{"code": item.code, "status": item.status,
                        "disposition": item.disposition, "adverse_action_code": None,
                        "evidence_refs": [ref for ref in item.evidence_refs if ref in allowed_refs]}
                        for item in reason_items],
                    "missing_inputs": spec.missing_inputs[:40],
                    "policy_conflicts": spec.policy_conflicts[:40],
                }
            normalized.append({
                "loan_id": loan.loan_id, "recommendation_path": loan.recommendation_path,
                "expanded_spec": expanded_context,
                "final_action": loan.final_action, "human_review_required": True,
                "eligibility_route": loan.eligibility_route,
                "credit_assessment": loan.credit_assessment,
                "authorization": loan.authorization,
                "strategy_branch": loan.strategy_branch,
                "business_banking_workflow": {
                    key: loan.normalized_data.get("business_banking_workflow", {}).get(key)
                    for key in ("eligibility", "recommendation_candidate", "missing_inputs", "policy_conflicts", "stages", "interpretation", "program_observation_available", "requested_path_mapping_status")
                },
                "strategy_diagnostics": {
                    "stages": loan.normalized_data.get("implementation_guide_diagnostics", {}).get("stages", {}),
                    "branch": loan.normalized_data.get("implementation_guide_diagnostics", {}).get("strategy_branch"),
                    "source_available": loan.normalized_data.get("implementation_guide_diagnostics", {}).get("audit", {}).get("source_available"),
                },
                "normalized_features": {
                    key: loan.normalized_data.get(key) for key in (
                        "sbss", "fico", "quick_app_nfp_eligible", "sbss_bucket", "fico_band", "ownership_at_least_2_years",
                        "ebitda_nonnegative", "bankruptcy_discharge_at_least_5_years", "minimum_age_met",
                        "naics_not_high_risk", "credit_freeze_clear", "ofac_confirmed_match", "ofac_alert_clear",
                        "new_money_within_diagnostic_limit", "tier_i_within_diagnostic_limit",
                        "requested_amount_le_100k", "requested_amount_le_100m",
                    ) if key in loan.normalized_data
                },
                "simulation_reference": loan.normalized_data.get("simulation_reference"),
                "normalized_amounts_usd": loan.model_dump(mode="json")["normalized_amounts_usd"],
                "missing_inputs": [item[:500] for item in loan.missing_inputs[:40]],
                "conflicts": [item[:500] for item in loan.conflicts[:40]],
                "rule_results": [{**rule.model_dump(exclude={"evidence_refs"}),
                                  "detail": rule.detail[:500],
                                  "evidence_refs": [ref for ref in rule.evidence_refs if ref in allowed_refs]}
                                 for owner, rule in candidates if owner == index],
            })
        return {
            "decision": "needs_review", "decision_recommendation": None,
            "policy_evaluation": {"recommended_route": "MANUAL_REVIEW"},
            "underwriting": {
                "mode": batch.mode, "currency": batch.currency, "policy_version": batch.policy_version,
                "source_document": batch.source_document, "source_available": batch.source_available,
                "source_sha256": SOURCE_SHA256 if batch.source_available else None,
                "implementation_guide_source_document": batch.implementation_guide_source_document,
                "implementation_guide_source_sha256": batch.implementation_guide_source_sha256,
                "implementation_guide_available": batch.implementation_guide_available,
                "expanded_spec_source_document": batch.expanded_spec_source_document,
                "expanded_spec_source_sha256": batch.expanded_spec_source_sha256,
                "expanded_spec_available": batch.expanded_spec_available,
                "loan_count": len(batch.loan_results),
                "rule_count": sum(len(loan.rule_results) for loan in batch.loan_results),
                "loan_results": normalized, "evidence_refs": evidence_refs,
                "data_quality_issues": [item[:500] for item in batch.data_quality_issues[:40]],
                "context_note": "At most 20 loans, 40 rules and 40 evidence IDs. No raw documents, fields, "
                                "source values or conversation. One aggregate commentary call only; "
                                "all per-loan explanations remain deterministic. Full batch is authoritative.",
            },
        }
    policy = report.policy_evaluation
    policy_context = None
    if policy:
        policy_context = policy.model_dump(exclude={"rule_results", "profiles", "missing_information"})
        policy_context["rule_results"] = [
            {**result.model_dump(exclude={"evidence"}), "evidence": [item[:500] for item in result.evidence[:8]]}
            for result in sorted(policy.rule_results, key=lambda result: result.status == "not_triggered")[:40]
        ]
        policy_context["missing_information"] = [item[:500] for item in policy.missing_information[:40]]
        policy_context["profile_count"] = len(policy.profiles)
        policy_context["context_note"] = "At most 40 policy results and 40 missing-information items shown; full report is authoritative."
    return {"decision": report.decision, "summary": report.summary, "policy_evaluation": policy_context,
            "findings": [finding.model_dump() for finding in sorted(report.findings, key=lambda f: f.status == "pass")[:40]],
            "total_findings": len(report.findings), "document_count": report.document_count}