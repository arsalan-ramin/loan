"""Isolated screenshot simulation v1; never authoritative underwriting policy.

Graph insertion order is source order. The first account-linked report is used
without checking report date/completion; poisoned fields remain unknown. No raw
characteristics are pivoted, no EBIT/net income is substituted for EBITDA, and
no graph is modified. EBITDA and public records are non-gating diagnostics.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .entity_graph import EntityGraph, EntityNode, iso_date
from .expanded_spec import REASONS
from .underwriting_models import USD_100K, USD_100M, exact_decimal

VERSION = "screenshot-test-demo-v1"
SELECTION_BASIS = "FIRST_ACCOUNT_MATCH_FOR_TEST_DEMO"
FICO_AGGREGATION_BASIS = "LINKED_BORROWER_GUARANTOR_ACCOUNT_AVERAGE"
AGE_AGGREGATION_BASIS = "LINKED_BORROWER_GUARANTOR_MIN_COMPLETED_CALENDAR_AGE"


def _matched_reasons(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only four knockout demo predicates; the expanded registry stays independent."""
    catalog = {code: label for code, label, *_ in REASONS}
    mappings = {
        "bankruptcy_clear": (catalog["BANKRUPTCY"], "SUPPLIED_DECLINE_REASON_CATALOG"),
        "fico": ("Average personal FICO below 650", "SUPPLIED_STRATEGY"),
        "sbss": ("SBSS below 160", "SUPPLIED_STRATEGY"),
        "age_years": ("Age below 18", "SUPPLIED_STRATEGY"),
    }
    return [{"criterion": item["criterion"], "label": mappings[item["criterion"]][0],
             "source": mappings[item["criterion"]][1], "status": "MATCHED",
             "evidence_refs": list(item["evidence_refs"]), "adverse_action_code": None}
            for item in criteria if item["criterion"] in mappings and item["status"] == "FAIL"
            and item.get("knockout", True) is not False]


def _number(value: Any) -> Decimal | None:
    try:
        return exact_decimal(value)
    except ValueError:
        return None


def _anniversary(original: date, years: int) -> date:
    year = original.year + years
    return date(year, original.month, min(original.day, monthrange(year, original.month)[1]))


