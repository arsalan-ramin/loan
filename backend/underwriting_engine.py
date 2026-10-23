"""Pure deterministic underwriting diagnostics; no LLM/network or operational acts.

Public API::

    evaluate_underwriting(documents, mode='PRODUCTION', *, policy_config=None)
        -> UnderwritingBatch

``documents`` is an iterable of existing Document objects (or objects with id,
name, metadata). Canonical sheet names/columns and flattened aliases are listed
in underwriting_config.json. Decimal money must reach this boundary as lexical
text, integer or Decimal; Excel float money is rejected, not repaired.

An optional config dictionary is recursively overlaid on a fresh default copy.
It does not change files. TEST simulation requires test_policy.synthetic=True,
approved=True, a nonempty label, explicit resolution of BOTH default conflicts,
and a nonempty criteria list. These are *synthetic fixture criteria*, not policy
authorization. Each criterion is {rule_id, feature, op, value}; op is one of
eq/ne/ge/gt/le/lt/in. Features are the normalized_data diagnostics (or amount
keys). All criteria pass -> SIMULATED_APPROVE_PATH; any fail -> SIMULATED_DECLINE_PATH;
otherwise review. Missing identity, graph defects, or policy evidence NEVER
become operational lending decisions. PRODUCTION ignores synthetic criteria.

Source availability is local-file AND pinned hash verified; a config assertion
alone cannot claim the v4 PDF exists. All v4 policy gates stay UNKNOWN
without that evidence. Deterministic feature classifications are NOT invented
SBSS/FICO branch conjunctions, approved limits, or automatic loan decisions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from calendar import monthrange
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from .entity_graph import EntityGraph, EntityNode, build_entity_graph, iso_date
from .expanded_spec import evaluate_expanded_spec
from .underwriting_models import (
    AMOUNT_NAMES, IMPLEMENTATION_GUIDE_DOCUMENT, IMPLEMENTATION_GUIDE_SHA256,
    SOURCE_DOCUMENT, SOURCE_SHA256, EXPANDED_SPEC_DOCUMENT, EXPANDED_SPEC_SHA256,
    USD_25M, USD_100K, USD_250M, USD_100M,
    LoanRecommendation, Mode, UnderwritingBatch, UnderwritingRuleResult,
    exact_decimal,
)

CONFIG_PATH = Path(__file__).with_name("underwriting_config.json")
LOGGER = logging.getLogger(__name__)
REQUIRED_CONFLICTS = {"REQUEST_THRESHOLD_100M_VS_100K", "INCREASE_WAIT_6_VS_12_MONTHS"}
AMOUNT_KEYS = tuple(AMOUNT_NAMES)


def personal_fico(report: EntityNode | None, role: str = "borrower") -> Any:
    """Never infer personal FICO from an unlabeled or business bureau score.

    Explicit canonical personal_fico is already typed by the source contract.
    Otherwise both personal category and an applicable party role are required.
    This is a single party observation, not an invented multi-owner average.
    """
    if report is None:
        return None
    if "personal_fico" in report.fields:
        return report.fields["personal_fico"]
    category = str(report.fields.get("report_category", "")).strip().casefold()
    report_role = str(report.fields.get("report_role", "")).strip().casefold()
    if category in {"personal", "consumer", "personal_fico"} and report_role == role:
        return report.fields.get("fico")
    return None


def load_underwriting_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return an isolated configuration, with no writes or shared mutable defaults."""
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def merge(target: dict, source: Mapping) -> None:
        for key, value in source.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise ValueError("policy_config must be a mapping")
        merge(config, overrides)
    if config.get("source_document") != SOURCE_DOCUMENT:
        raise ValueError("The v4 source document cannot be substituted")
    if config.get("implementation_guide_source_document") != IMPLEMENTATION_GUIDE_DOCUMENT:
        raise ValueError("The implementation-guide source document cannot be substituted")
    if config.get("expanded_spec_source_document") != EXPANDED_SPEC_DOCUMENT:
        raise ValueError("The expanded specification source cannot be substituted")
    minimum = config.get("minimum_age")
    if minimum is not None and (type(minimum) is not int or minimum < 0 or minimum > 120):
        raise ValueError("minimum_age must be null or an integer from 0 to 120")
    if not isinstance(config.get("high_risk_naics"), list) or any(not isinstance(code, str) for code in config["high_risk_naics"]):
        raise ValueError("high_risk_naics must be an explicit list of lexical codes")
    if not isinstance(config.get("unresolved_conflicts"), list):
        raise ValueError("unresolved_conflicts must be a list")
    if any(not isinstance(item, str) for item in config["unresolved_conflicts"]):
        raise ValueError("Unresolved conflict identifiers must be strings")
    policy = config.get("test_policy")
    if not isinstance(policy, dict):
        raise ValueError("test_policy must be a mapping")
    resolved = policy.get("resolved_conflicts")
    if not isinstance(resolved, list) or any(not isinstance(item, str) for item in resolved):
        raise ValueError("Synthetic resolved_conflicts must be a list of identifiers")
    return config


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {"true": True, "false": False, "yes": True, "no": False,
            "y": True, "n": False, "1": True, "0": False}.get(value.strip().casefold())
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _number(value: Any) -> Decimal | None:
    try:
        return exact_decimal(value)
    except ValueError:
        return None


def sbss_bucket(value: Any) -> str | None:
    """Diagnostic boundaries only: 159/160, 189/190, 199/200, not policy branches."""
    score = _number(value)
    if score is None or score != score.to_integral_value() or not 0 <= score <= 300:
        return None
    return "LT_160" if score < 160 else "160_189" if score < 190 else "190_199" if score < 200 else "GE_200"


def fico_band(value: Any) -> str | None:
    """Diagnostic bands only, with conventional 300..850 score validity."""
    score = _number(value)
    if score is None or score != score.to_integral_value() or not 300 <= score <= 850:
        return None
    return "LT_650" if score < 650 else "650_759" if score < 760 else "GE_760"


def calendar_anniversary(original: date, years: int) -> date:
    """Calendar-year anniversary; Feb 29 becomes Feb 28 in a non-leap year."""
    year = original.year + years
    return date(year, original.month, min(original.day, monthrange(year, original.month)[1]))


