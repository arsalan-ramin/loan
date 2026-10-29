"""Allowlisted local assistant operations. No LLM, authority changes or source edits.

Preflight parses a detached copy of uploaded files, never mutates a completed
report, and never treats conversation/review notes as underwriting evidence.
"""

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .agents import ExtractionAgent
from .excel import safe_excel_error_reason
from .loan_loading import load_loan_documents
from .datasets import (account_reference_headers, borrower_role_headers,
                       loan_reference_headers, mtb_query_ids,
                       soql_field_headers, soql_mapping_coverage,
                       soql_account_reference_headers,
                       soql_root_table, soql_unavailable_inputs)
from .models import AccountCandidate, LoanCandidate, LoanInspection, PreflightResult, Session
from .entity_graph import build_entity_graph, scalar_id
from .underwriting_engine import load_underwriting_config
from .underwriting_models import AMOUNT_NAMES, LoanRecommendation

ACCOUNT_INDEX_VERSION = 2


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


def _mapped_values(manifest: dict, table: str, sheet: str, values: dict,
                   *, root_table: str | None = None, headers=None) -> dict:
    plan = _mapping_plan(manifest, table, sheet, values.keys(),
                         root_table=root_table, headers=headers)
    return _apply_mapping_plan(plan, values)


def _mapping_plan(manifest: dict, table: str, sheet: str, value_headers,
                  *, root_table: str | None = None, headers=None) -> dict:
    """Compile aliases without inspecting cell values; preserve alias order."""
    tables = manifest["tables"]
    canonical_table = manifest.get("canonical_table_aliases", {}).get(sheet, sheet)
    canonical = canonical_table in tables
    mapping = tables.get(canonical_table) if canonical else manifest.get("flattened", {}).get(table)
    if mapping is None:
        return {}
    fields = mapping["fields"]
    candidates = {"id": mapping["id"]}
    if table == "Loan":
        candidates["id"] = list(dict.fromkeys([*candidates["id"], *loan_reference_headers(value_headers)]))
    if table == "Account" and root_table:
        query_role = {"EntityInvolvement": 1, "Indicator": 2, "CreditReport": 4}.get(root_table)
        candidates["id"] = soql_account_reference_headers(query_role, headers or value_headers)
    elif table == "Account":
        candidates["id"] = list(dict.fromkeys([*candidates["id"], *account_reference_headers(value_headers)]))
    candidates.update({key: [key] for key in fields} if isinstance(fields, list) else fields)
    if not canonical and root_table:
        candidates["id"] = list(dict.fromkeys([
            *candidates["id"], *soql_field_headers(table, "id", headers or value_headers, root_table=root_table)
        ]))
        for key in list(candidates):
            if key != "id":
                if key in {"account_id", "borrower_account_id"}:
                    candidates[key] = (soql_field_headers(table, key, headers or value_headers, root_table=root_table)
                                       if (table, key) in {("EntityInvolvement", "account_id"),
                                                            ("CreditReport", "account_id")} else [])
                else:
                    candidates[key] = list(dict.fromkeys([
                        *candidates[key],
                        *soql_field_headers(table, key, headers or value_headers, root_table=root_table),
                    ]))
    plan = {}
    for key, aliases in candidates.items():
        if (canonical or not root_table) and table != "Account" and key in {"account_id", "borrower_account_id"}:
            aliases = list(dict.fromkeys([*aliases, *account_reference_headers(value_headers)]))
        present = tuple(alias for alias in aliases if alias in value_headers)
        if present:
            plan[key] = present
    return plan


def _apply_mapping_plan(plan: dict, values: dict) -> dict:
    """Resolve every row independently, including blanks and conflicting aliases."""
    output = {}
    for key, aliases in plan.items():
        found = [values[alias] for alias in aliases if alias in values and values[alias] not in (None, "")]
        if found and len({str(value) for value in found}) == 1:
            output[key] = found[0]
    return output


