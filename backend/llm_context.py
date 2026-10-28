"""Bounded, report- and session-aware provider context; no provider calls."""

import json

from .demo_workflow import bounded_demo_context
from .entity_graph import iso_date
from .models import Report, Session
from .prompts import CHAT_WORKFLOW_INSTRUCTIONS
from .storage import Store
from .underwriting_models import SOURCE_SHA256


def chat_context(store: Store, session: Session, query: str) -> dict:
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
    return {
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


def report_context(report: Report) -> dict:
    # Structured truncation preserves valid JSON and authoritative aggregate counts.
    if report.underwriting is not None:
        batch = report.underwriting
        loans = batch.loan_results[:20]
        # Global budgets, not 40 rules/evidence per loan. Prioritize attention.
        candidates = sorted(((index, rule) for index, loan in enumerate(loans) for rule in loan.rule_results),
                            key=lambda pair: (pair[1].status not in {"FAIL", "UNKNOWN"}, pair[0]))[:40]
        known_refs = {item.id for loan in loans for item in loan.source_evidence}
        if batch.mode == "TEST":
            for loan in loans:
                demo = loan.normalized_data.get("demo_decision")
                if isinstance(demo, dict) and isinstance(demo.get("source_evidence"), list):
                    known_refs.update(item["id"] for item in demo["source_evidence"][:100]
                                      if isinstance(item, dict) and isinstance(item.get("id"), str)
                                      and len(item["id"]) <= 128)
        # Share the global citation budget; no raw source-cell values are sent.
        demo_refs = []
        if batch.mode == "TEST":
            for loan in loans:
                demo = bounded_demo_context(loan.normalized_data.get("demo_decision"),
                                            mode=loan.mode, allowed_refs=known_refs)
                if demo:
                    demo_refs.extend(ref for item in demo["criteria"] for ref in item["evidence_refs"])
        evidence_refs = list(dict.fromkeys([*demo_refs, *(ref for _, rule in candidates for ref in rule.evidence_refs
                                                        if ref in known_refs)]))[:40]
        allowed_refs = set(evidence_refs)
        normalized = []
        reason_budget = 40
        for index, loan in enumerate(loans):
            data = loan.normalized_data
            selected_report_id = data.get("selected_report_id")
            selection = data.get("report_selection")
            selection = selection if isinstance(selection, dict) else {}
            as_of = iso_date(data.get("as_of_date"))
            report_date = iso_date(data.get("report_date"))
            inconsistent = bool(selected_report_id) and (
                as_of is None or report_date is None or report_date > as_of
                or selection.get("status") == "NOT_SELECTED"
            )
            selection_context = {
                "status": "SAVED_REPORT_INCONSISTENT_REPROCESS_REQUIRED" if inconsistent else
                          "SELECTED" if selected_report_id and selection.get("status") == "SELECTED" else
                          "LEGACY_SELECTION_NOT_VERIFIED" if selected_report_id else "NOT_SELECTED",
                "selected_report_id": selected_report_id,
                "as_of_date_available": as_of is not None,
                "selected_report_date_available": report_date is not None,
                "reason": str(selection.get("reason"))[:500] if selection.get("reason") else None,
                "fico_usable_in_commentary": bool(selected_report_id) and not inconsistent
                    and selection.get("status") == "SELECTED" and data.get("fico") is not None,
            }
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
                "demo_decision": bounded_demo_context(data.get("demo_decision"), mode=batch.mode,
                                                       allowed_refs=allowed_refs),
                "expanded_spec": expanded_context,
                "final_action": loan.final_action, "human_review_required": True,
                "eligibility_route": loan.eligibility_route,
                "credit_assessment": loan.credit_assessment,
                "authorization": loan.authorization,
                "strategy_branch": loan.strategy_branch,
                "report_selection": selection_context,
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
                    key: (None if key in {"fico", "fico_band"} and not selection_context["fico_usable_in_commentary"]
                          else loan.normalized_data.get(key)) for key in (
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