def calendar_month_anniversary(original: date, months: int) -> date:
    """Exact calendar-month arithmetic, clamped to the target month's last day."""
    month_index = original.month - 1 + months
    year, month = original.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(original.day, monthrange(year, month)[1]))


def evaluate_implementation_strategy(
    *,
    identity_valid: bool | None,
    quick_app_eligible: bool | None,
    knockouts_completed: bool,
    knockout_detected: bool | None,
    sbss: Any,
    fico: Any,
    ownership_years: Any,
    requested_amount: Any,
    naics: Any,
    vehicle_only: bool | None,
    product_review_required: bool,
    compliance_review_required: bool,
    manual_review_required: bool,
    source_available: bool,
    policy_conflicts: Iterable[str],
    high_risk_naics: Iterable[str],
) -> dict[str, Any]:
    """Apply the guide's Section 8 tree as a pure, non-authorizing diagnostic.

    Every identity, eligibility, knockout, source and exception flag is supplied
    explicitly by the caller. Missing/invalid tree inputs fail closed. The
    The screenshot's 160-189 branch uses its explicit >$100K knockout-list
    condition; the conflicting <=$100M tree label remains recorded as an
    unresolved production-policy conflict. No operational outcome is issued.
    """
    conflicts = set(policy_conflicts) | REQUIRED_CONFLICTS
    stages: dict[str, str] = {
        "source": "PASS" if source_available else "UNKNOWN",
        "identity": "PASS" if identity_valid is True else "FAIL" if identity_valid is False else "UNKNOWN",
        "quick_app_eligibility": "PASS" if quick_app_eligible is True else "FAIL" if quick_app_eligible is False else "UNKNOWN",
        "knockouts": "PASS" if knockouts_completed and knockout_detected is not None else "UNKNOWN",
        "stage_0_schema_identity": "PASS" if identity_valid is True else "FAIL" if identity_valid is False else "UNKNOWN",
        "stage_1_policy_approval_and_mode": "DRAFT_UNAPPROVED_SIMULATION_ONLY",
        "stage_2_product_eligibility_and_completeness": "PASS" if quick_app_eligible is True else "FAIL" if quick_app_eligible is False else "UNKNOWN",
        "stage_3_hard_knockouts": "PASS" if knockouts_completed and knockout_detected is False else "CANDIDATE_MATCH" if knockouts_completed and knockout_detected is True else "UNKNOWN",
        "stage_4_credit_strategy": "PENDING",
        "stage_5_non_operational_route": "PENDING",
        "stage_6_explanation_and_audit": "REQUIRED",
    }
    result: dict[str, Any] = {
        "eligibility_route": "POLICY_REVIEW", "credit_assessment": "POLICY_REVIEW",
        "authorization": "NOT_ISSUED", "strategy_branch": None, "stages": stages,
        "out_of_scope": {item: "NOT_EVALUATED" for item in (
            "negative_news", "lawsuits", "garnishment", "sba_eligibility", "ltv",
            "collections_thresholds", "judgements_thresholds", "collateral_terms",
        )},
        "unresolved_conflicts": sorted(conflicts),
    }

    def stop(eligibility: str, assessment: str, stage: str, outcome: str) -> dict[str, Any]:
        result["eligibility_route"] = eligibility
        result["credit_assessment"] = assessment
        stages[stage] = outcome
        stages["stage_5_non_operational_route"] = assessment
        return result

    if not source_available:
        stages["stage_1_policy_approval_and_mode"] = "UNKNOWN_SOURCE_POLICY_REVIEW"
        return stop("POLICY_REVIEW", "POLICY_REVIEW", "policy", "UNKNOWN")
    if identity_valid is not True:
        return stop("DATA_REVIEW", "DATA_REVIEW", "identity", stages["identity"])
    if quick_app_eligible is False:
        return stop("FULL_UNDERWRITING", "MANUAL_REVIEW", "quick_app_eligibility", "FAIL")
    if quick_app_eligible is not True:
        return stop("DATA_REVIEW", "DATA_REVIEW", "quick_app_eligibility", "UNKNOWN")
    result["eligibility_route"] = "QUICK_APP"
    if product_review_required:
        return stop("PRODUCT_REVIEW", "MANUAL_REVIEW", "product", "REVIEW_REQUIRED")
    if compliance_review_required:
        return stop("QUICK_APP", "COMPLIANCE_REVIEW", "compliance", "REVIEW_REQUIRED")
    if manual_review_required:
        return stop("QUICK_APP", "MANUAL_REVIEW", "stop_conditions", "REVIEW_REQUIRED")
    if not knockouts_completed or knockout_detected is None:
        return stop("DATA_REVIEW", "DATA_REVIEW", "knockouts", "UNKNOWN")
    if knockout_detected:
        return stop("QUICK_APP", "SIMULATED_DECLINE_CANDIDATE", "knockouts", "CANDIDATE_MATCH")

    sbss_value = _number(sbss)
    fico_value = _number(fico)
    ownership_value = _number(ownership_years)
    amount_value = _number(requested_amount)
    naics_value = naics if isinstance(naics, str) and re.fullmatch(r"\d{2,6}", naics) else None
    scores_valid = (sbss_value is not None and sbss_value == sbss_value.to_integral_value()
                    and 0 <= sbss_value <= 300)
    fico_valid = (fico_value is not None and fico_value == fico_value.to_integral_value()
                  and 300 <= fico_value <= 850)
    ownership_valid = ownership_value is not None and ownership_value >= 0
    amount_valid = amount_value is not None and amount_value >= 0
    if not scores_valid or not fico_valid or not ownership_valid or not amount_valid or naics_value is None:
        return stop("DATA_REVIEW", "DATA_REVIEW", "strategy", "REQUIRED_INPUT_UNKNOWN")
    # The checks above establish these values before any ordered branch tests.
    assert sbss_value is not None and ownership_value is not None and amount_value is not None

    high_risk = tuple(code for code in high_risk_naics if isinstance(code, str))
    is_high_risk = bool(high_risk) and any(naics_value.startswith(code) for code in high_risk)
    branch: str
    assessment: str
    if sbss_value < 160:
        branch, assessment = "SBSS_LT_160", "SIMULATED_DECLINE_CANDIDATE"
    elif sbss_value < 190:
        if ownership_value >= 2:
            branch, assessment = "SBSS_160_189_OWNERSHIP_GE_2", "SIMULATED_APPROVAL_CANDIDATE"
        elif not fico_valid:
            return stop("DATA_REVIEW", "DATA_REVIEW", "strategy", "REQUIRED_FICO_UNKNOWN")
        else:
            assert fico_value is not None
            if fico_value >= 760:
                branch, assessment = "SBSS_160_189_OWNERSHIP_LT_2_FICO_GE_760", "SIMULATED_APPROVAL_CANDIDATE"
            elif fico_value >= 650:
                if amount_value <= USD_100K:
                    branch, assessment = "SBSS_160_189_OWNERSHIP_LT_2_FICO_650_759_AMOUNT_LE_100K", "SIMULATED_APPROVAL_CANDIDATE"
                else:
                    branch, assessment = "SBSS_160_189_OWNERSHIP_LT_2_FICO_650_759_AMOUNT_GT_100K", "SIMULATED_DECLINE_CANDIDATE"
            else:
                branch, assessment = "SBSS_160_189_OWNERSHIP_LT_2_FICO_LT_650", "SIMULATED_DECLINE_CANDIDATE"
    elif sbss_value < 200:
        branch, assessment = "SBSS_190_199", "SIMULATED_APPROVAL_CANDIDATE"
    elif ownership_value < 2:
        branch, assessment = "SBSS_GE_200_OWNERSHIP_LT_2", "SIMULATED_APPROVAL_CANDIDATE"
    elif is_high_risk:
        # The guide mentions a vehicle-only override but does not define enough
        # criteria to implement it safely; do not infer or apply that override.
        if vehicle_only is not False:
            return stop("POLICY_REVIEW", "POLICY_REVIEW", "strategy", "VEHICLE_OVERRIDE_UNKNOWN")
        branch, assessment = "SBSS_GE_200_OWNERSHIP_GE_2_HIGH_RISK_NAICS", "SIMULATED_APPROVAL_CANDIDATE"
    else:
        branch, assessment = "SBSS_GE_200_OWNERSHIP_GE_2_OTHER_NAICS", "SIMULATED_APPROVAL_CANDIDATE"

    result["strategy_branch"] = branch
    result["credit_assessment"] = assessment
    stages["strategy"] = "CANDIDATE_MATCH"
    stages["stage_4_credit_strategy"] = "CANDIDATE_MATCH"
    stages["stage_5_non_operational_route"] = assessment
    # Assessment candidates never unlock production paths or authorization.
    return result


