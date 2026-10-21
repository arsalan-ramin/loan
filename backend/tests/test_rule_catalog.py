"""Policy catalog, generic-check isolation and saved-session migration."""

from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from backend.agents import IntakeAgent, RulesAgent
from backend.main import create_app
from backend.models import Document, Rule, Session
from backend import rule_catalog
from backend.rule_catalog import guideline_rules
from backend.storage import Store
from backend.tests.test_underwriting import document, rows
from backend.underwriting_engine import evaluate_underwriting, load_underwriting_config
from backend.underwriting_models import SOURCE_DOCUMENT


def mtb_session(**kwargs):
    return Session(documents=[Document(name="MTB_Query_1.xlsx", extension=".xlsx", size=10,
                                       content="DRAFT", metadata={"data_row_count": 0})], **kwargs)


def test_catalog_is_page_cited_unique_and_readiness_not_outcomes():
    catalog = guideline_rules()
    assert len(catalog) == len({rule["id"] for rule in catalog})
    assert {rule["page"] for rule in catalog} == {1, 2, 3, 4, 5, 7, 8, 9, 12, 13}
    assert all(set(rule) == {"id", "name", "category", "page", "description", "state", "reason"}
               and all(rule.values()) for rule in catalog)
    assert {rule["state"] for rule in catalog} == {"enforced", "conditional", "blocked"}
    lookup = {rule["id"]: rule for rule in catalog}
    for rule_id in ("NEW_MONEY_100M_CANDIDATE", "TIER_I_250M_CANDIDATE",
                    "BANKRUPTCY_5_CALENDAR_YEARS_DIAGNOSTIC", "CREDIT_FREEZE_CLEAR",
                    "MINIMUM_AGE", "HIGH_RISK_NAICS", "PERSONAL_FICO_650_CANDIDATE",
                    "SBSS_160_CANDIDATE", "SBSS_190_199_SBA_CANDIDATE"):
        assert lookup[rule_id]["state"] == "conditional"
        assert lookup[rule_id]["page"] == 2
    for rule_id in ("IDENTITY_EXPLICIT_RELATIONSHIP", "NO_EXPLICIT_LOAN_RECORDS",
                    "INVALID_RECORD_SHAPE", "INVALID_USD_AMOUNT", "POLICY_SOURCE_V4",
                    "HUMAN_REVIEW_REQUIRED", "PRODUCT_MAPPING"):
        assert lookup[rule_id]["state"] == "enforced"
    assert not any(rule["id"].startswith("MTB-") for rule in catalog)
    assert not any(rule["id"] in {"readable-content", "draft-check"} for rule in catalog)


def test_catalog_covers_all_executable_default_rule_ids_and_citations():
    lookup = {item["id"]: item for item in guideline_rules()}
    emitted = set()
    for product in ("express_baloc", "conventional_baloc", "sba_baloc", "bil", "sba_bil", "term_loan"):
        for score in ("159", "160", "190", "200"):
            source = rows()
            source[1][1].update(product=product, is_startup=True, is_increase=True,
                                account_opened="2025-10-20")
            source[3][1]["sbss"] = score
            for index in (1, 2):
                source.extend([
                    ("Account", {"Id": f"G{index}"}),
                    ("EntityInvolvement", {"Id": f"I{index}", "loan_id": "L1",
                                           "account_id": f"G{index}", "role": "guarantor"}),
                    ("CreditReport", {"Id": f"GR{index}", "account_id": f"G{index}",
                                      "status": "completed", "report_date": "2026-10-19",
                                      "personal_fico": "760"}),
                ])
            result = evaluate_underwriting([document(source)]).loan_results[0]
            for check in result.rule_results:
                rule_id = re.sub(r"GUARANTOR_\d+_", "GUARANTOR_{n}_", check.rule_id)
                emitted.add(rule_id)
                assert rule_id in lookup, check.rule_id
                if check.page is not None:
                    assert check.page == lookup[rule_id]["page"] == 2
                    assert check.policy_reference == f"{SOURCE_DOCUMENT} p.2"
                if lookup[rule_id]["state"] == "blocked":
                    assert check.status in {"UNKNOWN", "NOT_APPLICABLE"}
            assert result.final_action == "MANUAL_REVIEW"
            assert result.human_review_required is True
    # These are explicitly labeled graph/model safeguards or a readiness-only
    # conflict, not invented per-loan result IDs. No legacy checks sneak back in.
    assert {key for key in lookup if not key.startswith(("EXPANDED-", "REASON-"))} - emitted == {
        "NO_EXPLICIT_LOAN_RECORDS", "INVALID_RECORD_SHAPE", "INVALID_USD_AMOUNT",
        "HUMAN_REVIEW_REQUIRED", "REQUEST_THRESHOLD_POLICY",
        "GUIDE_SOURCE_INTEGRITY", "GUIDE_STAGES_0_6", "GUIDE_SECTION_8_STRATEGY",
        "GUIDE_VEHICLE_OVERRIDE", "GUIDE_OUT_OF_SCOPE",
    }


