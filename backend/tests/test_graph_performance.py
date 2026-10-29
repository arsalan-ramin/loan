"""Synthetic-only graph regressions: no providers, uploads, or value caching."""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from functools import lru_cache
from time import perf_counter
from unittest.mock import Mock

import pytest

import backend.entity_graph as graph_module
from backend.models import Document
from backend.tests.test_large_preflight import synthetic_documents
from backend.underwriting_engine import load_underwriting_config
from backend.underwriting_models import SourceEvidence


def canonical_document(rows):
    return Document(name="canonical.xlsx", extension=".xlsx", size=0, metadata={
        "records": [{"sheet": table, "row": index + 2, "values": values}
                    for index, (table, values) in enumerate(rows)],
    })


def test_mapping_compilation_scales_with_schemas_not_rows(monkeypatch):
    compile_plan = Mock(wraps=graph_module._compile_graph_mapping)
    root = Mock(wraps=graph_module.soql_root_table)
    monkeypatch.setattr(graph_module, "_compile_graph_mapping", compile_plan)
    monkeypatch.setattr(graph_module, "soql_root_table", root)
    documents = synthetic_documents(200)
    config = load_underwriting_config()
    original_config, original_documents = deepcopy(config), deepcopy(documents)
    graph_module.build_entity_graph(documents, config)
    first_count = compile_plan.call_count
    assert 0 < first_count < 30
    assert root.call_count == 6  # Three coverage checks and three row schemas.
    # A new build must not reuse plans from a different config or document.
    graph_module.build_entity_graph(documents, config)
    assert compile_plan.call_count == first_count * 2
    assert config == original_config
    assert documents == original_documents


def test_sparse_schema_cache_is_bounded_and_preserves_order(monkeypatch):
    caches = []

    def tracked_cache(*args, **kwargs):
        def decorate(function):
            cached = lru_cache(*args, **kwargs)(function)
            caches.append(cached)
            return cached
        return decorate

    monkeypatch.setattr(graph_module, "lru_cache", tracked_cache)
    rows = [("Account", {"Id": f"A{i}", f"extra_{i}": None}) for i in range(300)]
    document = canonical_document(rows)
    cached = graph_module.build_entity_graph([document], load_underwriting_config())
    assert caches[0].cache_info().maxsize == 128
    assert caches[0].cache_info().currsize == 128
    assert caches[0].cache_info().misses == 300
    monkeypatch.setattr(graph_module, "lru_cache", lambda **kwargs: lambda function: function)
    uncached = graph_module.build_entity_graph([document], load_underwriting_config())
    assert asdict(cached) == asdict(uncached)


@pytest.mark.parametrize("metadata_headers", [False, True])
@pytest.mark.parametrize("selected_account_id", [None, "A00000"])
def test_cached_and_uncached_graphs_match_all_provenance(monkeypatch, metadata_headers, selected_account_id):
    documents = synthetic_documents(3)
    for document in documents:
        records = document.metadata["records"]
        base = records[0]["values"]
        records.extend([
            {"sheet": "Sheet1", "row": 10, "values": dict(reversed(list(base.items())))},
            {"sheet": "Sheet1", "row": 11, "values": {**base, "Loan_ID": "OTHER"}},
            {"sheet": "Sheet1", "row": 12, "values": {"Id": "SPARSE", "fico": None}},
            {"sheet": "Sheet1", "row": 13, "values": {**base, "Loan_ID": ""}},
        ])
        if not metadata_headers:
            document.metadata.pop("sheets")
    documents.append(canonical_document([
        ("Account", {"Id": "A00000", "name": "first"}),
        ("Account", {"Id": "A00000", "name": "conflicting"}),
        ("Loan", {"Id": "L00000", "borrower_account_id": "A00000", "requested_amount": Decimal("1.00")}),
        ("Indicator", {"Id": "I1", "analysis_id": "MISSING", "key": "k", "value": "0"}),
        ("Indicator", {"Id": "I2", "analysis_id": "MISSING", "key": "k", "value": "1"}),
    ]))
    config = load_underwriting_config()
    cached = graph_module.build_entity_graph(documents, config, selected_account_id=selected_account_id)
    monkeypatch.setattr(graph_module, "lru_cache", lambda **kwargs: lambda function: function)
    uncached = graph_module.build_entity_graph(documents, config, selected_account_id=selected_account_id)
    # Includes exact evidence ordering, IDs/hashes, refs, poisoned fields,
    # associations, issues, warnings, and mapped/unmapped source attributes.
    assert asdict(cached) == asdict(uncached)


