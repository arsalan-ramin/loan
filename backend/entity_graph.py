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
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from .datasets import mtb_query_ids
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
        return {account for loan, account, role, _ in self.associations if loan == loan_id and role == "borrower"}


def build_entity_graph(documents: Iterable[Any], config: dict[str, Any]) -> EntityGraph:
    """Build and validate all one-to-many relationships using the explicit manifest.

    A canonical Loan.borrower_account_id is an explicit direct borrower link;
    flattened Account ID alone is not. Identity_Mapping_Validated is ignored.
    IDs are opaque explicit scalars (synthetic IDs are allowed in test fixtures).
    """
    graph = EntityGraph()
    manifest = config["mapping_manifest"]
    tables = manifest["tables"]
    canonical_aliases = manifest.get("canonical_table_aliases", {})
    for document_index, document in enumerate(documents):
        metadata = document.metadata
        recognized = bool(mtb_query_ids(document.name, metadata))
        records = metadata.get("records", [])
        if not isinstance(records, list):
            graph.issue("INVALID_RECORDS_CONTAINER")
            continue
        reference = {sheet.get("name") for sheet in metadata.get("sheets", []) if sheet.get("is_reference")}
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
            mappings = {canonical_table: tables[canonical_table]} if canonical else manifest["flattened"]
            row_nodes: dict[str, EntityNode] = {}
            row_evidence: list[str] = []
            for table, mapping in mappings.items():
                raw_fields = mapping["fields"]
                field_map = ({key: [key] for key in raw_fields} if isinstance(raw_fields, list)
                             else {key: list(aliases) for key, aliases in raw_fields.items()})
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
                if node_id is None and table in {"Indicator", "Characteristic"} and fields.get("analysis_id") and fields.get("key"):
                    # Observation key, explicitly not an invented source record ID.
                    storage_id = "observation:" + prefix + ":" + table
                else:
                    storage_id = scalar_id(node_id)
                # On flattened rows, a generic currency alone must not fabricate
                # missing report/response entities. Entity-specific data must exist.
                specific = set(fields) - {"currency", "amount_scale", "account_id", "loan_id", "response_id", "entity_id", "analysis_id"}
                if storage_id is None:
                    if canonical or node_id is not None or specific:
                        graph.issue(f"MISSING_OR_INVALID_ID:{table}", (e.id for e in evidence))
                    continue
                for item in evidence:
                    item.record_id = scalar_id(node_id)
                node = graph.add(table, storage_id, fields, evidence, refs)
                row_nodes[table] = node
                row_evidence.extend(e.id for e in evidence)
            if not canonical and "Loan" in row_nodes and "Account" in row_nodes:
                roles = [values[key] for key in manifest["flattened_role_fields"] if values.get(key) is not None]
                approved = {manifest["roles"].get(str(role).strip().casefold()) for role in roles}
                if len(approved) == 1 and None not in approved:
                    for key in manifest["flattened_role_fields"]:
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
        role = manifest["roles"].get(str(node.fields.get("role", "")).strip().casefold())
        if role is None:
            graph.issue("MISSING_OR_INVALID_JUNCTION_ROLE", (e.id for e in node.evidence))
        elif node.fields.get("loan_id") in graph.table("Loan") and node.fields.get("account_id") in graph.table("Account"):
            graph.associations.append((node.fields["loan_id"], node.fields["account_id"], role, [e.id for e in node.evidence]))
    for loan in graph.table("Loan").values():
        if len(graph.borrowers(loan.id)) > 1:
            graph.issue("MULTIPLE_BORROWERS", (e.id for e in loan.evidence))
        explicit = loan.fields.get("response_id")
        response = graph.table("LCRESPONSE").get(explicit)
        if response and response.fields.get("loan_id") not in (None, loan.id):
            graph.issue("RESPONSE_LOAN_MISMATCH", (e.id for e in response.evidence))
    for table in ("Indicator", "Characteristic"):
        seen: dict[tuple[str, str], EntityNode] = {}
        for node in graph.table(table).values():
            key = (node.fields.get("analysis_id"), node.fields.get("key"))
            previous = seen.get(key)
            if previous and str(previous.fields.get("value")) != str(node.fields.get("value")):
                graph.issue(f"CONFLICTING_{table.upper()}_KEY", (e.id for item in (previous, node) for e in item.evidence))
                previous.poisoned.add("value")
                node.poisoned.add("value")
            seen[key] = node