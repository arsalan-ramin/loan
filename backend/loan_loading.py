"""Stream uploaded workbooks, retaining only an explicit loan's related rows."""

import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .datasets import mtb_query_ids, soql_root_table, soql_tables_for_role
from .entity_graph import _compile_graph_mapping, scalar_id
from .excel import extract_excel
from .models import Document
from .underwriting_engine import load_underwriting_config


PARENTS = {
    "loan_id": "Loan", "borrower_account_id": "Account", "account_id": "Account",
    "response_id": "LCRESPONSE", "entity_id": "LCREntity",
    "analysis_id": "DataSourceAnalysis", "entity_involvement_id": "EntityInvolvement",
    "report_id": "CreditReport",
}
SCOPE_PARENTS = {
    "CreditReport": ("account_id",),
    "LCREntity": ("response_id", "entity_involvement_id"),
    "DataSourceAnalysis": ("entity_id",),
    "Indicator": ("analysis_id",),
    "Characteristic": ("analysis_id",),
}


def load_loan_documents(documents: list[Document], directory: Path, loan_id: str) -> None:
    if scalar_id(loan_id) != loan_id:
        raise ValueError("Enter a valid explicit loan number.")
    manifest = load_underwriting_config()["mapping_manifest"]
    for document in documents:
        path = directory / document.id
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        document.metadata = {
            "filename": document.name, "extension": document.extension,
            "size_bytes": path.stat().st_size, "sha256": digest.hexdigest(),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
    related = {table: set() for table in manifest["tables"]}
    related["Loan"] = {loan_id}

    def row_filter(document: Document):
        @lru_cache(maxsize=128)
        def plans(sheet: str, headers: tuple[str, ...]):
            canonical_table = manifest.get("canonical_table_aliases", {}).get(sheet, sheet)
            canonical = canonical_table in manifest["tables"]
            roles = mtb_query_ids(document.name, {"sheets": [{"name": sheet, "headers": list(headers)}]})
            role = next(iter(roles)) if len(roles) == 1 else None
            root = soql_root_table(role, headers) if not canonical else None
            if canonical:
                mappings = {canonical_table: manifest["tables"][canonical_table]}
            elif role is not None:
                mappings = {table: mapping for table, mapping in manifest["flattened"].items()
                            if not root or table in soql_tables_for_role(role)}
            else:
                mappings = {}
            return tuple((table, *_compile_graph_mapping(
                table, mapping, manifest, canonical, root, role, list(headers), headers,
            )[:2]) for table, mapping in mappings.items())

        def keep(record: dict, headers: list[str]) -> bool:
            values = record["values"]
            mapped = {}
            for table, ids, fields in plans(record["sheet"], tuple(headers)):
                names = {"id": ids, **{key: aliases for key, aliases in fields.items() if key in PARENTS}}
                # Retain conflicting aliases so the graph can diagnose them,
                # rather than silently dropping the selected loan's evidence.
                mapped[table] = {
                    key: {identifier for alias in aliases
                          if (identifier := scalar_id(values.get(alias))) is not None}
                    for key, aliases in names.items()
                }
            explicit_loans = set().union(*(data.get("loan_id", set()) for data in mapped.values()))
            explicit_loans.update(mapped.get("Loan", {}).get("id", set()))
            if explicit_loans and loan_id not in explicit_loans:
                return False
            connected = bool(explicit_loans & {loan_id})
            for table, data in mapped.items():
                connected |= bool(data["id"] & related.get(table, set()))
                connected |= any(data.get(key, set()) & related.get(PARENTS[key], set())
                                 for key in SCOPE_PARENTS.get(table, ()))
            if not connected:
                return False
            for table, data in mapped.items():
                related.setdefault(table, set()).update(data["id"] if table != "Loan" else {loan_id})
                for key, parent in PARENTS.items():
                    if parent != "Loan":
                        related.setdefault(parent, set()).update(data.get(key, set()))
            return True

        return keep

    filters = {document.id: row_filter(document) for document in documents}
    # Repeat bounded-memory scans until even parents stored after their children
    # have been resolved. Never expand the scope to another loan of this account.
    while True:
        before = {table: frozenset(ids) for table, ids in related.items()}
        for document in documents:
            if document.extension != ".xlsx":
                raise ValueError("Loan-scoped loading requires XLSX source workbooks.")
            content, metadata = extract_excel(directory / document.id,
                record_filter=filters[document.id])
            document.content = content
            document.metadata = {**document.metadata, **metadata, "scoped_loan_id": loan_id,
                                 "word_count": len(content.split()), "character_count": len(content)}
        if before == related:
            break