def evaluate_demo(graph: EntityGraph, loan: EntityNode, config: dict[str, Any], *,
                  mode: str, response: EntityNode | None = None,
                  age_reference_date: date | None = None) -> dict[str, Any] | None:
    """Produce only additive TEST facts; PRODUCTION does not evaluate the tree."""
    if mode != "TEST":
        return None
    borrowers = graph.borrowers(loan.id)
    borrower = next(iter(borrowers)) if len(borrowers) == 1 else None
    account = graph.table("Account").get(borrower or "")
    reports = graph.related("CreditReport", "account_id", borrower) if borrower else []
    report = reports[0] if reports else None
    criteria: list[dict[str, Any]] = []
    facts: dict[str, Any] = {}
    missing: list[str] = []
    conflicts: list[str] = []
    identity = account is not None and not ({"borrower_account_id"} & loan.poisoned)
    profile_nodes = [loan, *graph.related("LoanExtension", "loan_id", loan.id)]
    if account is not None:
        profile_nodes.append(account)

    def read(field: str, nodes: list[EntityNode]) -> tuple[Any, list[str]]:
        refs = sorted({ref for node in nodes for ref in node.field_refs.get(field, [])})
        if any(field in node.poisoned for node in nodes):
            return None, refs
        values = [node.fields[field] for node in nodes if node.fields.get(field) is not None]
        if not values or any(str(value) != str(values[0]) for value in values):
            return None, refs
        return values[0], refs

    def add(key: str, value: Any, clear: bool | None, refs: list[str], detail: str,
            *, knockout: bool = True) -> None:
        facts[key] = str(value) if isinstance(value, Decimal) else value
        status = "UNKNOWN" if clear is None else "PASS" if clear else "FAIL"
        criteria.append({"criterion": key, "value": facts[key], "status": status,
                         "evidence_refs": refs, "detail": detail, "knockout": knockout})
        if clear is None and knockout:
            missing.append(key)

    report_nodes = [report] if report is not None else []
    code_raw, code_refs = read("max_delinquency_public_records", report_nodes)
    code = _number(code_raw)
    code = code if code in {Decimal(0), Decimal(1), Decimal(2), Decimal(3)} else None
    add("public_record_code", code, None, code_refs,
        "Observed diagnostic only; public records are excluded from the TEST demo decision flow.",
        knockout=False)
    criteria[-1]["status"] = "OBSERVED" if code is not None else "UNKNOWN"

    ebitda_raw, ebitda_refs = read("ebitda", profile_nodes)
    ebitda = _number(ebitda_raw)
    add("ebitda_usd", ebitda, None, ebitda_refs,
        "Observed diagnostic only; EBITDA is excluded from the TEST demo decision flow. "
        "Only explicit normalized EBITDA; neither EBIT nor net income is substituted. No clearance implied.",
        knockout=False)
    criteria[-1]["status"] = "OBSERVED" if ebitda is not None else "UNKNOWN"

    as_of_raw, date_refs = read("as_of_date", [loan])
    as_of = iso_date(as_of_raw)
    age_reference = as_of
    age_reference_basis = "EXPLICIT_APPLICATION_AS_OF_DATE" if as_of else "UNAVAILABLE"
    # Only an absent date can use today's UTC calendar date for age. Invalid or
    # poisoned supplied dates must stay unresolved, even with an injected date.
    if as_of_raw is None and not date_refs and "as_of_date" not in loan.poisoned:
        if age_reference_date is not None and type(age_reference_date) is not date:
            raise TypeError("age_reference_date must be a calendar date")
        age_reference = age_reference_date if age_reference_date is not None else datetime.now(timezone.utc).date()
        age_reference_basis = "CURRENT_UTC_DATE_FOR_AGE"
    count_raw, count_refs = read("bankruptcy_count", report_nodes)
    count = _number(count_raw)
    count = count if count is not None and count >= 0 and count == count.to_integral_value() else None
    discharge_raw, discharge_refs = read("bankruptcy_discharge_date", report_nodes)
    discharge = iso_date(discharge_raw)
    bankruptcy_clear = True if count == 0 else None
    if count is not None and count > 0 and discharge and as_of and discharge <= as_of:
        bankruptcy_clear = as_of.year - discharge.year >= 5 and as_of >= _anniversary(discharge, 5)
    add("bankruptcy_clear", bankruptcy_clear, bankruptcy_clear,
        sorted(set(count_refs + discharge_refs + date_refs)),
        "Explicit zero count needs no discharge date. Positive count needs verified discharge and application as-of dates; less than five calendar years matches a knockout.")
    facts["bankruptcy_count"] = str(count) if count is not None else None

    # Share validated graph roles for age and FICO, once per account. Guarantors
    # never supply the borrower-specific profile/report checks above.
    party_roles: dict[str, set[str]] = {}
    party_refs: dict[str, set[str]] = {}
    for loan_id, account_id, role, refs in graph.associations:
        if loan_id == loan.id and role in {"borrower", "guarantor"}:
            party_roles.setdefault(account_id, set()).add(role)
            party_refs.setdefault(account_id, set()).update(refs)
    for account_id in sorted(borrowers):
        party_roles.setdefault(account_id, set()).add("borrower")
        party_refs.setdefault(account_id, set()).update(loan.field_refs.get("borrower_account_id", []))
    age_audit: list[dict[str, Any]] = []
    age_nodes: list[EntityNode] = []
    age_refs: set[str] = set()
    dob_refs: set[str] = set()
    ages: list[int] = []
    for account_id, roles in party_roles.items():
        person = graph.table("Account").get(account_id)
        refs = set(party_refs[account_id]) | set(date_refs)
        person_age = None
        age_status = "NO_ACCOUNT_MATCH"
        if person is not None:
            age_nodes.append(person)
            # Graph ID evidence is outside canonical field_refs. Keep it even
            # when no DOB cell exists, to audit the unresolved Account itself.
            field_evidence = {ref for values in person.field_refs.values() for ref in values}
            refs.update(item.id for item in person.evidence if item.id not in field_evidence)
            dob_refs.update(person.field_refs.get("dob", []))
            refs.update(person.field_refs.get("dob", []))
            for field in person.poisoned:
                refs.update(person.field_refs.get(field, []))
            raw, _ = read("dob", [person])
            dob = iso_date(raw)
            if person.poisoned:
                age_status = "CONFLICTING_ACCOUNT"
            elif dob is None:
                age_status = "MISSING_OR_INVALID_DOB"
            elif age_reference is None:
                age_status = "MISSING_OR_INVALID_AS_OF_DATE"
            elif dob > age_reference:
                age_status = "FUTURE_DOB"
            else:
                completed = (age_reference.year - dob.year
                             - (age_reference < _anniversary(dob, age_reference.year - dob.year)))
                if 0 <= completed <= 150:
                    person_age, age_status = completed, "VALID_COMPLETED_CALENDAR_AGE"
                    ages.append(completed)
                else:
                    age_status = "AGE_OUT_OF_RANGE"
        age_refs.update(refs)
        age_audit.append({"account_id": account_id, "party_roles": sorted(roles),
                          "age_years": person_age, "age_status": age_status,
                          "status": "UNKNOWN" if person_age is None else "FAIL" if person_age < 18 else "PASS",
                          "evidence_refs": sorted(refs)})
    age_complete = bool(ages) and len(ages) == len(party_roles)
    underage = any(value < 18 for value in ages)
    age = min(ages) if ages and (age_complete or underage) else None
    add("age_years", age, False if underage else True if age_complete else None, sorted(age_refs),
        "Each linked borrower/guarantor must be at least 18 using only that Account DOB and explicit loan as-of date, "
        "or the recorded current UTC date for age only when the loan as-of date is absent. "
        "Completed calendar ages (0–150) are never averaged; the scalar is the minimum when all are valid, "
        "or the minimum known underage age. Any known under-18 is FAIL even with unresolved parties; "
        "otherwise any unresolved party is UNKNOWN. Invalid/conflicting loan dates stay UNKNOWN; report dates are never used.")
    if len(ages) < len(party_roles):
        # A known knockout does not erase the other parties' unresolved ages.
        missing.append("age_years")

    fico_audit: list[dict[str, Any]] = []
    fico_nodes: list[EntityNode] = []
    fico_refs: set[str] = set()
    scores: list[Decimal] = []
    for account_id, roles in party_roles.items():
        candidates = graph.related("CreditReport", "account_id", account_id)
        selected = candidates[0] if candidates else None
        refs = set(party_refs[account_id])
        score = None
        status = "NO_ACCOUNT_MATCH"
        if selected is not None:
            fico_nodes.append(selected)
            for field in ("account_id", "personal_fico", "fico", "report_category", "report_role"):
                refs.update(selected.field_refs.get(field, []))
            # A supplied canonical field, including a poisoned/null one, must
            # not fall through to a convenient generic score.
            explicit = ("personal_fico" in selected.fields
                        or "personal_fico" in selected.field_refs
                        or "personal_fico" in selected.poisoned)
            field = "personal_fico" if explicit else "fico"
            category = str(selected.fields.get("report_category", "")).strip().casefold()
            role = str(selected.fields.get("report_role", "")).strip().casefold()
            typed = explicit or (category in {"personal", "consumer", "personal_fico"} and role in roles)
            relevant = {"account_id", field} | (set() if explicit else {"report_category", "report_role"})
            if selected.poisoned & relevant:
                status = "CONFLICTING_REPORT"
            elif not typed:
                status = "UNTYPED_PERSONAL_FICO"
            else:
                raw, _ = read(field, [selected])
                number = _number(raw)
                if number is not None and 300 <= number <= 850 and number == number.to_integral_value():
                    score, status = number, "VALID_PERSONAL_FICO"
                    scores.append(number)
                else:
                    status = "MISSING_OR_INVALID_PERSONAL_FICO"
        fico_refs.update(refs)
        fico_audit.append({"account_id": account_id, "party_roles": sorted(roles),
                           "selected_report_id": selected.id if selected else None,
                           "report_status": "FIRST_ACCOUNT_MATCH" if selected else "NO_ACCOUNT_MATCH",
                           "score_status": status, "score": str(score) if score is not None else None,
                           "evidence_refs": sorted(refs)})
    fico = (sum(scores, Decimal(0)) / Decimal(len(scores))
            if scores and len(scores) == len(party_roles) else None)
    add("fico", fico, fico >= 650 if fico is not None else None, sorted(fico_refs),
        "Average personal FICO across all explicitly loan-linked borrower and guarantor accounts, once per account; "
        "below 650 matches a knockout. Each raw score must be integral 300–850; fractional averages are allowed. "
        "Any missing, conflicting, invalid or untyped party score makes the entire average UNKNOWN.")

    # Prefer the explicitly loan-scoped Query 2 scalar. A strict selected response
    # may supply a canonical score, but this module never changes response selection.
    sbss_raw, sbss_refs = read("sbss", [loan])
    if sbss_raw is None and not sbss_refs and response is not None:
        sbss_raw, sbss_refs = read("sbss", [response])
    sbss = _number(sbss_raw)
    sbss = sbss if sbss is not None and 0 <= sbss <= 300 and sbss == sbss.to_integral_value() else None
    add("sbss", sbss, sbss >= 160 if sbss is not None else None, sbss_refs,
        "SBSS below 160 matches a knockout, independently of missing report dates or other inputs.")

    ownership_raw, ownership_refs = read("ownership_years", profile_nodes)
    ownership = _number(ownership_raw)
    ownership = ownership if ownership is not None and ownership >= 0 else None
    amount_raw, amount_refs = read("requested_amount", profile_nodes)
    amount = _number(amount_raw)
    amount = amount if amount is not None and amount >= 0 else None
    naics_raw, naics_refs = read("naics", profile_nodes)
    naics = naics_raw if isinstance(naics_raw, str) and re.fullmatch(r"\d{2,6}", naics_raw) else None
    vehicle_raw, vehicle_refs = read("vehicle_only", profile_nodes)
    vehicle = vehicle_raw if type(vehicle_raw) is bool else (
        {"true": True, "false": False, "yes": True, "no": False, "1": True, "0": False,
         "y": True, "n": False}.get(str(vehicle_raw).strip().casefold()))
    facts.update(ownership_years=str(ownership) if ownership is not None else None,
                 requested_amount_usd=str(amount) if amount is not None else None,
                 naics=naics, vehicle_only=vehicle)

    label, route, branch, rationale = "Refer", None, "INPUTS_UNKNOWN", "Required diagram inputs remain unknown."
    failures = [item for item in criteria if item["knockout"] and item["status"] == "FAIL"]
    if not identity:
        missing.append("explicit_unique_borrower_account_relationship")
        rationale = "A unique explicit borrower account relationship is required."
    if failures:
        label, branch = "Decline", "KNOWN_KNOCKOUT"
        reasons = {
            "bankruptcy_clear": "Verified bankruptcy discharge is less than five calendar years ago",
            "age_years": f"Age {age} is below 18",
            "fico": f"Average personal FICO {facts['fico']} is below 650",
            "sbss": f"SBSS {facts['sbss']} is below 160",
        }
        rationale = "; ".join(reasons[item["criterion"]] for item in failures) + "."
    elif identity:
        # Determine a branch even with unrelated unknown knockout inputs. A known
        # amount decline is still a risk match; UNKNOWN never clears approval.
        if sbss is not None and sbss >= 160:
            if sbss < 190:
                if ownership is None:
                    missing.append("ownership_years")
                elif ownership >= 2:
                    route, branch = "SBA", "SBSS_160_189_OWNERSHIP_GE_2"
                elif fico is not None and fico >= 760:
                    route, branch = "SBA", "SBSS_160_189_OWNERSHIP_LT_2_FICO_GE_760"
                elif fico is not None and 650 <= fico < 760:
                    if amount is None:
                        missing.append("requested_amount_usd")
                    elif amount <= USD_100K:
                        route, branch = "SBA", "SBSS_160_189_AMOUNT_LE_100K"
                    elif amount <= USD_100M:
                        branch = "REQUEST_THRESHOLD_CONFLICT"
                        conflicts.append("REQUEST_THRESHOLD_100M_VS_100K")
                        rationale = "Requested amount is above USD100K and at most USD100M; screenshot thresholds conflict."
                    else:
                        label, branch = "Decline", "SBSS_160_189_AMOUNT_GT_100M"
                        rationale = "Requested amount exceeds both conflicting screenshot thresholds (USD100K and USD100M)."
            elif sbss < 200:
                route, branch = "SBA", "SBSS_190_199"
            elif ownership is None:
                missing.append("ownership_years")
            elif ownership < 2:
                route, branch = "SBA", "SBSS_GE_200_OWNERSHIP_LT_2"
            elif naics is None or not config.get("high_risk_naics"):
                missing.append("naics_or_listed_high_risk_prefixes")
            else:
                high_risk = any(naics.startswith(prefix) for prefix in config["high_risk_naics"])
                route = "SBA" if high_risk else "CONVENTIONAL"
                branch = "SBSS_GE_200_OWNERSHIP_GE_2_" + ("HIGH_RISK_NAICS" if high_risk else "OTHER_NAICS")
        if route == "SBA":
            if vehicle is None:
                missing.append("vehicle_only")
            elif vehicle:
                route = "CONVENTIONAL"
                branch += "_VEHICLE_ONLY_OVERRIDE"
        if (route and not missing and not conflicts
                and all(item["status"] == "PASS" for item in criteria if item["knockout"])):
            label = "Approve"
            rationale = f"All four knockout checks passed; {branch} selects {route} in this simulation."
        elif missing and label != "Decline" and not conflicts:
            rationale = "Unknown diagram inputs: " + ", ".join(dict.fromkeys(missing)) + "."

    for key, value, refs in (("ownership_years", facts["ownership_years"], ownership_refs),
                             ("requested_amount_usd", facts["requested_amount_usd"], amount_refs),
                             ("naics", naics, naics_refs), ("vehicle_only", vehicle, vehicle_refs)):
        criteria.append({"criterion": key, "value": value,
                         "status": "UNKNOWN" if value is None else "OBSERVED",
                         "evidence_refs": refs, "detail": "Branch input; required only where the screenshot tree uses it.",
                         "knockout": False})
    used_refs = {ref for item in criteria for ref in item["evidence_refs"]}
    demo_nodes = [*profile_nodes, *report_nodes, *age_nodes, *fico_nodes,
                  *graph.related("EntityInvolvement", "loan_id", loan.id),
                  *([response] if response is not None else [])]
    demo_evidence = {item.id: item for node in demo_nodes for item in node.evidence if item.id in used_refs}
    return {
        "version": VERSION, "mode": "TEST", "label": label,
        "simulation_only": True, "authorization": "NOT_ISSUED", "human_review_required": True,
        "final_action": "NO_OPERATIONAL_ACTION", "selection_basis": SELECTION_BASIS,
        "selected_report_id": report.id if report else None,
        "report_selection_status": "FIRST_ACCOUNT_MATCH" if report else "NO_ACCOUNT_MATCH",
        "report_date_checked": False, "report_completion_checked": False,
        "freshness_asserted": False,
        "reference_date": as_of.isoformat() if as_of else None,
        "reference_date_basis": "EXPLICIT_APPLICATION_AS_OF_DATE" if as_of else "UNAVAILABLE",
        "age_reference_date": age_reference.isoformat() if age_reference else None,
        "age_reference_date_basis": age_reference_basis,
        "candidate_route": route, "branch": branch, "rationale": rationale,
        "criteria": criteria, "normalized_facts": facts,
        "age_aggregation": {"basis": AGE_AGGREGATION_BASIS, "account_count": len(party_roles)},
        "age_account_audit": age_audit,
        "fico_aggregation": {"basis": FICO_AGGREGATION_BASIS, "account_count": len(party_roles)},
        "fico_account_audit": fico_audit,
        "matched_reasons": _matched_reasons(criteria),
        # Retain DOB provenance, not another copy of sensitive source values.
        "source_evidence": [{**item.model_dump(), **({"value": None} if item.id in dob_refs else {})}
                    for item in demo_evidence.values()],
        "missing_inputs": sorted(set(missing)), "conflicts": conflicts,
        "strict_trace_unchanged": True,
    }