def _select_report(graph: EntityGraph, account_id: str, as_of: date | None,
                   config: dict) -> tuple[EntityNode | None, str | None]:
    if as_of is None:
        return None, "report_selection_requires_explicit_as_of_date"
    reports = graph.related("CreditReport", "account_id", account_id)
    known_statuses = (config["mapping_manifest"]["completed_report_statuses"]
                      + config["mapping_manifest"]["incomplete_report_statuses"])
    if any(str(report.fields.get("status", "")).strip().casefold() not in known_statuses for report in reports):
        return None, "report_completion_status_missing_or_unknown"
    completed = [report for report in reports if str(report.fields.get("status", "")).strip().casefold()
                 in config["mapping_manifest"]["completed_report_statuses"]]
    if not completed:
        return None, "completed_credit_report"
    if any(iso_date(report.fields.get("report_date")) is None for report in completed):
        return None, "completed_report_date_missing_or_ambiguous"
    dated = [(report, iso_date(report.fields.get("report_date"))) for report in completed]
    applicable = [(report, report_date) for report, report_date in dated if report_date is not None and report_date <= as_of]
    if not applicable:
        return None, "completed_report_on_or_before_as_of_date"
    latest = max(report_date for _, report_date in applicable)
    selected = [report for report, report_date in applicable if report_date == latest]
    if len(selected) != 1:
        return None, "latest_completed_report_tie"
    return selected[0], None


def _linked_loan_reports(graph: EntityGraph, loan: EntityNode, as_of: date | None,
                         config: dict) -> tuple[list[tuple[str, EntityNode]], list[str]]:
    """Select one independently account-scoped report for each linked loan party."""
    selected: list[tuple[str, EntityNode]] = []
    missing: list[str] = []
    for account_id in sorted(graph.loan_accounts(loan.id)):
        report, error = _select_report(graph, account_id, as_of, config)
        if error:
            missing.append(f"account_{account_id}:{error}")
        elif report is not None:
            selected.append((account_id, report))
    return selected, missing


def _select_response(graph: EntityGraph, loan: EntityNode, borrower: str | None) -> tuple[EntityNode | None, str | None]:
    applicable = loan.fields.get("response_id")
    if applicable:
        candidates = [graph.table("LCRESPONSE")[applicable]] if applicable in graph.table("LCRESPONSE") else []
    else:
        candidates = graph.related("LCRESPONSE", "loan_id", loan.id)
        if len(candidates) > 1:
            dated = [(response, iso_date(response.fields.get("score_date"))) for response in candidates]
            if all(score_date is not None for _, score_date in dated):
                latest = max(score_date for _, score_date in dated if score_date is not None)
                candidates = [response for response, score_date in dated if score_date == latest]
    if len(candidates) != 1:
        return None, "unique_latest_response_or_applicable_response_id"
    response = candidates[0]
    if response.fields.get("loan_id") not in (None, loan.id):
        return None, "response_loan_mismatch"
    entities = graph.related("LCREntity", "response_id", response.id)
    if entities and not any(entity.fields.get("account_id") == borrower for entity in entities):
        return None, "response_has_no_borrower_entity_link"
    if response.fields.get("loan_id") is None and not any(entity.fields.get("account_id") == borrower for entity in entities):
        return None, "response_requires_loan_or_borrower_entity_link"
    return response, None


def _synthetic_ready(config: dict) -> bool:
    policy = config.get("test_policy", {})
    resolved = set(policy.get("resolved_conflicts") or [])
    required = REQUIRED_CONFLICTS | set(config["unresolved_conflicts"])
    return (policy.get("synthetic") is True and policy.get("approved") is True
            and isinstance(policy.get("label"), str) and bool(policy["label"].strip())
            and required <= resolved and isinstance(policy.get("criteria"), list) and bool(policy["criteria"]))