def _discover_accounts(documents, config: dict,
                       loan_candidates: list[LoanCandidate] | None = None) -> tuple[int, list[AccountCandidate]]:
    """Index all explicit loans separately from verified borrower accounts, without per-cell evidence."""
    manifest = config["mapping_manifest"]
    accounts: dict[str, str | None] = {}
    borrower_loans: set[tuple[str, str]] = set()
    explicit_loans: set[str] = set()
    for document in documents:
        query_roles = mtb_query_ids(document.name, document.metadata)
        query_role = next(iter(query_roles)) if len(query_roles) == 1 else None
        sheet_headers = {item.get("name"): item.get("headers", [])
                         for item in document.metadata.get("sheets", []) if isinstance(item, dict)}
        records = document.metadata.get("records", [])
        if not isinstance(records, list):
            continue
        # Local to this document/config, not a process-wide cache of user data.
        # Excel omits blank cells, so the ordered row keys are part of the schema:
        # sheet headers alone would change fallback and first-alias semantics.
        plans: dict[tuple, tuple] = {}
        roots: dict[str, str | None] = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("values"), dict):
                continue
            sheet, values = record.get("sheet"), record["values"]
            if not isinstance(sheet, str):
                continue
            schema = (sheet, tuple(values))
            if schema not in plans:
                headers = sheet_headers.get(sheet, list(values))
                if sheet in sheet_headers:
                    if sheet not in roots:
                        roots[sheet] = soql_root_table(query_role, headers)
                    root_table = roots[sheet]
                else:
                    root_table = soql_root_table(query_role, headers)
                table_plans = tuple(_mapping_plan(
                    manifest, table, sheet, values.keys(), root_table=root_table, headers=headers,
                ) for table in ("Account", "Loan", "EntityInvolvement"))
                # A canonical Account/junction/report's root Id is not a loan.
                canonical_table = manifest.get("canonical_table_aliases", {}).get(sheet, sheet)
                if canonical_table != "Loan" and "id" in table_plans[1]:
                    table_plans[1]["id"] = tuple(alias for alias in table_plans[1]["id"]
                                                  if alias != "Id")
                role_headers = tuple(dict.fromkeys([
                    *manifest.get("flattened_role_fields", []), *borrower_role_headers(values.keys())
                ]))
                # Highly sparse exports can have a different populated-column
                # subset on every row. Bound schema metadata without dropping
                # any records or caching resolved borrower values.
                if len(plans) >= 128:
                    plans.clear()
                plans[schema] = (*table_plans, role_headers)
            account_plan, loan_plan, involvement_plan, role_headers = plans[schema]
            account = _apply_mapping_plan(account_plan, values)
            account_id = scalar_id(account.get("id"))
            if account_id:
                name = account.get("name")
                accounts[account_id] = name.strip()[:200] if isinstance(name, str) and name.strip() else accounts.get(account_id)

            loan = _apply_mapping_plan(loan_plan, values)
            loan_id = scalar_id(loan.get("id"))
            borrower_id = scalar_id(loan.get("borrower_account_id"))
            direct_borrower_id = scalar_id(values.get("borrower_account_id"))
            role_value = next((values.get(key) for key in role_headers
                               if values.get(key) not in (None, "")), None)
            mapped_role = manifest.get("roles", {}).get(str(role_value).strip().casefold()) if role_value is not None else None
            if loan_id:
                explicit_loans.add(loan_id)
            if loan_id and direct_borrower_id and (role_value is None or mapped_role == "borrower"):
                borrower_loans.add((direct_borrower_id, loan_id))
            if loan_id and borrower_id and mapped_role == "borrower":
                borrower_loans.add((borrower_id, loan_id))
            involvement = _apply_mapping_plan(involvement_plan, values)
            involvement_loan = scalar_id(involvement.get("loan_id"))
            involvement_account = scalar_id(involvement.get("account_id"))
            role = str(involvement.get("role", "")).strip().casefold()
            if involvement_loan:
                explicit_loans.add(involvement_loan)
            if involvement_loan and involvement_account and role == "borrower":
                borrower_loans.add((involvement_account, involvement_loan))
    loans_by_account: dict[str, set[str]] = {}
    borrowers_by_loan: dict[str, set[str]] = {}
    for account_id, loan_id in borrower_loans:
        borrowers_by_loan.setdefault(loan_id, set()).add(account_id)
        if account_id in accounts:
            loans_by_account.setdefault(account_id, set()).add(loan_id)
    candidates = [AccountCandidate(account_id=account_id, name=accounts[account_id],
                                   loan_count=len(loan_ids), loan_ids=sorted(loan_ids))
                  for account_id, loan_ids in loans_by_account.items()]
    candidates.sort(key=lambda item: ((item.name or item.account_id).casefold(), item.account_id))
    if loan_candidates is not None:
        for loan_id in sorted(explicit_loans):
            borrowers = borrowers_by_loan.get(loan_id, set())
            borrower = next(iter(borrowers)) if len(borrowers) == 1 else None
            verified = borrower if borrower in accounts else None
            loan_candidates.append(LoanCandidate(loan_id=loan_id, account_id=verified,
                                                 account_name=accounts.get(verified)))
    return len(explicit_loans), candidates


