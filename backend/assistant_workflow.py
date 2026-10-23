"""Allowlisted local assistant operations. No LLM, authority changes or source edits.

Preflight parses a detached copy of uploaded files, never mutates a completed
report, and never treats conversation/review notes as underwriting evidence.
"""

import hashlib
import json
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .agents import ExtractionAgent
from .datasets import mtb_query_ids
from .models import AccountCandidate, LoanInspection, PreflightResult, Session
from .entity_graph import scalar_id
from .underwriting_engine import load_underwriting_config
from .underwriting_models import LoanRecommendation

ACCOUNT_INDEX_VERSION = 1


class InspectRequest(BaseModel):
    model_config = {"extra": "forbid"}
    loan_id: str = Field(min_length=1, max_length=128)
    topic: Literal["blockers", "rules", "evidence"] = "blockers"


class GuidedProcessRequest(BaseModel):
    model_config = {"extra": "forbid"}
    confirmed: Literal[True]
    expected_updated_at: str


class ReviewNoteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    loan_id: str = Field(min_length=1, max_length=128)
    report_id: str
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["note", "acknowledgment"] = "note"


def require_loan(session: Session, loan_id: str) -> LoanRecommendation:
    if session.status != "completed" or session.report is None or session.report.underwriting is None:
        raise HTTPException(409, "Complete an underwriting review before inspecting a loan.")
    matches = [loan for loan in session.report.underwriting.loan_results if loan.loan_id == loan_id]
    if len(matches) != 1:
        raise HTTPException(404, "A unique loan was not found in this session's completed report.")
    return matches[0]


def inspect_loan(session: Session, body: InspectRequest) -> LoanInspection:
    loan = require_loan(session, body.loan_id)
    assert session.report is not None
    rules = sorted(loan.rule_results, key=lambda item: (item.status != "FAIL", item.status != "UNKNOWN"))[:12]
    refs = {ref for rule in rules for ref in rule.evidence_refs}
    evidence = sorted(loan.source_evidence, key=lambda item: item.id not in refs)[:12]
    return LoanInspection(report_id=session.report.id, loan_id=loan.loan_id, topic=body.topic,
        recommendation_path=loan.recommendation_path, final_action=loan.final_action,
        missing_inputs=loan.missing_inputs[:20], conflicts=loan.conflicts[:20], rules=rules,
        evidence=evidence, total_rules=len(loan.rule_results), total_evidence=len(loan.source_evidence))


def _mapped_values(manifest: dict, table: str, sheet: str, values: dict) -> dict:
    tables = manifest["tables"]
    canonical_table = manifest.get("canonical_table_aliases", {}).get(sheet, sheet)
    canonical = canonical_table in tables
    mapping = tables.get(canonical_table) if canonical else manifest.get("flattened", {}).get(table)
    if mapping is None:
        return {}
    fields = mapping["fields"]
    candidates = {"id": mapping["id"]}
    candidates.update({key: [key] for key in fields} if isinstance(fields, list) else fields)
    output = {}
    for key, aliases in candidates.items():
        found = [values[alias] for alias in aliases if alias in values and values[alias] not in (None, "")]
        if found and len({str(value) for value in found}) == 1:
            output[key] = found[0]
    return output