def _criterion(criterion: Any, features: dict[str, Any]) -> tuple[bool | None, str]:
    """Small declarative TEST-only predicate language; no eval/executable text."""
    if not isinstance(criterion, dict) or set(criterion) != {"rule_id", "feature", "op", "value"}:
        return None, "Invalid synthetic criterion schema"
    feature, op, expected = criterion["feature"], criterion["op"], criterion["value"]
    if not isinstance(feature, str) or not isinstance(op, str) or not isinstance(criterion["rule_id"], str) or not criterion["rule_id"].startswith("TEST-"):
        return None, "Synthetic criteria require TEST- rule IDs and named features"
    actual = features.get(feature)
    if actual is None or expected is None:
        return None, "Synthetic criterion input is missing"
    try:
        if op in {"ge", "gt", "le", "lt"}:
            left, right = exact_decimal(actual), exact_decimal(expected)
            result = {"ge": left >= right, "gt": left > right, "le": left <= right, "lt": left < right}[op]
        elif op == "eq":
            result = type(actual) is type(expected) and actual == expected
        elif op == "ne":
            result = type(actual) is type(expected) and actual != expected
        elif op == "in" and isinstance(expected, list):
            result = any(type(actual) is type(item) and actual == item for item in expected)
        else:
            return None, "Unsupported synthetic criterion operator"
    except (ValueError, TypeError):
        return None, "Synthetic criterion values have incompatible types"
    return bool(result), "Synthetic fixture criterion only; not business policy"