def preflight(session: Session, root: Path) -> PreflightResult:
    if not session.documents:
        raise HTTPException(400, "Upload documents before local validation.")
    detached = session.model_copy(deep=True)
    issues: list[str] = []
    roles: set[int] = set()
    roles_by_document: list[set[int]] = []
    hashes = []
    for doc in detached.documents:
        try:
            ExtractionAgent().extract(
                doc, root / "uploads" / session.id / doc.id,
                include_text=doc.extension != ".xlsx",
                headers_only=doc.extension == ".xlsx",
            )
        except (ValueError, OSError) as exc:
            # Avoid exposing server paths or parser implementation details.
            reason = (safe_excel_error_reason(exc) if doc.extension == ".xlsx"
                      else "replace with a valid supported file")
            issues.append(f"FILE_PARSE_FAILED:{doc.name}: {reason}")
            continue
        document_roles = mtb_query_ids(doc.name, doc.metadata)
        roles_by_document.append(document_roles)
        roles.update(document_roles)
        hashes.append((doc.id, doc.metadata.get("sha256")))
        if doc.metadata.get("warning"):
            issues.append(f"FILE_REVIEW:{doc.name}:{doc.metadata['warning']}")
    requires_loan_number = any(doc.extension == ".xlsx" for doc in detached.documents) and not session.selected_loan_id
    if session.selected_loan_id and not any(item.startswith("FILE_PARSE_FAILED") for item in issues):
        try:
            load_loan_documents(detached.documents, root / "uploads" / session.id, session.selected_loan_id)
        except (ValueError, OSError) as exc:
            issues.append("FILE_PARSE_FAILED: " + safe_excel_error_reason(exc))
    recognized = bool(roles)
    # The three source exports are separate inputs; cross-file links use only
    # normalized explicit Account foreign-key columns, never positions/names.
    required_roles = {1, 2, 4}
    missing_roles = sorted(required_roles - roles) if recognized else []
    role_counts = {role: sum(role in item for item in roles_by_document) for role in required_roles}
    duplicate_roles = sorted(role for role, count in role_counts.items() if count > 1)
    query_file_set_valid = bool(
        recognized and len(detached.documents) == 3
        and len(roles_by_document) == 3
        and all(len(item) == 1 for item in roles_by_document)
        and all(count == 1 for count in role_counts.values())
    )
    if recognized and not query_file_set_valid:
        issues.append("QUERY_FILE_SET_INCOMPLETE_OR_AMBIGUOUS: upload exactly one Query 1, Query 2 and Query 4 workbook")
    if duplicate_roles:
        issues.append("DUPLICATE_QUERY_ROLES:" + ",".join(map(str, duplicate_roles)))
    loans = 0
    account_candidates: list[AccountCandidate] = []
    loan_candidates: list[LoanCandidate] = []
    unmapped_attributes: set[str] = set()
    mapped_fields: set[str] = set()
    mapping_warnings: set[str] = set()
    if (not requires_loan_number and query_file_set_valid and roles == required_roles
            and not any(item.startswith("FILE_PARSE_FAILED") for item in issues)):
        config = load_underwriting_config()
        loans, account_candidates = _discover_accounts(detached.documents, config, loan_candidates)
        graph = build_entity_graph(detached.documents, config)
        issues.extend(graph.issues)
        if not graph.table("Loan"):
            issues.append("NO_EXPLICIT_LOAN_RECORDS: continue with a diagnostic-only report; no loan records will be invented")
        mapping_warnings.update(graph.mapping_warnings)
        unmapped_attributes.update(graph.unmapped_source_fields)
        mapped_fields.update(graph.mapped_fields)
        for doc in detached.documents:
            query_ids = mtb_query_ids(doc.name, doc.metadata)
            if len(query_ids) != 1:
                continue
            query_role = next(iter(query_ids))
            for sheet in doc.metadata.get("sheets", []):
                if sheet.get("is_reference"):
                    continue
                headers = sheet.get("headers", [])
                root_table = soql_root_table(query_role, headers)
                canonical_table = config["mapping_manifest"].get("canonical_table_aliases", {}).get(sheet.get("name"), sheet.get("name"))
                if root_table is None and canonical_table not in config["mapping_manifest"]["tables"]:
                    unmapped_attributes.update(header for header in headers if isinstance(header, str))
                    mapping_warnings.add(f"SOQL_ROOT_ID_MAPPING_UNRESOLVED:Query{query_role}")
                    continue
                mapped_headers, targets = soql_mapping_coverage(
                    query_role, headers, config["mapping_manifest"],
                    AMOUNT_NAMES, sheet_name=sheet.get("name"),
                )
                mapped_fields.update(targets)
                unmapped_attributes.update(
                    header for header in headers if isinstance(header, str) and header not in mapped_headers
                )
    elif not recognized and not requires_loan_number:
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
    fingerprint = hashlib.sha256(json.dumps({"files": hashes, "mode": session.mode,
        "loan_id": session.selected_loan_id}, sort_keys=True).encode()).hexdigest()
    previous = session.preflight or session.previous_preflight
    comparison = "First local validation; no prior comparison."
    if previous:
        comparison = ("Same file bytes and mode as the last validation. " if previous.input_fingerprint == fingerprint else
                      "Inputs or mode changed since the last validation. ")
        comparison += f"Previous issue count {previous.issue_count}; current {len(issues)}. Counts are diagnostics, not credit outcomes."
    elif session.report and session.report.underwriting:
        comparison = f"Saved report has {len(session.report.underwriting.loan_results)} loans; local validation finds {loans}. The report was not rewritten."
    attributes = sorted({str(header) for doc in detached.documents
                         for sheet in doc.metadata.get("sheets", [])
                         for header in sheet.get("headers", [])
                         if isinstance(header, str) and header.strip()})
    return PreflightResult(input_fingerprint=fingerprint, recognized_mtb=recognized,
        requires_loan_number=requires_loan_number, scoped_loan_id=session.selected_loan_id,
        query_roles=sorted(roles), missing_query_roles=missing_roles,
        duplicate_query_roles=duplicate_roles, query_file_set_valid=query_file_set_valid,
        document_count=len(session.documents),
        attribute_count=len(attributes), attributes=attributes[:500],
        mapping_warnings=sorted(mapping_warnings),
        unmapped_attribute_count=len(unmapped_attributes),
        unmapped_attributes=sorted(unmapped_attributes)[:200],
        unavailable_inputs=soql_unavailable_inputs(mapped_fields) if query_file_set_valid else [],
        account_index_version=ACCOUNT_INDEX_VERSION,
        loan_count=loans, account_count=len(account_candidates), account_candidates=account_candidates,
        loan_candidates=loan_candidates,
        issue_count=len(issues), priority_issues=summaries[:10], comparison=comparison,
        suggested_next_step=("A workbook could not be read. Check its FILE_PARSE_FAILED reason above, correct or replace that upload, then validate again. This is a parsing failure, not a missing field mapping."
                     if any(item.startswith("FILE_PARSE_FAILED") for item in issues) else
                     "What loan number would you like to review? Workbook data has not been loaded. Enter the exact loan number to load only its explicitly related rows."
                     if requires_loan_number else
                     "Select an explicit loan number, then confirm processing. No unique verified borrower is available for some loans; missing or ambiguous borrower evidence remains UNKNOWN. Guarantors are never treated as borrowers. This is not a credit decision."
                     if loan_candidates and any(item.account_id is None for item in loan_candidates) else
                     "Mappings are incomplete. You can prepare a diagnostic-only review without selecting an account. Available evidence will be retained; unmapped inputs remain UNKNOWN and no borrower relationships or loan records will be invented."
                     if query_file_set_valid and not account_candidates else
                     "Review extracted attributes, select the intended borrower account/client, then confirm processing. Query files link only through matching explicit Account ID columns, never row position or name. This is not a credit decision."
                             if issues else "Select the borrower account/client to scope processing. This validation is not credit clearance."))