def _discover_accounts(documents, config: dict) -> tuple[int, list[AccountCandidate]]:
    """Index only explicit borrower IDs/names; avoid constructing per-cell evidence or loan evaluations."""
    manifest = config["mapping_manifest"]
    accounts: dict[str, str | None] = {}
    borrower_loans: set[tuple[str, str]] = set()
    for document in documents:
        records = document.metadata.get("records", [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("values"), dict):
                continue
            sheet, values = record.get("sheet"), record["values"]
            if not isinstance(sheet, str):
                continue
            account = _mapped_values(manifest, "Account", sheet, values)
            account_id = scalar_id(account.get("id"))
            if account_id:
                name = account.get("name")
                accounts[account_id] = name.strip()[:200] if isinstance(name, str) and name.strip() else accounts.get(account_id)

            loan = _mapped_values(manifest, "Loan", sheet, values)
            loan_id = scalar_id(loan.get("id"))
            borrower_id = scalar_id(loan.get("borrower_account_id"))
            if loan_id and borrower_id:
                borrower_loans.add((borrower_id, loan_id))
            involvement = _mapped_values(manifest, "EntityInvolvement", sheet, values)
            involvement_loan = scalar_id(involvement.get("loan_id"))
            involvement_account = scalar_id(involvement.get("account_id"))
            role = str(involvement.get("role", "")).strip().casefold()
            if involvement_loan and involvement_account and role == "borrower":
                borrower_loans.add((involvement_account, involvement_loan))
            role_value = next((values.get(key) for key in manifest.get("flattened_role_fields", [])
                               if values.get(key) not in (None, "")), None)
            if loan_id and borrower_id and role_value is not None:
                mapped_role = manifest.get("roles", {}).get(str(role_value).strip().casefold())
                if mapped_role == "borrower":
                    borrower_loans.add((borrower_id, loan_id))
    loans_by_account: dict[str, set[str]] = {}
    for account_id, loan_id in borrower_loans:
        if account_id in accounts:
            loans_by_account.setdefault(account_id, set()).add(loan_id)
    candidates = [AccountCandidate(account_id=account_id, name=accounts[account_id],
                                   loan_count=len(loan_ids), loan_ids=sorted(loan_ids))
                  for account_id, loan_ids in loans_by_account.items()]
    candidates.sort(key=lambda item: ((item.name or item.account_id).casefold(), item.account_id))
    return len({loan_id for _, loan_id in borrower_loans}), candidates


def preflight(session: Session, root: Path) -> PreflightResult:
    if not session.documents:
        raise HTTPException(400, "Upload documents before local validation.")
    detached = session.model_copy(deep=True)
    issues: list[str] = []
    roles: set[int] = set()
    hashes = []
    for doc in detached.documents:
        roles.update(mtb_query_ids(doc.name, doc.metadata))
        try:
            ExtractionAgent().extract(
                doc, root / "uploads" / session.id / doc.id,
                include_text=doc.extension != ".xlsx",
            )
        except (ValueError, OSError):
            # Avoid exposing server paths or parser implementation details.
            issues.append(f"FILE_PARSE_FAILED:{doc.name}: replace with a valid supported file")
            continue
        roles.update(mtb_query_ids(doc.name, doc.metadata))
        hashes.append((doc.id, doc.metadata.get("sha256")))
        if doc.metadata.get("warning"):
            issues.append(f"FILE_REVIEW:{doc.name}:{doc.metadata['warning']}")
    recognized = bool(roles)
    missing_roles = sorted({1, 2, 4} - roles) if recognized else []
    issues.extend(f"MISSING_QUERY_{role}: upload the missing workbook role" for role in missing_roles)
    loans = 0
    account_candidates: list[AccountCandidate] = []
    if recognized and not any(item.startswith("FILE_PARSE_FAILED") for item in issues):
        loans, account_candidates = _discover_accounts(detached.documents, load_underwriting_config())
    elif not recognized:
        issues.append("GENERIC_DOCUMENTS: underwriting schema/relationships were not validated; configure document checks")
    def priority(issue: str) -> int:
        if issue.startswith(("FILE_PARSE_FAILED", "MISSING_QUERY_", "NO_EXPLICIT", "ORPHAN_", "MISSING_", "MULTIPLE_", "CONFLICTING_", "INVALID_")):
            return 0
        if any(key in issue for key in ("identity", "borrower", "report_date", "report_selection", "score_date", "explicit_iso_as_of", "response")):
            return 1
        if "POLICY" in issue or "conflict" in issue:
            return 3
        return 2

    issues = sorted(dict.fromkeys(issues), key=priority)
    # Repeated row-level graph defects should not crowd out other next steps.
    # Keep the total diagnostic count, but group display by code and strip the
    # long evidence-ID list; saved/full reports retain detailed provenance.
    grouped: dict[str, int] = {}
    for issue in issues:
        label = issue.split(" [ev-", 1)[0]
        grouped[label] = grouped.get(label, 0) + 1
    summaries = [f"{label} ({count} affected observations)" if count > 1 else label
                 for label, count in grouped.items()]
    fingerprint = hashlib.sha256(json.dumps({"files": hashes, "mode": session.mode}, sort_keys=True).encode()).hexdigest()
    previous = session.preflight or session.previous_preflight
    comparison = "First local validation; no prior comparison."
    if previous:
        comparison = ("Same file bytes and mode as the last validation. " if previous.input_fingerprint == fingerprint else
                      "Inputs or mode changed since the last validation. ")
        comparison += f"Previous issue count {previous.issue_count}; current {len(issues)}. Counts are diagnostics, not credit outcomes."
    elif session.report and session.report.underwriting:
        comparison = f"Saved report has {len(session.report.underwriting.loan_results)} loans; local validation finds {loans}. The report was not rewritten."
    return PreflightResult(input_fingerprint=fingerprint, recognized_mtb=recognized,
        query_roles=sorted(roles), missing_query_roles=missing_roles, document_count=len(session.documents),
        account_index_version=ACCOUNT_INDEX_VERSION,
        loan_count=loans, account_count=len(account_candidates), account_candidates=account_candidates,
        issue_count=len(issues), priority_issues=summaries[:10], comparison=comparison,
        suggested_next_step=("Inspect structural blockers, select the borrower account/client, then confirm processing. This validation did not evaluate every loan or make a credit decision."
                             if issues else "Select the borrower account/client to scope processing. This validation is not credit clearance."))