"""Isolated screenshot simulation v1; never authoritative underwriting policy.

Graph insertion order is source order. The first account-linked report is used
without checking report date/completion; poisoned fields remain unknown.
FICO uses Query 1 nested credit-report scores (then Query 4) of every linked
non-business account (guarantors) and skips blank scores within each account. No raw
characteristics are pivoted, no EBIT/net income is substituted for EBITDA, and
no graph is modified. Age, EBITDA and public records are non-gating diagnostics.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .entity_graph import EntityGraph, EntityNode, iso_date, scalar_id
from .expanded_spec import REASONS
from .underwriting_models import USD_100K, USD_100M, exact_decimal

VERSION = "screenshot-test-demo-v1"
SELECTION_BASIS = "FIRST_ACCOUNT_MATCH_FOR_TEST_DEMO"
FICO_AGGREGATION_BASIS = "LINKED_INDIVIDUAL_ACCOUNT_AVERAGE"
AGE_AGGREGATION_BASIS = "LINKED_BORROWER_GUARANTOR_MIN_COMPLETED_CALENDAR_AGE"
OWNERSHIP_FALLBACK_BASIS = "ASSUMED_LT_ONE_YEAR_WHEN_NOT_PROVIDED"
OWNERSHIP_EXPLICIT_BASIS = "EXPLICIT_NONNEGATIVE_OWNERSHIP_YEARS"
OWNERSHIP_UNKNOWN_BASIS = "UNAVAILABLE_OR_INVALID"
VEHICLE_FALLBACK_BASIS = "ASSUMED_FALSE_WHEN_NOT_PROVIDED"
VEHICLE_EXPLICIT_BASIS = "EXPLICIT_VEHICLE_ONLY_FLAG"
VEHICLE_UNKNOWN_BASIS = "INVALID_OR_CONFLICTING"
BANKRUPTCY_FALLBACK_BASIS = "ASSUMED_ZERO_WHEN_NOT_PROVIDED"
BANKRUPTCY_EXPLICIT_BASIS = "EXPLICIT_BANKRUPTCY_COUNT"
BANKRUPTCY_UNKNOWN_BASIS = "INVALID_OR_CONFLICTING"
BORROWER_ROLE_BASIS = "EXPLICIT_BORROWER_ROLE"
BORROWER_BUSINESS_TYPE_BASIS = "BUSINESS_ACCOUNT_LINKED_BY_LOAN_ID"
BORROWER_ROLE_BUSINESS_BASIS = "BUSINESS_ACCOUNT_AMONG_BORROWER_ROLES"
SBSS_SINGLE_BASIS = "SINGLE_SUPPLIED_SBSS"
SBSS_QUERY1_BASIS = "QUERY1_BORROWER_ROW_SBSS"
SBSS_BORROWER_ROW_BASIS = "BORROWER_ROW_SBSS"
SBSS_LOWEST_BASIS = "LOWEST_OF_MULTIPLE_SBSS"
NAICS_MATCH_BASIS = "SUPPLIED_HIGH_RISK_PREFIX_LIST_ONLY"
NAICS_MAX_LENGTH = 128


def _demo_naics(value: Any) -> str | None:
    """A supplied category, not syntax/catalog validation or a USD amount."""
    if type(value) is str:
        return value.strip()[:NAICS_MAX_LENGTH] or None
    if type(value) not in {int, float, Decimal}:
        return None
    if type(value) is int and not 0 <= value < 10 ** NAICS_MAX_LENGTH:
        return None
    number = Decimal(str(value))
    if (not number.is_finite() or number < 0 or number >= 10 ** NAICS_MAX_LENGTH
            or number != number.to_integral_value()):
        return None
    return str(int(number))


def _matched_reasons(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only three knockout demo predicates; the expanded registry stays independent."""
    catalog = {code: label for code, label, *_ in REASONS}
    mappings = {
        "bankruptcy_clear": (catalog["BANKRUPTCY"], "SUPPLIED_DECLINE_REASON_CATALOG"),
        "fico": ("Average personal FICO below 650", "SUPPLIED_STRATEGY"),
        "sbss": ("SBSS below 160", "SUPPLIED_STRATEGY"),
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
    borrower_basis = BORROWER_ROLE_BASIS if borrower else None
    borrower_link_refs: set[str] = set()
    if borrower is None:
        # TEST rule: the borrower is the business entity on the loan. When the
        # Borrower role is absent or shared (e.g. an Individual co-borrower),
        # pick the single business account, first among Borrower-role accounts
        # and then among every account linked to this loan ID.
        linked: dict[str, set[str]] = {}
        for involvement in graph.related("EntityInvolvement", "loan_id", loan.id):
            if involvement.poisoned & {"loan_id", "account_id"}:
                continue
            account_id = scalar_id(involvement.fields.get("account_id"))
            if account_id and account_id in graph.table("Account"):
                linked.setdefault(account_id, set()).update(
                    ref for field in ("loan_id", "account_id", "role")
                    for ref in involvement.field_refs.get(field, []))
        for account_id in borrowers:
            linked.setdefault(account_id, set())

        def account_kind(account_id: str) -> str | None:
            candidate = graph.table("Account")[account_id]
            if "account_type" in candidate.poisoned:
                return None
            kind = str(candidate.fields.get("account_type") or "").strip().casefold()
            return "business" if kind == "business" else None if kind in {"", "individual"} else "entity"

        for pool, kinds, basis in ((borrowers, {"business"}, BORROWER_ROLE_BUSINESS_BASIS),
                                   (borrowers, {"business", "entity"}, BORROWER_ROLE_BUSINESS_BASIS),
                                   (set(linked), {"business"}, BORROWER_BUSINESS_TYPE_BASIS),
                                   (set(linked), {"business", "entity"}, BORROWER_BUSINESS_TYPE_BASIS)):
            matches = [account_id for account_id in pool if account_kind(account_id) in kinds]
            if len(matches) == 1:
                borrower, borrower_basis = matches[0], basis
                borrower_link_refs = linked.get(borrower, set()) | set(
                    graph.table("Account")[borrower].field_refs.get("account_type", []))
                borrowers = {borrower}
                break
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
    bankruptcy_basis = BANKRUPTCY_EXPLICIT_BASIS if count is not None else BANKRUPTCY_UNKNOWN_BASIS
    # TEST default: an absent or blank bankruptcy count means 0 (no bankruptcy),
    # even when no borrower report is found. Supplied invalid text or
    # conflicting values stay UNKNOWN.
    if (count is None and (count_raw is None or (isinstance(count_raw, str) and not count_raw.strip()))
            and not any("bankruptcy_count" in node.poisoned for node in report_nodes)):
        count, bankruptcy_basis = Decimal(0), BANKRUPTCY_FALLBACK_BASIS
    discharge_raw, discharge_refs = read("bankruptcy_discharge_date", report_nodes)
    discharge = iso_date(discharge_raw)
    bankruptcy_clear = True if count == 0 else None
    if count is not None and count > 0 and discharge and as_of and discharge <= as_of:
        bankruptcy_clear = as_of.year - discharge.year >= 5 and as_of >= _anniversary(discharge, 5)
    add("bankruptcy_clear", bankruptcy_clear, bankruptcy_clear,
        sorted(set(count_refs + discharge_refs + date_refs)),
        "Bankruptcy count not provided; TEST default treats it as 0. No source evidence is invented."
        if bankruptcy_basis == BANKRUPTCY_FALLBACK_BASIS else
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
        party_refs[account_id].update(borrower_link_refs)
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
                          "status": "UNKNOWN" if person_age is None else "OBSERVED",
                          "knockout": False,
                          "evidence_refs": sorted(refs)})
    age_complete = bool(ages) and len(ages) == len(party_roles)
    underage = any(value < 18 for value in ages)
    age = min(ages) if ages and (age_complete or underage) else None
    add("age_years", age, None, sorted(age_refs),
        "Observed diagnostic only; DOB/age is excluded from the TEST demo decision flow. "
        "Each linked borrower/guarantor age uses only that Account DOB and explicit loan as-of date, "
        "or the recorded current UTC date for age only when the loan as-of date is absent. "
        "Completed calendar ages (0–150) are never averaged; the scalar is the minimum when all are valid, "
        "or the minimum known underage age. Otherwise any unresolved party is UNKNOWN. "
        "Invalid/conflicting loan dates stay UNKNOWN; report dates are never used. No clearance implied.",
        knockout=False)
    criteria[-1]["status"] = "OBSERVED" if age is not None else "UNKNOWN"

    fico_audit: list[dict[str, Any]] = []
    fico_nodes: list[EntityNode] = []
    fico_refs: set[str] = set()
    scores: list[Decimal] = []

    def has_fico_value(candidate: EntityNode) -> bool:
        explicit = ("personal_fico" in candidate.fields or "personal_fico" in candidate.field_refs
                    or "personal_fico" in candidate.poisoned)
        field = "personal_fico" if explicit else "fico"
        raw = candidate.fields.get(field)
        return (bool(candidate.poisoned & {"account_id", field})
                or (raw is not None and not (isinstance(raw, str) and not raw.strip())))

    # FICO parties: the role-validated links above, plus every non-business
    # account on an Entity Involvement row that explicitly carries this loan
    # ID (Individual or untyped guarantors), whatever Borrower_Type label they
    # carry, so connect them to the business loan by loan ID alone.
    fico_roles = {account_id: set(roles) for account_id, roles in party_roles.items()}
    fico_party_refs = {account_id: set(refs) for account_id, refs in party_refs.items()}
    for involvement in graph.related("EntityInvolvement", "loan_id", loan.id):
        if involvement.poisoned & {"loan_id", "account_id"}:
            continue
        account_id = scalar_id(involvement.fields.get("account_id"))
        if not account_id or account_id in fico_roles:
            continue
        person = graph.table("Account").get(account_id)
        if person is not None and "account_type" in person.poisoned:
            continue
        raw_type = person.fields.get("account_type") if person is not None else None
        kind = str(raw_type or "").strip().casefold()
        # Business accounts are listed in the audit (as excluded) so every
        # account linked to the loan is visible, but never scored.
        business = kind == "business" or (account_id == borrower and kind != "individual")
        fico_roles[account_id] = {"borrower" if account_id == borrower else "linked"} if business else {"guarantor"}
        fico_party_refs[account_id] = {
            ref for field in ("loan_id", "account_id", "role")
            for ref in involvement.field_refs.get(field, [])}

    for account_id, roles in fico_roles.items():
        refs = set(fico_party_refs[account_id])
        person = graph.table("Account").get(account_id)
        account_type = None
        if person is not None:
            fico_nodes.append(person)
            raw_type, type_refs = read("account_type", [person])
            refs.update(type_refs)
            account_type = str(raw_type).strip() if raw_type is not None and str(raw_type).strip() else None
        conflicting = person is not None and "account_type" in person.poisoned
        kind = (account_type or "").casefold()
        if conflicting or kind == "business" or (account_id == borrower and kind != "individual"):
            # FICO applies to every linked account except the business
            # borrower and Business-typed accounts, which are excluded from
            # both sum and count.
            fico_refs.update(refs)
            fico_audit.append({"account_id": account_id, "party_roles": sorted(roles),
                               "account_type": account_type, "selected_report_id": None,
                               "report_status": "NOT_EVALUATED",
                               "score_status": ("CONFLICTING_ACCOUNT_TYPE" if conflicting
                                                else "EXCLUDED_NON_INDIVIDUAL_ACCOUNT"),
                               "score": None, "evidence_refs": sorted(refs)})
            continue
        candidates = graph.related("CreditReport", "account_id", account_id)
        selected = next((candidate for candidate in candidates if has_fico_value(candidate)),
                        candidates[0] if candidates else None)
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
            raw, _ = read(field, [selected])
            if selected.poisoned & relevant:
                status = "CONFLICTING_REPORT"
            elif raw is None or (isinstance(raw, str) and not raw.strip()):
                status = "MISSING_PERSONAL_FICO"
            elif not typed:
                status = "UNTYPED_PERSONAL_FICO"
            else:
                number = _number(raw)
                if number is not None and 0 <= number <= 850:
                    score, status = number, "VALID_PERSONAL_FICO"
                    scores.append(number)
                else:
                    status = "MISSING_OR_INVALID_PERSONAL_FICO"
        if status in {"MISSING_PERSONAL_FICO", "NO_ACCOUNT_MATCH"}:
            # Query 1 supplied no score for this Individual: fall back to the
            # first non-blank Query 4 root credit score for the same account,
            # row by row in source order. Repeated report IDs with different
            # scores across rows are not a conflict; the first row wins.
            fallback, fallback_ref, raw = None, None, None
            for candidate in candidates:
                if "account_id" in candidate.poisoned:
                    continue
                by_id = {item.id: item for item in candidate.evidence}
                for ref in candidate.field_refs.get("query4_fico", []):
                    item = by_id.get(ref)
                    value = item.value if item is not None else None
                    if value is None and "query4_fico" not in candidate.poisoned:
                        value = candidate.fields.get("query4_fico")
                    if value is None or (isinstance(value, str) and not value.strip()):
                        continue
                    fallback, fallback_ref, raw = candidate, ref, value
                    break
                if fallback is not None:
                    break
            if fallback is not None:
                selected = fallback
                fico_nodes.append(fallback)
                refs.update(fallback.field_refs.get("account_id", []))
                refs.add(fallback_ref)
                number = _number(str(raw) if isinstance(raw, float) else raw)
                if number is not None and 0 <= number <= 850:
                    score, status = number, "VALID_QUERY4_FICO"
                    scores.append(number)
                else:
                    status = "MISSING_OR_INVALID_PERSONAL_FICO"
        fico_refs.update(refs)
        fico_audit.append({"account_id": account_id, "party_roles": sorted(roles),
                           "account_type": account_type,
                           "selected_report_id": selected.id if selected else None,
                           "report_status": ("QUERY4_FALLBACK_ACCOUNT_MATCH" if status == "VALID_QUERY4_FICO"
                                             else "FIRST_NONBLANK_FICO_ACCOUNT_MATCH" if selected
                                             else "NO_ACCOUNT_MATCH"),
                           "score_source": ("QUERY_4" if status == "VALID_QUERY4_FICO"
                                            else "QUERY_1" if status == "VALID_PERSONAL_FICO" else None),
                           "score_status": status, "score": str(score) if score is not None else None,
                           "evidence_refs": sorted(refs)})
    invalid_scores = any(item["score_status"] not in {
        "VALID_PERSONAL_FICO", "VALID_QUERY4_FICO", "MISSING_PERSONAL_FICO", "NO_ACCOUNT_MATCH",
        "EXCLUDED_NON_INDIVIDUAL_ACCOUNT"
    } for item in fico_audit)
    fico = (sum(scores, Decimal(0)) / Decimal(len(scores))
            if scores and not invalid_scores else None)
    add("fico", fico, fico >= 650 if fico is not None else None, sorted(fico_refs),
        "Average available personal FICO across every account connected to the loan by loan ID (Individual or untyped "
        "guarantors), once per account; the business borrower and Business accounts are excluded. Each account's FICO comes from the Query 1 nested Entity Involvement "
        "credit report first; only when Query 1 has no score for that account is its Query 4 credit score used. "
        "Below 650 matches a knockout. Use the first nonblank score per account in source order. "
        "Supplied numeric scores from 0 to 850, including decimals, are used without rescaling. "
        "Missing scores are excluded from both sum and count. No available scores, or any conflicting, invalid "
        "or untyped supplied party score, makes the average UNKNOWN.")

    # Prefer the explicitly loan-scoped Query 2 scalar. A strict selected response
    # may supply a canonical score, but this module never changes response selection.
    sbss_raw, sbss_refs = read("sbss", [loan])
    if sbss_raw is None and not sbss_refs and response is not None:
        sbss_raw, sbss_refs = read("sbss", [response])
    sbss = _number(sbss_raw)
    sbss = sbss if sbss is not None and 0 <= sbss <= 300 and sbss == sbss.to_integral_value() else None
    sbss_basis = SBSS_SINGLE_BASIS if sbss is not None else None
    # Query 1 first: the SBSS on the business borrower's own Entity Involvement
    # row(s) for this loan. Only when none is found does Query 2 apply.
    q1_sbss: list[tuple[Decimal, list[str]]] = []
    for involvement in graph.related("EntityInvolvement", "loan_id", loan.id) if borrower else []:
        if (involvement.poisoned & {"loan_id", "account_id", "sbss"}
                or scalar_id(involvement.fields.get("account_id")) != borrower):
            continue
        value = _number(involvement.fields.get("sbss"))
        if value is not None and 0 <= value <= 300 and value == value.to_integral_value():
            q1_sbss.append((value, involvement.field_refs.get("sbss", [])))
    if q1_sbss:
        sbss, sbss_basis = min(value for value, _ in q1_sbss), SBSS_QUERY1_BASIS
        sbss_refs = sorted({ref for value, refs in q1_sbss if value == sbss for ref in refs})
    if sbss is None and "sbss" in loan.poisoned:
        # TEST rule for a loan carrying different SBSS values: use the value on
        # the business borrower's own source row; otherwise the lowest value.
        refs = set(loan.field_refs.get("sbss", []))
        observed = []
        for item in loan.evidence:
            value = _number(item.value) if item.id in refs else None
            if value is not None and 0 <= value <= 300 and value == value.to_integral_value():
                observed.append((value, item))
        borrower_rows = ({(item.workbook, item.sheet, item.row) for item in account.evidence}
                         if account is not None else set())
        on_borrower = [(value, item) for value, item in observed
                       if (item.workbook, item.sheet, item.row) in borrower_rows]
        if len({value for value, _ in on_borrower}) == 1:
            sbss, sbss_basis = on_borrower[0][0], SBSS_BORROWER_ROW_BASIS
            sbss_refs = sorted({item.id for _, item in on_borrower})
        elif observed:
            pool = on_borrower or observed
            sbss, sbss_basis = min(value for value, _ in pool), SBSS_LOWEST_BASIS
            sbss_refs = sorted({item.id for value, item in pool if value == sbss})
    add("sbss", sbss, sbss >= 160 if sbss is not None else None, sbss_refs,
        "SBSS below 160 matches a knockout, independently of missing report dates or other inputs."
        + (" SBSS comes from Query 1 on the business borrower's row; Query 2 is not used."
           if sbss_basis == SBSS_QUERY1_BASIS else
           " No Query 1 SBSS on the borrower's row; Query 2 SBSS linked by loan ID is used."
           if sbss_basis == SBSS_SINGLE_BASIS else
           " The loan carries different SBSS values; the value on the business borrower's row is used."
           if sbss_basis == SBSS_BORROWER_ROW_BASIS else
           " The loan carries different SBSS values and none is uniquely on the borrower's row; the lowest is used."
           if sbss_basis == SBSS_LOWEST_BASIS else ""))

    ownership_raw, ownership_refs = read("ownership_years", profile_nodes)
    ownership = _number(ownership_raw)
    ownership = ownership if ownership is not None and ownership >= 0 else None
    ownership_basis = OWNERSHIP_EXPLICIT_BASIS if ownership is not None else OWNERSHIP_UNKNOWN_BASIS
    # User-defined TEST ownership assumption, not measured corporation/formation
    # age. Supplied null/formula evidence and poisoned fields cannot use it.
    # profile_nodes deliberately excludes guarantors even without a borrower;
    # the independent mandatory borrower-identity guard still blocks approval.
    if (ownership_raw is None and not ownership_refs
            and not any("ownership_years" in node.poisoned for node in profile_nodes)):
        ownership, ownership_basis = Decimal(0), OWNERSHIP_FALLBACK_BASIS
    amount_raw, amount_refs = read("requested_amount", profile_nodes)
    amount = _number(amount_raw)
    amount = amount if amount is not None and amount >= 0 else None
    naics_raw, naics_refs = read("naics", profile_nodes)
    naics = _demo_naics(naics_raw)
    prefixes = config.get("high_risk_naics")
    high_risk = (any(naics.startswith(prefix) for prefix in prefixes)
                 if naics is not None and isinstance(prefixes, list) and prefixes
                 and all(type(prefix) is str and prefix for prefix in prefixes) else None)
    vehicle_raw, vehicle_refs = read("vehicle_only", profile_nodes)
    vehicle = vehicle_raw if type(vehicle_raw) is bool else (
        {"true": True, "false": False, "yes": True, "no": False, "1": True, "0": False,
         "y": True, "n": False}.get(str(vehicle_raw).strip().casefold()))
    vehicle_basis = VEHICLE_EXPLICIT_BASIS if vehicle is not None else VEHICLE_UNKNOWN_BASIS
    # TEST default: an absent or blank vehicle-only flag means False. Supplied
    # invalid text or conflicting values stay UNKNOWN.
    if (vehicle is None and (vehicle_raw is None or (isinstance(vehicle_raw, str) and not vehicle_raw.strip()))
            and not any("vehicle_only" in node.poisoned for node in profile_nodes)):
        vehicle, vehicle_basis = False, VEHICLE_FALLBACK_BASIS
    facts.update(ownership_years=str(ownership) if ownership is not None else None,
                 requested_amount_usd=str(amount) if amount is not None else None,
                 naics=naics, high_risk_naics=high_risk, vehicle_only=vehicle)

    label, route, branch, rationale = "Refer", None, "INPUTS_UNKNOWN", "Required diagram inputs remain unknown."
    failures = [item for item in criteria if item["knockout"] and item["status"] == "FAIL"]
    if not identity:
        missing.append("explicit_unique_borrower_account_relationship")
        rationale = "A unique explicit borrower account relationship is required."
    if failures:
        label, branch = "Decline", "KNOWN_KNOCKOUT"
        reasons = {
            "bankruptcy_clear": "Verified bankruptcy discharge is less than five calendar years ago",
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
            elif high_risk is None:
                missing.append("naics_or_listed_high_risk_prefixes")
            else:
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
            rationale = f"All three knockout checks passed; {branch} selects {route} in this simulation."
        elif missing and label != "Decline" and not conflicts:
            rationale = "Unknown diagram inputs: " + ", ".join(dict.fromkeys(missing)) + "."

    for key, value, refs in (("ownership_years", facts["ownership_years"], ownership_refs),
                             ("requested_amount_usd", facts["requested_amount_usd"], amount_refs),
                             ("naics", naics, naics_refs), ("vehicle_only", vehicle, vehicle_refs)):
        detail = "Branch input; required only where the screenshot tree uses it."
        if key == "ownership_years":
            detail = ("User-defined TEST demo ownership fallback: absent current-owner years are assumed less "
                      "than one year, represented as 0 for branch comparison; not measured corporation age, "
                      "DOB or formation age. No source evidence is invented."
                      if ownership_basis == OWNERSHIP_FALLBACK_BASIS else
                      "Current-owner years from loan, extension or borrower profile; explicit zero means less "
                      "than one year for this TEST demo. Invalid/conflicting supplied values stay UNKNOWN; "
                      "not DOB or corporation formation age.")
        elif key == "vehicle_only" and vehicle_basis == VEHICLE_FALLBACK_BASIS:
            detail = ("Vehicle-only flag not provided; TEST default treats it as false. "
                      "No source evidence is invented.")
        elif key == "naics":
            detail = ("Supplied category compared only with configured high-risk prefixes using startswith; "
                      "no valid-code catalogue or syntax gate. Trimmed canonical text is capped at 128 "
                      "characters; integral Excel numbers become digit strings. Missing/conflicting "
                      "values stay UNKNOWN; original source values remain in local evidence.")
        criteria.append({"criterion": key, "value": value,
                         "status": "UNKNOWN" if value is None else "OBSERVED",
                         "evidence_refs": refs, "detail": detail,
                         "knockout": False})
    used_refs = {ref for item in criteria for ref in item["evidence_refs"]}
    demo_nodes = [*profile_nodes, *report_nodes, *age_nodes, *fico_nodes,
                  *graph.related("EntityInvolvement", "loan_id", loan.id),
                  *([response] if response is not None else [])]
    demo_evidence = {item.id: item for node in demo_nodes for item in node.evidence if item.id in used_refs}
    # The shared graph intentionally omits float evidence for strict monetary
    # fields. Integral Excel NAICS is categorical: retain its original lexical
    # value in this local trace only, never the canonicalized digit string.
    naics_evidence = ({ref: str(naics_raw) for ref in naics_refs}
                      if type(naics_raw) is float and naics is not None else {})
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
        "naics_match_basis": NAICS_MATCH_BASIS if high_risk is not None else None,
        "ownership_years_basis": ownership_basis,
        "vehicle_only_basis": vehicle_basis,
        "bankruptcy_count_basis": bankruptcy_basis,
        "borrower_link_basis": borrower_basis,
        "sbss_basis": sbss_basis,
        "age_aggregation": {"basis": AGE_AGGREGATION_BASIS, "account_count": len(party_roles)},
        "age_account_audit": age_audit,
        "fico_aggregation": {"basis": FICO_AGGREGATION_BASIS, "account_count": sum(
            item["score_status"] != "EXCLUDED_NON_INDIVIDUAL_ACCOUNT" for item in fico_audit)},
        "fico_account_audit": fico_audit,
        "matched_reasons": _matched_reasons(criteria),
        # Retain DOB provenance, not another copy of sensitive source values.
        "source_evidence": [{**item.model_dump(),
                     **({"value": naics_evidence[item.id]} if item.id in naics_evidence else {}),
                     **({"value": None} if item.id in dob_refs else {})}
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
    if fico is not None and 0 <= fico <= 850 and len(str(fico)) <= 100:
        normalized["fico"] = str(fico)
    for key in ("sbss", "ebitda_usd", "requested_amount_usd"):
        number = _number(facts.get(key))
        if number is not None and len(str(number)) <= 100:
            normalized[key] = str(number)
    ownership = _number(facts.get("ownership_years"))
    if ownership is not None and ownership >= 0 and len(str(ownership)) <= 100:
        normalized["ownership_years"] = str(ownership)
    ownership_context = {}
    ownership_basis = data.get("ownership_years_basis")
    if ((ownership_basis == OWNERSHIP_FALLBACK_BASIS and ownership == 0)
            or (ownership_basis == OWNERSHIP_EXPLICIT_BASIS and "ownership_years" in normalized)
            or (ownership_basis == OWNERSHIP_UNKNOWN_BASIS and "ownership_years" not in normalized)):
        ownership_context["ownership_years_basis"] = ownership_basis
    vehicle_basis = data.get("vehicle_only_basis")
    if ((vehicle_basis == VEHICLE_FALLBACK_BASIS and facts.get("vehicle_only") is False)
            or (vehicle_basis == VEHICLE_EXPLICIT_BASIS and type(facts.get("vehicle_only")) is bool)
            or (vehicle_basis == VEHICLE_UNKNOWN_BASIS and facts.get("vehicle_only") is None)):
        ownership_context["vehicle_only_basis"] = vehicle_basis
    bankruptcy_basis = data.get("bankruptcy_count_basis")
    if ((bankruptcy_basis == BANKRUPTCY_FALLBACK_BASIS and facts.get("bankruptcy_clear") is True)
            or (bankruptcy_basis == BANKRUPTCY_EXPLICIT_BASIS and type(facts.get("bankruptcy_clear")) is bool)
            or (bankruptcy_basis == BANKRUPTCY_UNKNOWN_BASIS and facts.get("bankruptcy_clear") is None)):
        ownership_context["bankruptcy_count_basis"] = bankruptcy_basis
    if (data.get("sbss_basis") in {SBSS_SINGLE_BASIS, SBSS_QUERY1_BASIS, SBSS_BORROWER_ROW_BASIS, SBSS_LOWEST_BASIS}
            and _number(facts.get("sbss")) is not None):
        ownership_context["sbss_basis"] = data["sbss_basis"]
    if data.get("borrower_link_basis") in {BORROWER_ROLE_BASIS, BORROWER_BUSINESS_TYPE_BASIS,
                                           BORROWER_ROLE_BUSINESS_BASIS}:
        ownership_context["borrower_link_basis"] = data["borrower_link_basis"]
    if type(facts.get("age_years")) is int and 0 <= facts["age_years"] <= 150:
        normalized["age_years"] = facts["age_years"]
    for key in ("bankruptcy_clear", "vehicle_only"):
        if type(facts.get(key)) is bool:
            normalized[key] = facts[key]
    naics = _demo_naics(facts.get("naics"))
    # Freeform categories remain supplied observations, but never provider text.
    # Carry only digit-only categories and an allowlisted derived classification.
    if naics is not None and re.fullmatch(r"[0-9]{1,128}", naics):
        normalized["naics"] = naics
    naics_context = {}
    if (naics is not None and data.get("naics_match_basis") == NAICS_MATCH_BASIS
            and type(facts.get("high_risk_naics")) is bool):
        normalized["high_risk_naics"] = facts["high_risk_naics"]
        naics_context["naics_match_basis"] = NAICS_MATCH_BASIS
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
        knockout = (key in {"bankruptcy_clear", "fico", "sbss"}
                    and item.get("knockout", True) is not False)
        if not knockout:
            observed = naics is not None if key == "naics" else value is not None
            status = "OBSERVED" if observed else "UNKNOWN"
        elif status != "UNKNOWN":
            clear = None
            if value is not None:
                number = _number(value)
                clear = (value if key == "bankruptcy_clear" else
                         number >= {"fico": 650, "sbss": 160}[key] if number is not None else None)
            status = "UNKNOWN" if clear is None else "PASS" if clear else "FAIL"
        refs = item.get("evidence_refs")
        checks.append({"criterion": key, "status": status, "value": normalized.get(key), "knockout": knockout,
                       "evidence_refs": [ref for ref in refs[:40] if isinstance(ref, str) and ref in allowed_refs]
                       if isinstance(refs, list) else []})
    branch = data.get("branch")
    branch = branch if isinstance(branch, str) and re.fullmatch(r"[A-Z0-9_]{1,128}", branch) else None
    known_missing = (keys - {"public_record_code", "ebitda_usd", "age_years"}) | {
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
        **naics_context,
        **ownership_context,
        **age_context,
        **aggregate_context,
        "matched_reasons": _matched_reasons(checks),
        "missing_inputs": [key for key in missing[:20] if isinstance(key, str) and key in known_missing]
        if isinstance(missing, list) else [],
        "conflicts": ["REQUEST_THRESHOLD_100M_VS_100K"] if branch == "REQUEST_THRESHOLD_CONFLICT" else [],
        "rationale": "Separate TEST simulation; explain supplied check statuses and normalized facts only. "
                 "DOB/age, EBITDA and public records are nonblocking diagnostics, not missing decision inputs "
                 "or decline reasons. Historical labels are preserved; reprocess to update the decision.",
    }