_INVENTORY_WORDS = re.compile(r"\b(?:attribute|attributes|field|fields|column|columns)\b", re.IGNORECASE)
_INVENTORY_ACTIONS = re.compile(r"\b(?:list|show|display|enumerate|table|all|every)\b", re.IGNORECASE)
_GENERIC_LOAN_WORDS = {"all", "every", "attributes", "attribute", "fields", "field", "columns", "column", "table", "data", "details", "information", "list", "show"}
_INVENTORY_PAGE_SIZE = 100


def is_loan_attribute_inventory_request(query: str) -> bool:
    """Recognize explicit exhaustive loan-attribute requests for local handling."""
    return bool(re.search(r"\bloan\b", query, re.IGNORECASE)
                and _INVENTORY_WORDS.search(query)
                and _INVENTORY_ACTIONS.search(query))


def _inventory_explicit_ids(query: str, known_ids: set[str]) -> tuple[set[str], set[str]]:
    """Return explicitly mentioned known IDs and syntactically marked unknown IDs."""
    known = {loan_id for loan_id in known_ids if re.search(
        rf"(?<![A-Za-z0-9_-]){re.escape(loan_id)}(?![A-Za-z0-9_-])", query)}
    unknown: set[str] = set()
    marker = re.compile(r"\bloan(?:\s*[_ -]?\s*(?:id|number))?\s*[:#=]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,127})", re.IGNORECASE)
    for match in marker.finditer(query):
        candidate = match.group(1)
        if candidate.casefold() not in _GENERIC_LOAN_WORDS and candidate not in known_ids:
            unknown.add(candidate)
    return known, unknown