def test_evidence_dedup_preserves_first_occurrence_and_duplicate_refs():
    graph = graph_module.EntityGraph()
    first = SourceEvidence(id="ev-1", workbook="synthetic", sheet="Account", row=2, field="name", value="first")
    duplicate = first.model_copy(update={"value": "second"})
    second = first.model_copy(update={"id": "ev-2"})
    graph.add("Account", "A1", {"name": "first"}, [first, duplicate, second], {"name": ["ev-1", "ev-1"]})
    node = graph.add("Account", "A1", {"name": "conflict"}, [duplicate, second], {"name": ["ev-2"]})
    assert node.evidence == [first, second]
    assert node.field_refs["name"] == ["ev-1", "ev-1", "ev-2"]
    assert node.fields["name"] is None and node.poisoned == {"name"}
    assert graph.issues == ["CONFLICTING_DUPLICATE:Account:name [ev-1,ev-2]"]


def test_duplicate_evidence_batch_has_linear_id_checks():
    class CountedEvidence:
        reads = 0

        def __init__(self, value):
            self.value = value

        @property
        def id(self):
            type(self).reads += 1
            return self.value

    graph = graph_module.EntityGraph()
    evidence = [CountedEvidence(str(index)) for index in range(1000)]
    graph.add("Account", "A1", {}, evidence, {})
    graph.add("Account", "A1", {}, evidence, {})
    assert CountedEvidence.reads <= 4000
    assert len(graph.table("Account")["A1"].evidence) == 1000


def test_relationship_indexes_retain_all_conflicts_and_order():
    document = canonical_document([
        ("Account", {"Id": "A1"}), ("Account", {"Id": "A2"}),
        ("Loan", {"Id": "L1", "borrower_account_id": "A1"}),
        ("Loan", {"Id": "L2", "borrower_account_id": "A2"}),
        ("EntityInvolvement", {"Id": "J1", "loan_id": "L1", "account_id": "A2", "role": "Borrower"}),
        ("LoanExtension", {"Id": "E1", "loan_id": "L1"}),
        ("LoanExtension", {"Id": "E2", "loan_id": "L1"}),
        ("LoanExtension", {"Id": "E3", "loan_id": "MISSING"}),
        ("Indicator", {"Id": "I1", "analysis_id": "MISSING", "key": "k", "value": "0"}),
        ("Indicator", {"Id": "I2", "analysis_id": "MISSING", "key": "k", "value": "1"}),
    ])
    graph = graph_module.build_entity_graph([document], load_underwriting_config())
    codes = [issue.split(" [")[0] for issue in graph.issues]
    assert codes == ["ORPHAN_RELATION:LoanExtension:loan_id",
                     "ORPHAN_RELATION:Indicator:analysis_id",
                     "ORPHAN_RELATION:Indicator:analysis_id", "MULTIPLE_LOAN_EXTENSIONS",
                     "MULTIPLE_BORROWERS", "CONFLICTING_INDICATOR_KEY"]
    assert graph.borrowers("L1") == {"A1", "A2"}
    assert graph.borrowers("L2") == {"A2"}
    assert graph.table("Indicator")["I1"].poisoned == {"value"}
    assert graph.table("Indicator")["I2"].poisoned == {"value"}
    for node in graph.table("Indicator").values():
        assert node.evidence and node.field_refs["value"]


def test_full_three_workbook_graph_keeps_final_row_conflict_and_evidence():
    documents = synthetic_documents()
    started = perf_counter()
    graph = graph_module.build_entity_graph(documents, load_underwriting_config())
    print(f"\nFull synthetic 3 x 16001 graph: {perf_counter() - started:.3f}s")
    for table in ("Loan", "Account", "EntityInvolvement", "Indicator", "Characteristic", "CreditReport"):
        assert len(graph.table(table)) == 16_001
    report = graph.table("CreditReport")["R16000"]
    conflict = next(issue for issue in graph.issues if issue.startswith("CONFLICTING_ALIASES:CreditReport:fico"))
    fico_evidence = [item for item in report.evidence if item.field in {"fico", "nFUSE__Credit_Score__c"}]
    assert len(fico_evidence) == 2
    assert {item.value for item in fico_evidence} == {"620", "740"}
    assert all(item.id in conflict for item in fico_evidence)
    assert len(graph.issues) == 16_002  # Every unlinked involvement is still validated.
    assert all(node.evidence for nodes in graph.nodes.values() for node in nodes.values())
    assert "UNMAPPED_RELATION:Indicator:analysis_id" in graph.mapping_warnings