def bounded_demo_context(data: Any, *, mode: str, allowed_refs: set[str]) -> dict[str, Any] | None:
    """Allowlist normalized facts; never send raw source fields or free text."""
    if (mode != "TEST" or not isinstance(data, dict) or data.get("version") != VERSION
            or data.get("mode") != "TEST" or data.get("simulation_only") is not True
            or data.get("authorization") != "NOT_ISSUED" or data.get("human_review_required") is not True
            or data.get("selection_basis") != SELECTION_BASIS
            or data.get("label") not in {"Approve", "Decline", "Refer"}):
        return None
    keys = {"public_record_code", "ebitda_usd", "bankruptcy_clear", "age_years", "fico", "sbss",
            "ownership_years", "requested_amount_usd", "naics", "vehicle_only"}
    facts = data.get("normalized_facts")
    facts = facts if isinstance(facts, dict) else {}
    normalized: dict[str, Any] = {}
    code = _number(facts.get("public_record_code"))
    if code in {Decimal(0), Decimal(1), Decimal(2), Decimal(3)}:
        normalized["public_record_code"] = str(code)
    fico = _number(facts.get("fico"))
    if fico is not None and 300 <= fico <= 850 and len(str(fico)) <= 100:
        normalized["fico"] = str(fico)
    for key in ("sbss", "ownership_years", "ebitda_usd", "requested_amount_usd"):
        number = _number(facts.get(key))
        if number is not None and len(str(number)) <= 100:
            normalized[key] = str(number)
    if type(facts.get("age_years")) is int and 0 <= facts["age_years"] <= 150:
        normalized["age_years"] = facts["age_years"]
    for key in ("bankruptcy_clear", "vehicle_only"):
        if type(facts.get(key)) is bool:
            normalized[key] = facts[key]
    naics = facts.get("naics")
    if isinstance(naics, str) and re.fullmatch(r"\d{2,6}", naics):
        normalized["naics"] = naics
    checks = []
    raw_criteria = data.get("criteria")
    for item in raw_criteria[:10] if isinstance(raw_criteria, list) else []:
        if not isinstance(item, dict) or item.get("criterion") not in keys:
            continue
        key, status = item["criterion"], item.get("status")
        if status not in {"PASS", "FAIL", "UNKNOWN", "OBSERVED"}:
            continue
        # A stale saved status cannot contradict the normalized value sent to
        # the model (e.g. SBSS 177 must never be described as below 160).
        value = normalized.get(key)
        knockout = (key in {"bankruptcy_clear", "age_years", "fico", "sbss"}
                    and item.get("knockout", True) is not False)
        if not knockout:
            status = "OBSERVED" if value is not None else "UNKNOWN"
        elif knockout and (status != "UNKNOWN" or (key == "age_years" and value is not None and value < 18)):
            clear = None
            if value is not None:
                number = _number(value)
                clear = (value if key == "bankruptcy_clear" else
                         number >= {"age_years": 18,
                                    "fico": 650, "sbss": 160}[key] if number is not None else None)
            status = "UNKNOWN" if clear is None else "PASS" if clear else "FAIL"
        refs = item.get("evidence_refs")
        checks.append({"criterion": key, "status": status, "value": normalized.get(key), "knockout": knockout,
                       "evidence_refs": [ref for ref in refs[:40] if isinstance(ref, str) and ref in allowed_refs]
                       if isinstance(refs, list) else []})
    branch = data.get("branch")
    branch = branch if isinstance(branch, str) and re.fullmatch(r"[A-Z0-9_]{1,128}", branch) else None
    known_missing = (keys - {"public_record_code", "ebitda_usd"}) | {
        "explicit_unique_borrower_account_relationship", "naics_or_listed_high_risk_prefixes"}
    missing = data.get("missing_inputs")
    reference = iso_date(data.get("reference_date"))
    age_reference = iso_date(data.get("age_reference_date"))
    age_reference_basis = data.get("age_reference_date_basis")
    age_context = {}
    if age_reference and age_reference_basis in {"EXPLICIT_APPLICATION_AS_OF_DATE", "CURRENT_UTC_DATE_FOR_AGE"}:
        age_context = {"age_reference_date": age_reference.isoformat(),
                       "age_reference_date_basis": age_reference_basis}
    # Legacy snapshots have no aggregation metadata: preserve their facts but
    # do not guess an account count or reinterpret a saved single-party score.
    aggregate = data.get("fico_aggregation")
    aggregate_context = {}
    if (isinstance(aggregate, dict) and aggregate.get("basis") == FICO_AGGREGATION_BASIS
            and type(aggregate.get("account_count")) is int
            and 0 <= aggregate["account_count"] <= 1_000_000):
        aggregate_context["fico_aggregation"] = {
            "basis": FICO_AGGREGATION_BASIS, "account_count": aggregate["account_count"]}
    age_aggregate = data.get("age_aggregation")
    if (isinstance(age_aggregate, dict) and age_aggregate.get("basis") == AGE_AGGREGATION_BASIS
            and type(age_aggregate.get("account_count")) is int
            and 0 <= age_aggregate["account_count"] <= 1_000_000):
        aggregate_context["age_aggregation"] = {
            "basis": AGE_AGGREGATION_BASIS, "account_count": age_aggregate["account_count"]}
    return {
        "version": VERSION, "label": data["label"], "simulation_only": True,
        "authorization": "NOT_ISSUED", "human_review_required": True,
        "selection_basis": SELECTION_BASIS, "report_date_checked": False,
        "report_completion_checked": False, "freshness_asserted": False,
        "reference_date": reference.isoformat() if reference else None,
        "reference_date_basis": "EXPLICIT_APPLICATION_AS_OF_DATE" if reference else "UNAVAILABLE",
        "candidate_route": data.get("candidate_route") if data.get("candidate_route") in {"SBA", "CONVENTIONAL"} else None,
        "branch": branch, "normalized_facts": normalized, "criteria": checks,
        **age_context,
        **aggregate_context,
        "matched_reasons": _matched_reasons(checks),
        "missing_inputs": [key for key in missing[:20] if isinstance(key, str) and key in known_missing]
        if isinstance(missing, list) else [],
        "conflicts": ["REQUEST_THRESHOLD_100M_VS_100K"] if branch == "REQUEST_THRESHOLD_CONFLICT" else [],
        "rationale": "Separate TEST simulation; explain supplied check statuses and normalized facts only.",
    }