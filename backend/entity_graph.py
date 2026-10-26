"""Explicit Salesforce graph, without positional joins or implicit indicator pivots.

``build_entity_graph(documents, config)`` accepts Document-like objects with name,
id and metadata.records. Only MTB-recognized workbooks or exact canonical sheet
names in the manifest participate. Node keys are (table, explicit source ID).
Id-less indicator observations use local provenance keys, never Salesforce IDs.
Conflicting duplicates are retained in provenance and poisoned, not overwritten.
Batch-wide issues intentionally hold *all* loans until the input is reconciled.
"""

from __future__ import annotations

import hashlib
import re
from copy import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from .datasets import (account_reference_headers, borrower_role_headers,
                       loan_reference_headers, mtb_query_ids,
                       soql_field_headers, soql_root_table,
                       soql_mapping_coverage, soql_tables_for_role)
from .underwriting_models import AMOUNT_NAMES, SourceEvidence, exact_decimal

MONEY_FIELDS = set(AMOUNT_NAMES) - {"effective_tgc"}
SIGNED_MONEY_FIELDS = {"ebitda", "business_net_income", "net_worth", "prior_profit", "current_profit"}
ID_FIELDS = {
    "loan_id", "account_id", "borrower_account_id", "response_id", "entity_id",
    "analysis_id", "entity_involvement_id",
}