def _inventory_value(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _markdown_cell(value: str) -> str:
    escaped = html.escape(value, quote=True).replace("|", "&#124;").replace("`", "&#96;")
    return escaped.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def loan_attribute_inventory(session: Session, root: Path, query: str,
                             requested_loan_id: str | None = None) -> str:
    """Render exact source-row attributes for one unambiguously selected loan.

    The operation is local and read-only. Source workbook rows remain separate
    observations; no graph conflict is resolved and no report field is changed.
    """
    report = session.report
    loans = report.underwriting.loan_results if (
        session.status == "completed" and report is not None and report.underwriting is not None
    ) else []
    by_id = {loan.loan_id: loan for loan in loans}
    known_ids = set(by_id)
    mentioned_ids, unknown_ids = _inventory_explicit_ids(query, known_ids)

    if unknown_ids:
        return ("I can’t match the explicitly requested loan ID "
                + ", ".join(_markdown_cell(item) for item in sorted(unknown_ids))
                + " to a loan in this session’s completed report. Please select an existing loan ID; no other loan was substituted.")
    if len(mentioned_ids) > 1:
        return "This request names multiple loans. Please request the attribute inventory for one exact loan ID at a time."
    if requested_loan_id and requested_loan_id not in known_ids:
        return (f"I can’t match the focused loan ID {_markdown_cell(requested_loan_id)} to a loan in this session’s completed report. "
                "Please select an existing loan ID; no other loan was substituted.")
    mentioned_id = next(iter(mentioned_ids), None)
    if requested_loan_id and mentioned_id and requested_loan_id != mentioned_id:
        return (f"The focused loan ID {_markdown_cell(requested_loan_id)} conflicts with the loan named in your message "
                f"({_markdown_cell(mentioned_id)}). Please choose one exact loan ID.")
    selected_id = requested_loan_id or mentioned_id
    if selected_id is None:
        if len(loans) == 1:
            selected_id = loans[0].loan_id
        elif len(loans) > 1:
            return "This completed report contains multiple loans. Please specify one exact loan ID (for example, ‘loan L1’); no loan was selected automatically."
        else:
            return "A completed report with an explicit loan is required for a source-attribute inventory. No loan attributes were inferred from unrelated document text."

    config = load_underwriting_config()
    manifest = config["mapping_manifest"]
    documents = []
    for original in session.documents:
        document = original.model_copy(deep=True)
        if not isinstance(document.metadata.get("records"), list):
            # Re-extract only from this session's fixed opaque upload location.
            uploads_root = (root / "uploads").resolve()
            session_dir = (uploads_root / session.id).resolve()
            path = session_dir / document.id
            try:
                if (session_dir.parent != uploads_root or session_dir.is_symlink()
                        or path.is_symlink() or path.resolve().parent != session_dir
                        or not path.is_file()):
                    continue
                load_loan_documents([document], session_dir, selected_id)
            except (OSError, ValueError):
                continue
        documents.append(document)

    if not documents:
        return (f"## Loan attribute inventory — {_markdown_cell(selected_id)}\n\n"
                "Source row metadata is unavailable and the session’s fixed uploaded files could not be safely re-read. "
                "No provider was called and no report data was changed.")

    selected_loan = by_id[selected_id]
    account_ids: set[str] = set()
    report_account = scalar_id(selected_loan.borrower_account_id)
    if report_account:
        account_ids.add(report_account)
    row_matches: list[tuple[object, dict, str, int, dict, str]] = []
    sheet_maps = []
    for document in documents:
        query_roles = mtb_query_ids(document.name, document.metadata)
        query_role = next(iter(query_roles)) if len(query_roles) == 1 else None
        headers_by_sheet = {item.get("name"): item.get("headers", [])
                            for item in document.metadata.get("sheets", []) if isinstance(item, dict)}
        reference_sheets = {item.get("name") for item in document.metadata.get("sheets", [])
                            if isinstance(item, dict) and item.get("is_reference")}
        sheet_maps.append((document, query_role, headers_by_sheet, reference_sheets))
        for record in document.metadata.get("records", []):
            if not isinstance(record, dict) or not isinstance(record.get("values"), dict):
                continue
            sheet, values = record.get("sheet"), record["values"]
            if not isinstance(sheet, str) or sheet in reference_sheets:
                continue
            headers = headers_by_sheet.get(sheet, list(values))
            root_table = soql_root_table(query_role, headers)
            loan_data = _mapped_values(manifest, "Loan", sheet, values, root_table=root_table, headers=headers)
            row_loan_id = scalar_id(loan_data.get("id"))
            explicit_loan_ids = {scalar_id(values[key]) for key in loan_reference_headers(values.keys())
                                 if values.get(key) not in (None, "")}
            explicit_loan_ids.discard(None)
            direct_match = row_loan_id == selected_id or explicit_loan_ids == {selected_id}
            if direct_match:
                row_matches.append((document, record, sheet, record.get("row", 0), headers, "loan-linked source row"))
                account_value = scalar_id(loan_data.get("borrower_account_id"))
                if account_value:
                    account_ids.add(account_value)
                involvement = _mapped_values(manifest, "EntityInvolvement", sheet, values,
                                             root_table=root_table, headers=headers)
                if (str(involvement.get("role", "")).strip().casefold() == "borrower"
                        and scalar_id(involvement.get("account_id"))):
                    account_ids.add(scalar_id(involvement["account_id"]))

    # Include account-level rows only when the row is explicitly an Account
    # record with an exact matching Account ID and has no conflicting Loan FK.
    for document, query_role, headers_by_sheet, reference_sheets in sheet_maps:
        aliases = manifest.get("canonical_table_aliases", {})
        for record in document.metadata.get("records", []):
            if not isinstance(record, dict) or not isinstance(record.get("values"), dict):
                continue
            sheet, values = record.get("sheet"), record["values"]
            if not isinstance(sheet, str) or sheet in reference_sheets:
                continue
            canonical_table = aliases.get(sheet, sheet)
            if canonical_table != "Account":
                continue
            headers = headers_by_sheet.get(sheet, list(values))
            root_table = soql_root_table(query_role, headers)
            account_data = _mapped_values(manifest, "Account", sheet, values,
                                          root_table=root_table, headers=headers)
            account_id = scalar_id(account_data.get("id"))
            direct_ids = {scalar_id(values[key]) for key in loan_reference_headers(values.keys())
                          if values.get(key) not in (None, "")}
            direct_ids.discard(None)
            if account_id in account_ids and not direct_ids:
                row_matches.append((document, record, sheet, record.get("row", 0), headers,
                                    f"account-level context · Account ID {_markdown_cell(account_id)}"))

    page_match = re.search(r"\bpage\s+(\d+)\b", query, re.IGNORECASE)
    page = max(1, int(page_match.group(1))) if page_match else 1
    requested_start = (page - 1) * _INVENTORY_PAGE_SIZE
    observations = []
    total = 0
    for document, record, sheet, row, headers, scope in row_matches:
        values = record["values"]
        sources = record.get("sources") if isinstance(record.get("sources"), dict) else {}
        for attribute in headers:
            if not isinstance(attribute, str) or not attribute.strip():
                continue
            if attribute not in values:
                value, status = "not provided (blank or absent cell)", "not provided"
            elif values[attribute] is None:
                value, status = "null", "null"
            elif values[attribute] == "":
                value, status = "not provided (blank cell)", "not provided"
            else:
                value, status = _inventory_value(values[attribute]), "present"
            source = f"{document.name} / {sheet} / row {row}"
            if isinstance(sources.get(attribute), str):
                source += f" / {sources[attribute]}"
            if requested_start <= total < requested_start + _INVENTORY_PAGE_SIZE:
                observations.append((attribute, value, status, source, scope))
            total += 1

    if total == 0:
        return (f"## Loan attribute inventory — {_markdown_cell(selected_id)}\n\n"
                "No source rows with an exact explicit Loan ID were found, and no explicitly identified borrower Account rows were available. "
                "Row order, names, report summaries, and other loans were not used to invent a relationship.")

    page_count = (total + _INVENTORY_PAGE_SIZE - 1) // _INVENTORY_PAGE_SIZE
    if page > page_count:
        return (f"## Loan attribute inventory — {_markdown_cell(selected_id)}\n\n"
                f"Page {page} is outside the available range (1–{page_count}); {total} source attribute observations remain available. "
                "No provider was called and no report data was changed.")
    start = requested_start
    selected_observations = observations
    lines = [f"## Loan attribute inventory — {_markdown_cell(selected_id)}", "",
             f"Showing source attribute observations {start + 1}–{start + len(selected_observations)} of {total} · page {page} of {page_count}.",
             (f"To view remaining observations, ask for page {page + 1}." if page < page_count else "This is the final page."),
             "Each row is a preserved source observation; duplicates and disagreements are not merged or resolved. Status distinguishes present, null, and not provided.",
             "Scope: rows with an exact explicit Loan ID; separately labeled Account rows require an exact explicit borrower Account ID. Other loans, row order, and names are not used to join data.",
             "Source data inventory only—not an underwriting evaluation, credit decision, or change to the saved report.", "",
             "| Attribute | Source value | Status | Workbook / sheet / row | Scope |",
             "| --- | --- | --- | --- | --- |"]
    lines.extend("| " + " | ".join(_markdown_cell(str(item)) for item in observation) + " |"
                 for observation in selected_observations)
    lines.extend(["", "No Anthropic request was made. Raw source values remain in this local assistant response and are excluded from future provider history and memory indexing."])
    return "\n".join(lines)