def test_unknown_policy_gates_and_conflicts_are_blocked_not_inferred():
    lookup = {item["id"]: item for item in guideline_rules()}
    gates = {
        "QUICKAPP_POLICY", "PRODUCT_BALOC_POLICY", "SBSS_FICO_BRANCH_POLICY",
        "OWNERSHIP_EBITDA_POLICY", "BANKRUPTCY_POLICY", "GROUP_EXPOSURE_POLICY",
        "INCREASE_WAIT_POLICY", "REQUEST_THRESHOLD_POLICY", "SBSS_160_189_BRANCH",
        "SBSS_200_OWNERSHIP_NAICS_CANDIDATE", "GUIDE_VEHICLE_OVERRIDE",
    }
    assert {key for key, item in lookup.items() if item["state"] == "blocked" and not key.startswith(("EXPANDED-", "REASON-"))} == gates
    assert "REQUEST_THRESHOLD_100M_VS_100K" in lookup["REQUEST_THRESHOLD_POLICY"]["description"]
    assert "not a separately emitted engine rule" in lookup["REQUEST_THRESHOLD_POLICY"]["description"]
    assert "INCREASE_WAIT_6_VS_12_MONTHS" in lookup["INCREASE_WAIT_POLICY"]["description"]
    assert "both explicit amounts" in lookup["GROUP_EXPOSURE_POLICY"]["description"]
    for months in (6, 12):
        assert lookup[f"INCREASE_{months}_MONTHS_DIAGNOSTIC"]["state"] == "conditional"


def test_catalog_uses_v4_config_not_legacy_policy_parameters(monkeypatch):
    original = Path.read_text
    reads = []

    def read_text(path, *args, **kwargs):
        reads.append(path.name)
        assert path.name != "policy_config.json"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    lookup = {item["id"]: item for item in guideline_rules()}
    assert "underwriting_config.json" in reads
    assert "minimum_age=18" in lookup["MINIMUM_AGE"]["description"]
    assert "23, 4451, 45, 8111, 8121, 8123" in lookup["HIGH_RISK_NAICS"]["description"]
    assert SOURCE_DOCUMENT in lookup["POLICY_SOURCE_V4"]["description"]
    assert "SHA-256" in lookup["POLICY_SOURCE_V4"]["description"]


@pytest.mark.parametrize("overrides,blocked", [
    ({"minimum_age": None}, {"MINIMUM_AGE"}),
    ({"high_risk_naics": []}, {"HIGH_RISK_NAICS"}),
])
def test_catalog_parameter_readiness_does_not_invent_missing_criteria(monkeypatch, overrides, blocked):
    config = load_underwriting_config(overrides)
    monkeypatch.setattr(rule_catalog, "load_underwriting_config", lambda: config)
    lookup = {item["id"]: item for item in guideline_rules()}
    assert all(lookup[key]["state"] == "blocked" for key in blocked)
    assert lookup["SBSS_FICO_BRANCH_POLICY"]["state"] == "blocked"
    assert lookup["GROUP_EXPOSURE_POLICY"]["state"] == "blocked"