def _evaluate_loan(graph: EntityGraph, loan: EntityNode, mode: Mode, config: dict,
                   source_available: bool, implementation_guide_available: bool,
                   expanded_spec_available: bool) -> LoanRecommendation:
    borrowers = graph.borrowers(loan.id)
    borrower = next(iter(borrowers)) if len(borrowers) == 1 else None
    guarantors = sorted({account for lid, account, role, _ in graph.associations if lid == loan.id and role == "guarantor"})
    account = graph.table("Account").get(borrower) if borrower else None
    missing: list[str] = []
    conflicts = list(graph.issues)
    unresolved = REQUIRED_CONFLICTS | set(config["unresolved_conflicts"])
    if mode == "TEST" and _synthetic_ready(config):
        unresolved -= set(config["test_policy"]["resolved_conflicts"])
    conflicts.extend(sorted(unresolved))
    evidence = {item.id: item for item in loan.evidence}
    refs: dict[str, list[str]] = copy.deepcopy(loan.field_refs)
    profile = dict(loan.fields)
    rules: list[UnderwritingRuleResult] = []

    def include(node: EntityNode | None, merge: bool = False) -> None:
        if node is None:
            return
        evidence.update({item.id: item for item in node.evidence})
        if merge:
            for key, value in node.fields.items():
                refs.setdefault(key, []).extend(node.field_refs.get(key, []))
                if key in {"currency", "amount_scale", "loan_id", "account_id"}:
                    continue
                equal = (profile.get(key) == value if isinstance(value, Decimal)
                         and isinstance(profile.get(key), Decimal) else str(profile.get(key)) == str(value))
                if key in profile and profile[key] is not None and value is not None and not equal:
                    profile[key] = None
                    conflicts.append("CONFLICTING_PROFILE_FIELD:" + key)
                elif key not in profile:
                    profile[key] = value

    def check(rule_id: str, condition: bool | None, detail: str, fields: tuple[str, ...] = (),
              applicable: bool = True, page: int | None = None) -> None:
        if page is not None and not source_available:
            condition = None
        status = "NOT_APPLICABLE" if not applicable else "UNKNOWN" if condition is None else "PASS" if condition else "FAIL"
        evidence_refs = sorted({item for key in fields for item in refs.get(key, [])})
        rules.append(UnderwritingRuleResult(rule_id=rule_id, status=status, detail=detail, evidence_refs=evidence_refs,
                                           page=page, policy_reference=f"{SOURCE_DOCUMENT} p.{page}" if page else None))
        if status == "UNKNOWN":
            missing.append(rule_id)
        # No loan/account IDs, values, names or free-text details in logs.
        LOGGER.info("underwriting rule_id=%s status=%s provenance=%s", rule_id, status, ",".join(evidence_refs))

    include(account, True)
    for node in graph.related("LoanExtension", "loan_id", loan.id):
        include(node, True)
    for lid, aid, role, relationship_refs in graph.associations:
        if lid != loan.id:
            continue
        for nodes in graph.nodes.values():
            for node in nodes.values():
                for item in node.evidence:
                    if item.id in relationship_refs:
                        evidence[item.id] = item
        refs.setdefault("identity", []).extend(relationship_refs)
    identity = borrower is not None and account is not None and not (account.poisoned or loan.poisoned)
    check("IDENTITY_EXPLICIT_RELATIONSHIP", identity, "An explicit unique borrower relationship and existing account are mandatory.", ("identity", "borrower_account_id"))
    if not identity:
        missing.append("explicit_unique_borrower_account_relationship")
    as_of = iso_date(profile.get("as_of_date"))
    if as_of is None:
        missing.append("explicit_iso_as_of_date")
    report, report_error = _select_report(graph, borrower, as_of, config) if borrower else (None, "borrower_credit_identity")
    if report_error:
        missing.append(report_error)
    include(report, True)
    # Only the selected account/role-scoped report may establish personal FICO.
    profile["fico"] = personal_fico(report)
    refs["fico"] = (report.field_refs.get("personal_fico", report.field_refs.get("fico", []))
                    if report else [])
    response, response_error = _select_response(graph, loan, borrower)
    if response_error:
        missing.append(response_error)
    include(response, True)

    # Indicators/characteristics are retained one-to-many and scoped through
    # analysis -> entity -> selected response AND this loan's borrower account.
    observations: dict[str, list[dict[str, Any]]] = {"indicators": [], "characteristics": []}
    if response and borrower:
        for entity in graph.related("LCREntity", "response_id", response.id):
            if entity.fields.get("account_id") != borrower:
                continue
            include(entity)
            for analysis in graph.related("DataSourceAnalysis", "entity_id", entity.id):
                include(analysis)
                for table, output, mapping_key in (("Indicator", "indicators", "indicator_feature_mapping"),
                                                   ("Characteristic", "characteristics", "characteristic_feature_mapping")):
                    for observation in graph.related(table, "analysis_id", analysis.id):
                        include(observation)
                        raw = observation.fields.get("value")
                        safe_value = str(raw) if isinstance(raw, (str, int, Decimal)) and not isinstance(raw, bool) else raw if isinstance(raw, bool) else None
                        observations[output].append({"record_id": None if observation.id.startswith("observation:") else observation.id,
                                                     "key": observation.fields.get("key"), "value": safe_value,
                                                     "evidence_refs": [item.id for item in observation.evidence]})
                        feature = config["mapping_manifest"].get(mapping_key, {}).get(observation.fields.get("key"))
                        if feature and "value" not in observation.poisoned:
                            # Money pivoting needs explicit source-row currency
                            # normalization and is deliberately not supported.
                            if feature in AMOUNT_KEYS or feature.endswith("_usd") or feature in {"borrower_account_id", "loan_id", "response_id", "fico", "personal_fico", "sbss", "report_category", "report_role"}:
                                conflicts.append("UNSUPPORTED_INDICATOR_TARGET")
                            elif feature in profile and str(profile[feature]) != str(raw):
                                profile[feature] = None
                                conflicts.append("CONFLICTING_INDICATOR_FEATURE")
                            else:
                                profile[feature] = raw
                                refs.setdefault(feature, []).extend(item.id for item in observation.evidence)

    amounts = {key: profile.get(key) if isinstance(profile.get(key), Decimal) else None for key in AMOUNT_KEYS}
    tiers = amounts["tier_i"], amounts["tier_ii"]
    amounts["effective_tgc"] = max(tiers[0], tiers[1]) if tiers[0] is not None and tiers[1] is not None else None
    features: dict[str, Any] = {key: value for key, value in profile.items()
                                if key not in AMOUNT_KEYS and key not in {"currency", "amount_scale", "personal_fico"}}
    features.update(observations)
    features.update({"guarantor_account_ids": guarantors, "as_of_date": as_of.isoformat() if as_of else None,
                     "selected_report_id": report.id if report else None,
                     "selected_response_id": response.id if response else None,
                     "identity_valid": identity,
                     "sbss_bucket": sbss_bucket(profile.get("sbss")),
                     "fico_band": fico_band(profile.get("fico"))})
    # Keep all nonmoney numeric fields lexical and avoid accidentally allowing
    # a response/report from another account to provide a replacement score.
    features["sbss"] = str(profile["sbss"]) if features["sbss_bucket"] else None
    features["fico"] = str(profile["fico"]) if features["fico_band"] else None
    quick = str(profile.get("requested_path", "")).casefold() == "quick_app"
    product = str(profile.get("product", "")).casefold()
    baloc = product in {"express_baloc", "conventional_baloc", "sba_baloc"}
    nfp = _boolean(profile.get("is_nfp"))
    features["quick_app_nfp_eligible"] = not nfp if nfp is not None else None
    check("QUICKAPP_NFP_DIAGNOSTIC", features["quick_app_nfp_eligible"], "Candidate Quick App excludes nonprofits; full underwriting required.", ("is_nfp", "requested_path"), quick, page=2)
    for key, limit, diagnostic in (("new_money", USD_100M, "NEW_MONEY_100M_CANDIDATE"),
                                    ("tier_i", USD_250M, "TIER_I_250M_CANDIDATE")):
        value = amounts[key]
        features[key + "_within_diagnostic_limit"] = value <= limit if value is not None else None
        check(diagnostic, features[key + "_within_diagnostic_limit"], "Draft Quick App candidate limit; request threshold conflict remains unresolved.", (key,), quick, page=2)
    features["requested_amount_le_100k"] = amounts["requested_amount"] <= USD_100K if amounts["requested_amount"] is not None else None
    features["requested_amount_le_100m"] = amounts["requested_amount"] <= USD_100M if amounts["requested_amount"] is not None else None
    supported = product in {"express_baloc", "conventional_baloc", "sba_baloc", "bil", "sba_bil", "term_loan"}
    check("PRODUCT_MAPPING", True if supported else None, "Only explicitly named supported products can be classified.", ("product",))
    secured = _boolean(profile.get("secured"))
    if product in {"express_baloc", "bil", "conventional_baloc"}:
        express = product in {"express_baloc", "bil"}
        threshold = amounts["requested_amount"]
        features["product_amount_diagnostic"] = (threshold <= USD_100M if express else threshold > USD_100M) if threshold is not None and product != "bil" else None
        check("PRODUCT_AMOUNT_DIAGNOSTIC", features["product_amount_diagnostic"], "Draft Express versus conventional BALOC uses USD 100M; BIL boundary unspecified.", ("requested_amount", "product"), page=2)
        check("PRODUCT_SECURITY_DIAGNOSTIC", (not secured if express else secured) if secured is not None else None,
              "Draft Express unsecured versus conventional secured; BIL unspecified.", ("secured", "product"), applicable=product != "bil", page=2)
    special_flags = [_boolean(profile.get(key)) for key in ("is_startup", "is_investment_re")]
    special = True if True in special_flags else False if all(flag is False for flag in special_flags) else None
    features["baloc_special_cap_within_25m"] = amounts["requested_amount"] <= USD_25M if amounts["requested_amount"] is not None else None
    check("BALOC_SPECIAL_25M_CANDIDATE", features["baloc_special_cap_within_25m"] if special is True else None,
          "Draft startup/investment-real-estate BALOC cap USD 25M; never a general term-loan cap.",
          ("requested_amount", "is_startup", "is_investment_re"), baloc and special is not False, page=2)
    opened = iso_date(profile.get("account_opened"))
    increase = _boolean(profile.get("is_increase"))
    for months in (6, 12):
        elapsed = as_of >= calendar_month_anniversary(opened, months) if opened and as_of and opened <= as_of else None
        features[f"increase_at_least_{months}_months"] = elapsed
        check(f"INCREASE_{months}_MONTHS_DIAGNOSTIC", elapsed,
              "Calendar-month diagnostic only; six-versus-twelve-month conflict remains unresolved.",
              ("account_opened", "as_of_date", "is_increase"), baloc and increase is not False)
    check("SBSS_BUCKET_DIAGNOSTIC", True if features["sbss_bucket"] else None, "Score bucket only; no SBSS/FICO conjunction is inferred.", ("sbss",))
    check("FICO_BAND_DIAGNOSTIC", True if features["fico_band"] else None, "FICO band only; no SBSS/FICO conjunction is inferred.", ("fico",))
    ownership = _number(profile.get("ownership_years"))
    features["ownership_at_least_2_years"] = ownership >= 2 if ownership is not None and ownership >= 0 else None
    check("OWNERSHIP_2_YEARS_DIAGNOSTIC", features["ownership_at_least_2_years"], "Two-year ownership diagnostic requires explicit ownership years.", ("ownership_years",))
    features["ebitda_nonnegative"] = amounts["ebitda"] >= 0 if amounts["ebitda"] is not None else None
    check("EBITDA_NONNEGATIVE_DIAGNOSTIC", features["ebitda_nonnegative"], "Draft negative EBITDA knock-out candidate, not an operational denial. No EBIT derivation.", ("ebitda",), page=2)
    count = _number(profile.get("bankruptcy_count"))
    discharge = iso_date(profile.get("bankruptcy_discharge_date"))
    bankruptcy_clear = True if count == 0 else None
    if count is not None and count > 0 and count == count.to_integral_value() and discharge and as_of and discharge <= as_of:
        bankruptcy_clear = as_of >= calendar_anniversary(discharge, 5)
    features["bankruptcy_discharge_at_least_5_years"] = bankruptcy_clear
    check("BANKRUPTCY_5_CALENDAR_YEARS_DIAGNOSTIC", bankruptcy_clear, "Draft knock-out candidate: discharge recency uses five calendar years; future/absent dates unknown.", ("bankruptcy_count", "bankruptcy_discharge_date", "as_of_date"), page=2)
    dob = iso_date(profile.get("dob"))
    age = None
    if dob and as_of and dob <= as_of:
        age = as_of.year - dob.year - (as_of < calendar_anniversary(dob, as_of.year - dob.year))
    features["age_years"] = age
    minimum = config["minimum_age"]
    features["minimum_age_met"] = age >= minimum if age is not None and minimum is not None else None
    check("MINIMUM_AGE", features["minimum_age_met"], "Draft under-18 knock-out candidate; explicit DOB and as-of date required.", ("dob", "as_of_date"), page=2)
    public_record_code = _number(profile.get("max_delinquency_public_records"))
    valid_public_record_code = (public_record_code is not None
                                and public_record_code == public_record_code.to_integral_value()
                                and public_record_code in {Decimal("0"), Decimal("1"), Decimal("2"), Decimal("3")})
    features["public_record_knockout_candidate"] = (
        public_record_code in {Decimal("1"), Decimal("2"), Decimal("3")}
        if valid_public_record_code else None
    )
    check("MAX_DELINQUENCY_PUBLIC_RECORDS_KNOCKOUT_CANDIDATE",
          None if features["public_record_knockout_candidate"] is None
          else not features["public_record_knockout_candidate"],
          "Screenshot diagnostic: public-record codes 1, 2, or 3 match a knockout candidate; explicit code 0 does not. Not a production lending decision.",
          ("max_delinquency_public_records",))
    disposition = str(profile.get("ofac_disposition", "")).casefold()
    features["ofac_confirmed_match"] = True if disposition == "confirmed_match" else False if disposition == "cleared" else None
    check("OFAC_CONFIRMED_MATCH", not features["ofac_confirmed_match"] if features["ofac_confirmed_match"] is not None else None,
          "Confirmed match requires compliance review; alert alone never proves a match.", ("ofac_disposition",))
    for field_name in ("ofac_alert", "credit_freeze"):
        flag = _boolean(profile.get(field_name))
        features[field_name + "_clear"] = not flag if flag is not None else None
        check(field_name.upper() + "_CLEAR", features[field_name + "_clear"], "Alert/freeze requires review, not an automatic lending denial.", (field_name,))
    naics, risk_codes = profile.get("naics"), config["high_risk_naics"]
    valid_naics = isinstance(naics, str) and re.fullmatch(r"\d{2,6}", naics) is not None
    features["naics_not_high_risk"] = not any(str(naics).startswith(code) for code in risk_codes) if risk_codes and valid_naics else None
    check("HIGH_RISK_NAICS", features["naics_not_high_risk"], "Draft example prefixes only, not an exhaustive approved industry policy.", ("naics",), page=2)
    # Guarantors have independent, account-scoped credit evidence; never replace
    # borrower FICO with a convenient guarantor/other-account value.
    parties = []
    for index, guarantor in enumerate(guarantors, 1):
        guarantor_account = graph.table("Account").get(guarantor)
        include(guarantor_account)
        guarantor_report, error = _select_report(graph, guarantor, as_of, config)
        include(guarantor_report)
        if error:
            missing.append("guarantor_" + str(index) + ":" + error)
        band = fico_band(personal_fico(guarantor_report, "guarantor"))
        parties.append({"account_id": guarantor, "role": "guarantor", "selected_report_id": guarantor_report.id if guarantor_report else None, "fico_band": band})
        check("GUARANTOR_" + str(index) + "_CREDIT_DIAGNOSTIC", True if band else None,
              "Guarantor credit is independently account-scoped; aggregation is not inferred.")
    features["guarantors"] = parties
    check("POLICY_SOURCE_V4", True if source_available else None, "Missing/unpinned v4 PDF cannot be substituted with the older draft.")
    fico_score, sbss_score = _number(features["fico"]), _number(features["sbss"])
    check("PERSONAL_FICO_650_CANDIDATE", fico_score >= 650 if fico_score is not None else None,
          "Draft personal FICO knock-out candidate. Personal score only; multi-owner averaging is not inferred.", ("fico",), page=2)
    check("SBSS_160_CANDIDATE", sbss_score >= 160 if sbss_score is not None else None,
          "Draft SBSS below 160 knock-out candidate, not an operational denial.", ("sbss",), page=2)
    bucket = features["sbss_bucket"]
    features["sbss_candidate_path"] = {"LT_160": "KNOCKOUT_CANDIDATE", "160_189": "UNSPECIFIED_OWNERSHIP_FICO_AMOUNT",
                                        "190_199": "SBA_CANDIDATE", "GE_200": "OWNERSHIP_NAICS_CANDIDATE"}.get(bucket)
    check("SBSS_160_189_BRANCH", None, "Ownership/FICO/amount conjunction is unspecified; no invented thresholds.",
          ("sbss", "fico", "ownership_years", "requested_amount"), applicable=bucket == "160_189", page=2)
    check("SBSS_190_199_SBA_CANDIDATE", True if bucket == "190_199" else None,
          "SBA candidate route only, not guaranteed eligibility or approval.", ("sbss",), applicable=bucket == "190_199", page=2)
    check("SBSS_200_OWNERSHIP_NAICS_CANDIDATE", None,
          "Ownership and NAICS candidate path; complete approved conjunction not specified.",
          ("sbss", "ownership_years", "naics"), applicable=bucket == "GE_200", page=2)
    for gate in ("QUICKAPP_POLICY", "PRODUCT_BALOC_POLICY", "SBSS_FICO_BRANCH_POLICY", "OWNERSHIP_EBITDA_POLICY",
                 "BANKRUPTCY_POLICY", "GROUP_EXPOSURE_POLICY", "INCREASE_WAIT_POLICY"):
        check(gate, None, "Verified draft candidates do not resolve policy conflicts or authorize production decisions.", page=2)

    # The implementation guide is independently pinned. Its Section 8 tree is
    # only a diagnostic and never changes recommendation_path/final_action.
    knockout_inputs_known = all(value is not None for value in (
        features["sbss_bucket"], features["fico_band"], features["ownership_at_least_2_years"],
        features["ebitda_nonnegative"], features["bankruptcy_discharge_at_least_5_years"],
        features["minimum_age_met"], features["naics_not_high_risk"],
        features["public_record_knockout_candidate"],
        features["ofac_confirmed_match"], features["ofac_alert_clear"], features["credit_freeze_clear"],
    ))
    knockout_detected = None if not knockout_inputs_known else any((
        sbss_score is not None and sbss_score < 160,
        fico_score is not None and fico_score < 650,
        features["ebitda_nonnegative"] is False,
        features["bankruptcy_discharge_at_least_5_years"] is False,
        features["minimum_age_met"] is False,
        features["public_record_knockout_candidate"] is True,
    ))
    if quick:
        quick_app_eligible = (
            False if nfp is True else
            None if nfp is None or amounts["new_money"] is None or amounts["tier_i"] is None else
            amounts["new_money"] <= USD_100M and amounts["tier_i"] <= USD_250M
        )
    elif str(profile.get("requested_path", "")).casefold() == "full_underwriting":
        quick_app_eligible = False
    else:
        quick_app_eligible = None
    ofac_alert = _boolean(profile.get("ofac_alert"))
    credit_freeze = _boolean(profile.get("credit_freeze"))
    manual_review_required = credit_freeze is not False or features["ofac_confirmed_match"] is None
    strategy = evaluate_implementation_strategy(
        identity_valid=identity,
        quick_app_eligible=quick_app_eligible,
        knockouts_completed=knockout_inputs_known,
        knockout_detected=knockout_detected,
        sbss=features["sbss"],
        fico=features["fico"],
        ownership_years=profile.get("ownership_years"),
        requested_amount=amounts["requested_amount"],
        naics=profile.get("naics"),
        vehicle_only=_boolean(profile.get("vehicle_only")),
        product_review_required=(not supported or (baloc and increase is True)),
        compliance_review_required=ofac_alert is True or features["ofac_confirmed_match"] is True,
        manual_review_required=manual_review_required,
        source_available=implementation_guide_available,
        policy_conflicts=REQUIRED_CONFLICTS | set(config["unresolved_conflicts"]),
        high_risk_naics=config["high_risk_naics"],
    )
    strategy_inputs = {
        "identity_valid": identity, "quick_app_eligible": quick_app_eligible,
        "knockouts_completed": knockout_inputs_known, "knockout_detected": knockout_detected,
        "sbss": features["sbss"], "fico": features["fico"],
        "ownership_years": profile.get("ownership_years"),
        "requested_amount_usd": amounts["requested_amount"], "naics": profile.get("naics"),
        "vehicle_only": _boolean(profile.get("vehicle_only")),
    }
    strategy["audit"] = {
        "policy_version": config["policy_version"],
        "source_document": IMPLEMENTATION_GUIDE_DOCUMENT,
        "source_sha256": IMPLEMENTATION_GUIDE_SHA256,
        "source_available": implementation_guide_available,
        "source_pages": {"section_8_decision_tree": 4, "processing_stages": 5, "out_of_scope": 5},
        "input_fingerprint_sha256": hashlib.sha256(json.dumps(
            {key: None if value is None else str(value) if isinstance(value, (Decimal, date)) else value
             for key, value in strategy_inputs.items()}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
        "evidence_refs": sorted({ref for key in (
            "identity", "requested_path", "new_money", "tier_i", "sbss", "fico", "ownership_years",
            "requested_amount", "naics", "vehicle_only", "ebitda", "bankruptcy_count",
            "bankruptcy_discharge_date", "dob", "max_delinquency_public_records",
            "ofac_disposition", "ofac_alert", "credit_freeze",
        ) for ref in refs.get(key, [])}),
        "authorization": "NOT_ISSUED",
        "human_review_required": True,
    }
    features["implementation_guide_diagnostics"] = strategy
    features["out_of_scope_diagnostics"] = strategy["out_of_scope"]
    expanded = evaluate_expanded_spec(graph=graph, loan=loan, profile=profile,
        features=features, refs=refs, strategy=strategy, source_available=expanded_spec_available)
    synthetic = mode == "TEST" and _synthetic_ready(config)
    criterion_results: list[bool | None] = []
    if synthetic:
        test_features = {**features, **amounts, **{AMOUNT_NAMES[key]: value for key, value in amounts.items()}}
        for index, criterion in enumerate(config["test_policy"]["criteria"]):
            outcome, detail = _criterion(criterion, test_features)
            rule_id = criterion.get("rule_id") if isinstance(criterion, dict) else None
            # Invalid config cannot inject arbitrary borrower values into logs.
            safe_id = rule_id if isinstance(rule_id, str) and re.fullmatch(r"TEST-[A-Z0-9_-]{1,80}", rule_id) else "TEST-INVALID-" + str(index)
            if safe_id.startswith("TEST-INVALID-"):
                outcome = None
            criterion_feature = criterion.get("feature") if isinstance(criterion, dict) else None
            check(safe_id, outcome, detail, (criterion_feature,) if isinstance(criterion_feature, str) else ())
            criterion_results.append(outcome)
    data_hold = bool(graph.issues) or not identity or any(item.startswith(("CONFLICTING_", "UNSUPPORTED_INDICATOR")) for item in conflicts)
    required_amounts = ("requested_amount", "new_money", "tier_i", "tier_ii", "ebitda")
    required_features = ("sbss_bucket", "fico_band", "ownership_at_least_2_years", "minimum_age_met", "naics_not_high_risk", "bankruptcy_discharge_at_least_5_years")
    needs_review = not synthetic or any(value is None for value in criterion_results)
    needs_review = needs_review or not knockout_inputs_known
    needs_review = needs_review or report_error is not None or response_error is not None
    needs_review = needs_review or any(item.startswith("guarantor_") for item in missing)
    needs_review = needs_review or any(amounts[key] is None for key in required_amounts)
    needs_review = needs_review or any(features.get(key) is None for key in required_features)
    needs_review = needs_review or not supported or not profile.get("requested_path") or nfp is None
    needs_review = needs_review or features["ofac_confirmed_match"] is not False
    needs_review = needs_review or features["ofac_alert_clear"] is not True or features["credit_freeze_clear"] is not True
    if data_hold:
        path = "SIMULATED_REVIEW" if mode == "TEST" else "DATA_QUALITY_HOLD"
    elif mode == "PRODUCTION":
        path = "POLICY_REVIEW_REQUIRED"
    elif needs_review:
        path = "SIMULATED_REVIEW"
    elif any(value is False for value in criterion_results):
        path = "SIMULATED_DECLINE_PATH"
    else:
        path = "SIMULATED_APPROVE_PATH"
    # A missing input cannot be hidden behind criteria that only select one good
    # score: TEST fixtures must explicitly require their necessary features.
    explanation = ("Synthetic TEST fixture recommendation only; not business policy or an operational lending decision. "
                   if path.startswith("SIMULATED_") else "No operational lending decision. ")
    explanation += "Authorized human review is always required; v4 policy evidence and unresolved conflicts remain separate from deterministic diagnostics."
    # Ensure JSON-compatible nonmoney data without float financial leakage.
    def lexical(value: Any) -> Any:
        if isinstance(value, (Decimal, date)):
            return str(value)
        if isinstance(value, float):
            return None
        if isinstance(value, dict):
            return {key: lexical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [lexical(item) for item in value]
        return value

    return LoanRecommendation(loan_id=loan.id, borrower_account_id=borrower, mode=mode,
                              policy_version=config["policy_version"], normalized_amounts_usd={AMOUNT_NAMES[key]: value for key, value in amounts.items()},
                              normalized_data=lexical(features), rule_results=rules,
                              source_evidence=sorted(evidence.values(), key=lambda item: item.id),
                              missing_inputs=sorted(set(missing)), conflicts=sorted(set(conflicts)),
                              recommendation_path=path,
                              eligibility_route=strategy["eligibility_route"],
                              credit_assessment=strategy["credit_assessment"],
                              authorization="NOT_ISSUED", strategy_branch=strategy["strategy_branch"],
                              expanded_spec=expanded,
                              final_action="NO_OPERATIONAL_ACTION" if mode == "TEST" else "MANUAL_REVIEW",
                              explanation=explanation)


def evaluate_underwriting(documents: Iterable[Any], mode: Mode = "PRODUCTION", *,
                          policy_config: Mapping[str, Any] | None = None,
                          selected_account_id: str | None = None) -> UnderwritingBatch:
    """Evaluate extracted documents, returning safe per-loan graph diagnostics.

    Strict uppercase mode, fresh config, no I/O except local config/PDF integrity
    verification, no persistence or LLM. See module contract for TEST injection.
    """
    if mode not in ("TEST", "PRODUCTION"):
        raise ValueError("mode must be TEST or PRODUCTION")
    config = load_underwriting_config(policy_config)
    source = Path(__file__).parent / "data" / SOURCE_DOCUMENT
    digest = config.get("source_sha256")
    try:
        source_available = bool(digest == SOURCE_SHA256 and source.is_file()
                                and hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA256)
    except OSError:
        source_available = False
    implementation_source = Path(__file__).parent / "data" / IMPLEMENTATION_GUIDE_DOCUMENT
    implementation_digest = config.get("implementation_guide_source_sha256")
    try:
        implementation_guide_available = bool(
            implementation_digest == IMPLEMENTATION_GUIDE_SHA256
            and implementation_source.is_file()
            and hashlib.sha256(implementation_source.read_bytes()).hexdigest() == IMPLEMENTATION_GUIDE_SHA256
        )
    except OSError:
        implementation_guide_available = False
    expanded_source = Path(__file__).parent / "data" / EXPANDED_SPEC_DOCUMENT
    try:
        expanded_spec_available = bool(
            config.get("expanded_spec_source_sha256") == EXPANDED_SPEC_SHA256
            and expanded_source.is_file()
            and hashlib.sha256(expanded_source.read_bytes()).hexdigest() == EXPANDED_SPEC_SHA256
        )
    except OSError:
        expanded_spec_available = False
    if selected_account_id is not None:
        if not isinstance(selected_account_id, str) or not selected_account_id.strip():
            raise ValueError("selected_account_id must be a nonempty account ID")
        selected_account_id = selected_account_id.strip()
    graph = build_entity_graph(documents, config, selected_account_id=selected_account_id)
    if not graph.table("Loan"):
        graph.issue("NO_EXPLICIT_LOAN_RECORDS")
    results = [_evaluate_loan(graph, loan, mode, config, source_available, implementation_guide_available, expanded_spec_available)
               for loan in sorted(graph.table("Loan").values(), key=lambda item: item.id)]
    return UnderwritingBatch(mode=mode, policy_version=config["policy_version"], source_available=source_available,
                             implementation_guide_source_document=IMPLEMENTATION_GUIDE_DOCUMENT,
                             implementation_guide_source_sha256=IMPLEMENTATION_GUIDE_SHA256,
                             implementation_guide_available=implementation_guide_available,
                             expanded_spec_available=expanded_spec_available,
                             data_quality_issues=graph.issues, loan_results=results)