def scalar_id(value: Any) -> str | None:
    """Explicit ID scalar only, not an object, formula, repr, number or bool."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value.strip()):
        return value.strip()
    return None


def iso_date(value: Any) -> date | None:
    """ISO calendar dates only; ambiguous locale dates and timestamps fail closed."""
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _canonical_soql_product(value: Any) -> Any:
    """Map only known SOQL product labels; leave all other labels untouched."""
    if not isinstance(value, str):
        return value
    return {
        "express baloc": "express_baloc",
        "sba baloc": "sba_baloc",
        "conventional baloc": "conventional_baloc",
        "bil": "bil",
        "sba bil": "sba_bil",
        "term loan": "term_loan",
    }.get(value.strip().casefold(), value)


@dataclass
class EntityNode:
    table: str
    id: str
    fields: dict[str, Any] = field(default_factory=dict)
    evidence: list[SourceEvidence] = field(default_factory=list)
    field_refs: dict[str, list[str]] = field(default_factory=dict)
    poisoned: set[str] = field(default_factory=set)


@dataclass
class EntityGraph:
    nodes: dict[str, dict[str, EntityNode]] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    mapping_warnings: list[str] = field(default_factory=list)
    unmapped_source_fields: set[str] = field(default_factory=set)
    mapped_fields: set[str] = field(default_factory=set)
    has_soql_exports: bool = False
    # Explicit relationship observations, including flattened borrower roles.
    associations: list[tuple[str, str, str, list[str]]] = field(default_factory=list)

    def issue(self, code: str, refs: Iterable[str] = ()) -> None:
        # Only codes and provenance IDs; no account/borrower values in diagnostics.
        message = code + (" [" + ",".join(sorted(set(refs))) + "]" if refs else "")
        if message not in self.issues:
            self.issues.append(message)

    def table(self, name: str) -> dict[str, EntityNode]:
        return self.nodes.setdefault(name, {})

    def add(self, table: str, node_id: str, values: dict[str, Any], evidence: list[SourceEvidence],
            refs: dict[str, list[str]]) -> EntityNode:
        node = self.table(table).setdefault(node_id, EntityNode(table, node_id))
        node.evidence.extend(e for e in evidence if e.id not in {old.id for old in node.evidence})
        for key, value in values.items():
            node.field_refs.setdefault(key, []).extend(refs.get(key, []))
            if value is None or value == "":
                continue
            if key in node.poisoned:
                continue
            identical = (node.fields.get(key) == value if isinstance(value, Decimal)
                         and isinstance(node.fields.get(key), Decimal) else str(node.fields.get(key)) == str(value))
            if key in node.fields and not identical:
                node.fields[key] = None
                node.poisoned.add(key)
                self.issue(f"CONFLICTING_DUPLICATE:{table}:{key}", node.field_refs[key])
            else:
                node.fields[key] = value
        return node

    def related(self, table: str, key: str, value: str) -> list[EntityNode]:
        return [node for node in self.table(table).values() if node.fields.get(key) == value]

    def borrowers(self, loan_id: str) -> set[str]:
        linked = {account for loan, account, role, _ in self.associations
                  if loan == loan_id and role == "borrower"}
        direct = self.table("Loan").get(loan_id)
        if direct is not None:
            account_id = direct.fields.get("borrower_account_id")
            if account_id in self.table("Account"):
                linked.add(account_id)
        return linked

    def loan_accounts(self, loan_id: str) -> set[str]:
        """All accounts explicitly linked to a loan through direct FK or junction."""
        accounts = {account for loan, account, _, _ in self.associations if loan == loan_id}
        direct = self.table("Loan").get(loan_id)
        if direct is not None:
            account_id = direct.fields.get("borrower_account_id")
            if account_id in self.table("Account"):
                accounts.add(account_id)
        return accounts


def build_entity_graph(documents: Iterable[Any], config: dict[str, Any], *, selected_account_id: str | None = None) -> EntityGraph:
    """Build and validate all one-to-many relationships using the explicit manifest.

    A canonical Loan.borrower_account_id is an explicit direct borrower link;
    flattened Account ID alone is not. Identity_Mapping_Validated is ignored.
    IDs are opaque explicit scalars (synthetic IDs are allowed in test fixtures).
    """
    graph = EntityGraph()
    documents = list(documents)
    manifest = config["mapping_manifest"]
    if selected_account_id is not None:
        documents = _scope_documents_to_borrower(documents, manifest, selected_account_id)
    tables = manifest["tables"]
    canonical_aliases = manifest.get("canonical_table_aliases", {})
    for document_index, document in enumerate(documents):
        metadata = document.metadata
        query_roles = mtb_query_ids(document.name, metadata)
        recognized = bool(query_roles)
        soql_role = next(iter(query_roles)) if len(query_roles) == 1 else None
        records = metadata.get("records", [])
        if not isinstance(records, list):
            graph.issue("INVALID_RECORDS_CONTAINER")
            continue
        reference = {sheet.get("name") for sheet in metadata.get("sheets", []) if sheet.get("is_reference")}
        sheet_headers = {item.get("name"): item.get("headers", [])
                 for item in metadata.get("sheets", []) if isinstance(item, dict)}
        if soql_role in {1, 2, 4}:
            for sheet_name, headers in sheet_headers.items():
                root_table_for_sheet = soql_root_table(soql_role, headers)
                if root_table_for_sheet:
                    graph.has_soql_exports = True
                mapped_headers, mapped_targets = soql_mapping_coverage(
                    soql_role, headers, manifest, AMOUNT_NAMES,
                )
                graph.unmapped_source_fields.update(
                    header for header in headers if isinstance(header, str) and header not in mapped_headers
                )
                graph.mapped_fields.update(mapped_targets)
        if recognized and not records:
            graph.issue("EMPTY_RECOGNIZED_WORKBOOK")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                graph.issue("INVALID_RECORD")
                continue
            sheet, row, values = record.get("sheet"), record.get("row"), record.get("values")
            if sheet in reference:
                continue
            canonical_table = canonical_aliases.get(sheet, sheet)
            canonical = canonical_table in tables
            if not recognized and not canonical:
                continue
            if not isinstance(sheet, str) or not isinstance(row, int) or isinstance(row, bool) or row < 1 or not isinstance(values, dict):
                graph.issue("INVALID_RECORD_SHAPE")
                continue
            prefix = f"{document_index}:{document.id}:{sheet}:{row}:{index}"
            headers = sheet_headers.get(sheet, [])
            root_table = soql_root_table(soql_role, headers) if not canonical else None
            if canonical:
                mappings = {canonical_table: tables[canonical_table]}
            elif soql_role in {1, 2, 4} and root_table:
                allowed_tables = soql_tables_for_role(soql_role)
                mappings = {table: manifest["flattened"][table]
                            for table in allowed_tables if table in manifest["flattened"]}
            else:
                mappings = manifest["flattened"]
            row_nodes: dict[str, EntityNode] = {}
            row_evidence: list[str] = []
            for table, mapping in mappings.items():
                mapping = dict(mapping)
                mapping["id"] = list(mapping["id"])
                if table == "Loan":
                    mapping["id"].extend(loan_reference_headers(values.keys()))
                if table == "Account":
                    mapping["id"].extend(account_reference_headers(values.keys()))
                raw_fields = mapping["fields"]
                field_map = ({key: [key] for key in raw_fields} if isinstance(raw_fields, list)
                             else {key: list(aliases) for key, aliases in raw_fields.items()})
                if not canonical and root_table:
                    mapping["id"].extend(soql_field_headers(table, "id", headers, root_table=root_table))
                    for key, aliases in field_map.items():
                        aliases.extend(soql_field_headers(table, key, headers, root_table=root_table))
                        field_map[key] = list(dict.fromkeys(aliases))
                # Expanded screenshot names are unverified. New fields are
                # accepted only as exact canonical columns on canonical sheets.
                if canonical:
                    for key in manifest.get("expanded_canonical_fields", {}).get(table, []):
                        field_map.setdefault(key, [key])
                for key, aliases in manifest.get("verified_usd_source_fields", {}).get(table, {}).items():
                    field_map.setdefault(key, [key]).extend(aliases)
                if table == "CreditReport":
                    for key in ("personal_fico", "report_category", "report_role"):
                        field_map.setdefault(key, [key])
                for key, aliases in field_map.items():
                    if table != "Account" and key in {"account_id", "borrower_account_id"}:
                        aliases.extend(account_reference_headers(values.keys()))
                        field_map[key] = list(dict.fromkeys(aliases))
                    if key in AMOUNT_NAMES:
                        aliases.append(AMOUNT_NAMES[key])
                    if key == "tier_i" and manifest.get("group_credit_tier1_scope_confirmed") is not True:
                        field_map[key] = [alias for alias in aliases if alias != "Group_Credit_Amount__c"]
                relevant = {alias for aliases in field_map.values() for alias in aliases} | set(mapping["id"])
                if not any(key in values and values[key] is not None and values[key] != "" for key in relevant):
                    continue
                fields: dict[str, Any] = {}
                refs: dict[str, list[str]] = {}
                evidence: list[SourceEvidence] = []

                def select(target: str, aliases: list[str]) -> Any:
                    observed = []
                    for alias in aliases:
                        if alias not in values or values[alias] is None or values[alias] == "":
                            continue
                        raw = values[alias]
                        evidence_id = "ev-" + hashlib.sha256(f"{prefix}:{alias}".encode()).hexdigest()[:24]
                        safe = raw if isinstance(raw, (str, bool)) else str(raw) if isinstance(raw, (int, date)) else None
                        # Decimal values preserve lexical precision; floats/objects are omitted.
                        if isinstance(raw, Decimal):
                            safe = str(raw)
                        evidence.append(SourceEvidence(id=evidence_id, workbook=document.name, sheet=sheet,
                                                       row=row, field=alias, value=safe))
                        refs.setdefault(target, []).append(evidence_id)
                        observed.append(raw)
                    if len({str(item) for item in observed}) > 1:
                        graph.issue(f"CONFLICTING_ALIASES:{table}:{target}", refs[target])
                        return None
                    return observed[0] if observed else None

                node_id = select("id", mapping["id"])
                for key, aliases in field_map.items():
                    value = select(key, aliases)
                    if value is None:
                        continue
                    if key in ID_FIELDS:
                        if scalar_id(value) is None:
                            graph.issue(f"INVALID_RELATION_ID:{table}:{key}", refs.get(key, []))
                            continue
                        value = scalar_id(value)
                    if not canonical and table == "Loan" and key == "product":
                        value = _canonical_soql_product(value)
                    fields[key] = value
                # A shared flattened currency/scale applies to this row, not to
                # other rows with the same entity ID.
                currency = values.get("currency", config.get("source_currency"))
                scale = values.get("amount_scale", config.get("amount_scale"))
                for key in MONEY_FIELDS & fields.keys():
                    try:
                        if currency != "USD" or scale != "actual_dollars":
                            raise ValueError("Unknown currency or units")
                        amount = exact_decimal(fields[key])
                        if key not in SIGNED_MONEY_FIELDS and amount < 0:
                            raise ValueError("Negative exposure")
                        fields[key] = amount
                    except ValueError:
                        fields[key] = None
                        graph.issue(f"INVALID_USD_AMOUNT:{table}:{key}", refs.get(key, []))
                if node_id is None and table in {"Indicator", "Characteristic"} and fields.get("key"):
                    # Local provenance storage key only; it is not a Salesforce
                    # ID and never supplies a missing analysis relationship.
                    storage_id = "observation:" + prefix + ":" + table
                else:
                    storage_id = scalar_id(node_id)
                # On flattened rows, a generic currency alone must not fabricate
                # missing report/response entities. Entity-specific data must exist.
                specific = set(fields) - {"currency", "amount_scale", "account_id", "loan_id", "response_id", "entity_id", "analysis_id"}
                if storage_id is None:
                    if not canonical and table == "EntityInvolvement" and root_table != "EntityInvolvement":
                        continue
                    if canonical or node_id is not None or specific:
                        graph.issue(f"MISSING_OR_INVALID_ID:{table}", (e.id for e in evidence))
                    continue
                for item in evidence:
                    item.record_id = scalar_id(node_id)
                node = graph.add(table, storage_id, fields, evidence, refs)
                row_nodes[table] = node
                row_evidence.extend(e.id for e in evidence)
                if table in {"Indicator", "Characteristic"} and not fields.get("analysis_id"):
                    warning = f"UNMAPPED_RELATION:{table}:analysis_id"
                    if warning not in graph.mapping_warnings:
                        graph.mapping_warnings.append(warning)
            if not canonical and "Loan" in row_nodes and "Account" in row_nodes:
                role_headers = list(dict.fromkeys([
                    *manifest["flattened_role_fields"], *borrower_role_headers(values.keys())
                ]))
                roles = [values[key] for key in role_headers if values.get(key) is not None]
                approved = {manifest["roles"].get(str(role).strip().casefold()) for role in roles}
                if len(approved) == 1 and None not in approved:
                    for key in role_headers:
                        if key not in values or values[key] is None:
                            continue
                        evidence_id = "ev-" + hashlib.sha256(f"{prefix}:{key}".encode()).hexdigest()[:24]
                        role_evidence = SourceEvidence(id=evidence_id, workbook=document.name, sheet=sheet, row=row,
                                                       record_id=row_nodes["Loan"].id, field=key, value=str(values[key]))
                        row_nodes["Loan"].evidence.append(role_evidence)
                        row_nodes["Loan"].field_refs.setdefault("identity", []).append(evidence_id)
                        row_evidence.append(evidence_id)
                    graph.associations.append((row_nodes["Loan"].id, row_nodes["Account"].id, next(iter(approved)), row_evidence))
                elif roles:
                    graph.issue("INVALID_OR_CONFLICTING_ROLE", row_evidence)
    _validate_relationships(graph, manifest)
    return graph


def _scope_documents_to_borrower(documents: Iterable[Any], manifest: dict[str, Any], account_id: str) -> list[Any]:
    """Keep only records explicitly connected to one borrower account.

    The first pass finds the selected account's loan IDs and the second resolves
    related accounts, responses, entities and analyses before retaining evidence
    rows. No row-order or name-based relationships are used.
    """
    source_documents = list(documents)
    tables = manifest["tables"]
    aliases = manifest.get("canonical_table_aliases", {})

    def mapped(table: str, sheet: Any, values: dict[str, Any]) -> dict[str, Any]:
        canonical_table = aliases.get(sheet, sheet)
        canonical = canonical_table in tables
        mapping = tables.get(canonical_table) if canonical else manifest.get("flattened", {}).get(table)
        if mapping is None:
            return {}
        raw_fields = mapping["fields"]
        field_map = ({key: [key] for key in raw_fields} if isinstance(raw_fields, list)
                     else {key: list(names) for key, names in raw_fields.items()})
        id_headers = list(mapping["id"])
        if table == "Loan":
            id_headers.extend(loan_reference_headers(values.keys()))
        if table == "Account":
            id_headers.extend(account_reference_headers(values.keys()))
        if table != "Account":
            for key in ("account_id", "borrower_account_id"):
                if key in field_map:
                    field_map[key] = list(dict.fromkeys(
                        [*field_map[key], *account_reference_headers(values.keys())]
                    ))
        names = {"id": list(dict.fromkeys(id_headers))}
        names.update(field_map)
        result = {}
        for key, candidates in names.items():
            found = [values[name] for name in candidates if name in values and values[name] not in (None, "")]
            if len({str(value) for value in found}) == 1 and found:
                result[key] = found[0]
        return result

    rows = [(doc, record) for doc in source_documents
            for record in doc.metadata.get("records", []) if isinstance(record, dict)]
    loan_ids: set[str] = set()
    for _, record in rows:
        values = record.get("values")
        if not isinstance(values, dict):
            continue
        sheet = record.get("sheet")
        row_loan = mapped("Loan", sheet, values)
        direct_account = row_loan.get("borrower_account_id")
        canonical_table = aliases.get(sheet, sheet)
        is_canonical_loan = canonical_table == "Loan"
        direct_borrower_id = scalar_id(values.get("borrower_account_id"))
        if is_canonical_loan:
            direct_borrower_id = scalar_id(direct_account)
        role_headers = list(dict.fromkeys([
            *manifest.get("flattened_role_fields", []), *borrower_role_headers(values.keys())
        ]))
        flat_role = next((values.get(key) for key in role_headers
                          if values.get(key) not in (None, "")), None)
        explicit_borrower_role = (flat_role is not None
                                  and str(flat_role).strip().casefold() == "borrower")
        if (scalar_id(row_loan.get("id")) and direct_borrower_id == account_id
                and (is_canonical_loan or explicit_borrower_role)):
            loan_ids.add(scalar_id(row_loan["id"]))
        involvement = mapped("EntityInvolvement", sheet, values)
        role = str(involvement.get("role", "")).strip().casefold()
        if role == "borrower" and scalar_id(involvement.get("account_id")) == account_id:
            loan_id = scalar_id(involvement.get("loan_id"))
            if loan_id:
                loan_ids.add(loan_id)
        flat_account = scalar_id(row_loan.get("borrower_account_id"))
        flat_loan = scalar_id(row_loan.get("id"))
        if (explicit_borrower_role
                and flat_account == account_id and flat_loan):
            loan_ids.add(flat_loan)

    related_accounts = {account_id}
    response_ids: set[str] = set()
    for _, record in rows:
        values = record.get("values")
        if not isinstance(values, dict):
            continue
        sheet = record.get("sheet")
        loan = mapped("Loan", sheet, values)
        if scalar_id(loan.get("id")) in loan_ids:
            response_id = scalar_id(loan.get("response_id"))
            if response_id:
                response_ids.add(response_id)
        involvement = mapped("EntityInvolvement", sheet, values)
        if scalar_id(involvement.get("loan_id")) in loan_ids:
            linked_account = scalar_id(involvement.get("account_id"))
            if linked_account:
                related_accounts.add(linked_account)
        response = mapped("LCRESPONSE", sheet, values)
        if scalar_id(response.get("loan_id")) in loan_ids:
            response_id = scalar_id(response.get("id"))
            if response_id:
                response_ids.add(response_id)

    entity_ids: set[str] = set()
    for _, record in rows:
        values = record.get("values")
        if not isinstance(values, dict):
            continue
        entity = mapped("LCREntity", record.get("sheet"), values)
        if (scalar_id(entity.get("response_id")) in response_ids
                and scalar_id(entity.get("account_id")) in related_accounts):
            entity_id = scalar_id(entity.get("id"))
            if entity_id:
                entity_ids.add(entity_id)

    analysis_ids: set[str] = set()
    for _, record in rows:
        values = record.get("values")
        if not isinstance(values, dict):
            continue
        analysis = mapped("DataSourceAnalysis", record.get("sheet"), values)
        if scalar_id(analysis.get("entity_id")) in entity_ids:
            analysis_id = scalar_id(analysis.get("id"))
            if analysis_id:
                analysis_ids.add(analysis_id)

    def keep(record: dict[str, Any]) -> bool:
        values = record.get("values")
        if not isinstance(values, dict):
            return False
        sheet = record.get("sheet")
        targets = (
            ("Account", lambda data: scalar_id(data.get("id")) in related_accounts),
            ("Loan", lambda data: scalar_id(data.get("id")) in loan_ids),
            ("EntityInvolvement", lambda data: scalar_id(data.get("loan_id")) in loan_ids),
            ("CreditReport", lambda data: scalar_id(data.get("account_id")) in related_accounts),
            ("LoanExtension", lambda data: scalar_id(data.get("loan_id")) in loan_ids),
            ("LCRESPONSE", lambda data: scalar_id(data.get("id")) in response_ids or scalar_id(data.get("loan_id")) in loan_ids),
            ("LCREntity", lambda data: scalar_id(data.get("id")) in entity_ids),
            ("DataSourceAnalysis", lambda data: scalar_id(data.get("id")) in analysis_ids),
            ("Indicator", lambda data: scalar_id(data.get("analysis_id")) in analysis_ids),
            ("Characteristic", lambda data: scalar_id(data.get("analysis_id")) in analysis_ids),
            ("Regulation", lambda data: scalar_id(data.get("loan_id")) in loan_ids),
        )
        for table, predicate in targets:
            if predicate(mapped(table, sheet, values)):
                return True
        # Flattened rows can encode several related entities in one source row.
        loan_data = mapped("Loan", sheet, values)
        involvement_data = mapped("EntityInvolvement", sheet, values)
        if scalar_id(loan_data.get("id")) in loan_ids:
            return True
        return scalar_id(involvement_data.get("loan_id")) in loan_ids

    scoped = []
    for document in source_documents:
        detached = copy(document)
        detached.metadata = dict(document.metadata)
        records = document.metadata.get("records", [])
        if isinstance(records, list):
            detached.metadata["records"] = [record for record in records if isinstance(record, dict) and keep(record)]
        scoped.append(detached)
    return scoped


def _validate_relationships(graph: EntityGraph, manifest: dict[str, Any]) -> None:
    links = {
        "Loan": {"borrower_account_id": "Account", "response_id": "LCRESPONSE"},
        "EntityInvolvement": {"loan_id": "Loan", "account_id": "Account"},
        # Regulation rows are historical communicated decisions, never model predictions.
        "Regulation": {"loan_id": "Loan"},
        "CreditReport": {"account_id": "Account"},
        "LoanExtension": {"loan_id": "Loan"},
        "LCRESPONSE": {"loan_id": "Loan"},
        "LCREntity": {"account_id": "Account", "response_id": "LCRESPONSE",
                   "entity_involvement_id": "EntityInvolvement"},
        "DataSourceAnalysis": {"entity_id": "LCREntity"},
        "Indicator": {"analysis_id": "DataSourceAnalysis"},
        "Characteristic": {"analysis_id": "DataSourceAnalysis"},
    }
    for table, relations in links.items():
        for node in graph.table(table).values():
            for key, target in relations.items():
                value = node.fields.get(key)
                if table in {"Indicator", "Characteristic"} and key == "analysis_id" and value is None:
                    # Keep the source observation/evidence, but never invent an
                    # analysis ID or treat an unscoped value as a rule input.
                    warning = f"UNMAPPED_RELATION:{table}:analysis_id"
                    if warning not in graph.mapping_warnings:
                        graph.mapping_warnings.append(warning)
                    continue
                optional = (table == "Loan" or (table == "LCRESPONSE" and key == "loan_id")
                            or (table == "LCREntity" and key == "entity_involvement_id"))
                if value is None and optional:
                    continue
                if value is None or value not in graph.table(target):
                    graph.issue(f"ORPHAN_RELATION:{table}:{key}", (e.id for e in node.evidence))
    for loan in graph.table("Loan").values():
        account = loan.fields.get("borrower_account_id")
        if account in graph.table("Account"):
            graph.associations.append((loan.id, account, "borrower", loan.field_refs.get("borrower_account_id", [])))
        extensions = graph.related("LoanExtension", "loan_id", loan.id)
        if len(extensions) > 1:
            graph.issue("MULTIPLE_LOAN_EXTENSIONS", (e.id for node in extensions for e in node.evidence))
    for node in graph.table("EntityInvolvement").values():
        raw_role = node.fields.get("role")
        role = manifest["roles"].get(str(raw_role).strip().casefold()) if raw_role is not None else None
        if raw_role is not None and role is None:
            graph.issue("INVALID_JUNCTION_ROLE", (e.id for e in node.evidence))
        elif role is not None and node.fields.get("loan_id") in graph.table("Loan") and node.fields.get("account_id") in graph.table("Account"):
            graph.associations.append((node.fields["loan_id"], node.fields["account_id"], role, [e.id for e in node.evidence]))
    for loan in graph.table("Loan").values():
        if len(graph.borrowers(loan.id)) > 1:
            graph.issue("MULTIPLE_BORROWERS", (e.id for e in loan.evidence))
        explicit = loan.fields.get("response_id")
        response = graph.table("LCRESPONSE").get(explicit)
        if response and response.fields.get("loan_id") not in (None, loan.id):
            graph.issue("RESPONSE_LOAN_MISMATCH", (e.id for e in response.evidence))
    for entity in graph.table("LCREntity").values():
        involvement_id = entity.fields.get("entity_involvement_id")
        if not involvement_id:
            continue
        involvement = graph.table("EntityInvolvement").get(involvement_id)
        response = graph.table("LCRESPONSE").get(entity.fields.get("response_id"))
        linked_loan = response.fields.get("loan_id") if response else None
        mismatch = (
            involvement is None
            or involvement.fields.get("account_id") != entity.fields.get("account_id")
            or (linked_loan is not None and involvement.fields.get("loan_id") != linked_loan)
        )
        if mismatch:
            graph.issue("MISMATCHED_LCR_ENTITY_INVOLVEMENT", (e.id for e in entity.evidence))
    for table in ("Indicator", "Characteristic"):
        seen: dict[tuple[str, str], EntityNode] = {}
        for node in graph.table(table).values():
            # Repeated flattened SOQL keys without an Analysis FK are not
            # comparable observations: their scope is unknown, so preserve
            # them rather than claiming they conflict or combining them.
            if not node.fields.get("analysis_id") or not node.fields.get("key"):
                continue
            key = (node.fields.get("analysis_id"), node.fields.get("key"))
            previous = seen.get(key)
            if previous and str(previous.fields.get("value")) != str(node.fields.get("value")):
                graph.issue(f"CONFLICTING_{table.upper()}_KEY", (e.id for item in (previous, node) for e in item.evidence))
                previous.poisoned.add("value")
                node.poisoned.add("value")
            seen[key] = node