def test_catalog_readiness_does_not_claim_source_verified_or_checks_passed(monkeypatch):
    catalog = guideline_rules()
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    batch = evaluate_underwriting([document(rows())])
    assert batch.source_available is False
    assert all(check.status in {"UNKNOWN", "NOT_APPLICABLE"}
               for check in batch.loan_results[0].rule_results if check.page)
    # Enforced means the integrity check cannot be disabled, not that it passed.
    assert guideline_rules() == catalog
    source = next(item for item in catalog if item["id"] == "POLICY_SOURCE_V4")
    assert source["state"] == "enforced"
    assert "not a claim of PASS" in source["reason"]


def test_catalog_calls_return_fresh_definitions():
    catalog = guideline_rules()
    catalog[0]["state"] = "PASS"
    catalog.pop()
    refreshed = guideline_rules()
    assert refreshed[0]["state"] == "enforced"
    assert len(refreshed) == len(catalog) + 1


def test_guide_catalog_is_distinct_from_emitted_v4_policy_results():
    from backend.underwriting_models import IMPLEMENTATION_GUIDE_DOCUMENT

    lookup = {item["id"]: item for item in guideline_rules()}
    assert IMPLEMENTATION_GUIDE_DOCUMENT in lookup["GUIDE_SOURCE_INTEGRITY"]["description"]
    assert lookup["GUIDE_SOURCE_INTEGRITY"]["state"] == "enforced"
    for key in ("GUIDE_SECTION_8_STRATEGY", "GUIDE_VEHICLE_OVERRIDE"):
        assert lookup[key]["page"] == 4
        assert lookup[key]["category"].startswith("Implementation guide")
    assert lookup["GUIDE_STAGES_0_6"]["page"] == 5
    assert "NOT_ISSUED" in lookup["GUIDE_STAGES_0_6"]["description"]
    assert "NOT_EVALUATED" in lookup["GUIDE_OUT_OF_SCOPE"]["description"]
    loan = evaluate_underwriting([document(rows())]).loan_results[0]
    assert not any(rule.rule_id.startswith("GUIDE_") for rule in loan.rule_results)
    assert loan.authorization == "NOT_ISSUED"


def test_mtb_can_process_without_optional_checks():
    session = mtb_session(rules=[])
    assert "underwriting" in IntakeAgent().check(session)
    assert len(session.policy_rules) > 0
    assert Session().policy_rules == []


def test_mtb_ignores_bundled_examples_but_retains_custom_checks():
    session = mtb_session()
    session.rules.append(Rule(id="custom", name="Custom required phrase", kind="required_text", value="missing"))
    results = RulesAgent().evaluate(session)
    ids = {result.rule_id for result in results}
    assert "draft-check" not in ids
    assert "readable-content" not in ids
    assert "custom" in ids
    assert "system-excel-records" in ids


def test_saved_mtb_session_migrates_exact_examples_only(tmp_path):
    root = tmp_path / "data"
    store = Store(root)
    session = mtb_session()
    modified = session.rules[0].model_copy(update={"value": "99"})
    session.rules.append(modified)
    store.save(session)
    app = create_app(root)
    with TestClient(app) as client:
        data = client.get(f"/api/sessions/{session.id}").json()
        assert len(data["rules"]) == 1
        assert data["rules"][0]["value"] == "99"
        assert data["policy_rules"] == guideline_rules()
        assert data["report"] is None


def test_import_removes_examples_and_catalog_refreshes_in_api(tmp_path):
    from backend.tests.test_excel import workbook_bytes
    root = tmp_path / "data"
    with TestClient(create_app(root)) as client:
        for n in (1, 2, 4):
            (root / f"MTB_Query_{n}.xlsx").write_bytes(workbook_bytes(False))
        sid = client.post("/api/sessions", json={}).json()["id"]
        imported = client.post(f"/api/sessions/{sid}/dataset").json()
        assert imported["rules"] == []
        assert imported["policy_rules"] == guideline_rules()
        # Saving an empty optional-check list does not disable mandatory policy checks.
        changed = client.put(f"/api/sessions/{sid}/rules", json={"rules": []}).json()
        assert changed["policy_rules"] == guideline_rules()