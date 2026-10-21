"""Page-cited draft policy routing, never automated lending approval or denial.

Canonical request fields must be explicitly supplied. No guessed role mappings,
row-order joins, currency scaling, or financial ratio formulas are permitted.
"""

import hashlib
import json
from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import PolicyEvaluation, PolicyProfileResult, PolicyRuleResult, Session
from .datasets import mtb_query_ids

CONFIG_PATH = Path(__file__).with_name("policy_config.json")
PRIORITY = {"COMPLIANCE_HOLD": 0, "PRODUCT_INELIGIBLE": 1, "FULL_UNDERWRITING": 2,
            "MANUAL_REVIEW": 3, "ELIGIBLE_FOR_SCORING": 4}

# Only clear bureau fields are mapped. Account is not a loan ID or borrower role.
ALIASES = {"credit_freeze": "nFUSE__Credit_Report_Freeze__c", "ofac_alert": "nFUSE__OFAC_Alert_Indicator__c",
           "bankruptcies": "nFUSE__Number_Of_Bankruptcies__c", "past_due": "nFUSE__Total_Past_Due__c",
           "credit_score": "nFUSE__Credit_Score__c", "sbss": "SCOC__LCResponse__r.SCOC__SBSS7Score__c"}
FIELDS = {"account_id", "loan_id", "borrower_role", "product", "requested_path", "requested_amount", "new_money",
          "tier_i", "tier_ii", "is_nfp", "is_startup", "is_investment_re", "secured", "collateral_taken",
          "ofac_disposition", "ofac_alert", "credit_freeze", "bankruptcies", "past_due", "sbss", "credit_score",
          "is_increase", "account_opened", "as_of_date", "acceptance_letter", "loan_purpose", "sba_memo", "cam",
          "conversion_from", "conversion_to", "previously_secured", "is_reduction", "scores_as_sba"}


def decimal_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        # No suffixes, percent signs, currency symbols or locale scaling are inferred.
        number = Decimal(str(value).strip())
        return number if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def boolean_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def six_month_anniversary(opened: date) -> date:
    month = opened.month - 1 + 6
    year, month = opened.year + month // 12, month % 12 + 1
    return date(year, month, min(opened.day, monthrange(year, month)[1]))


def evaluate_profile(profile: dict, config: dict, profile_id: str, evidence: list[str]) -> tuple[PolicyProfileResult, list[PolicyRuleResult]]:
    results: list[PolicyRuleResult] = []
    missing = []

    def check(rule_id: str, condition: bool | None, route: str, page: int, detail: str) -> None:
        results.append(PolicyRuleResult(rule_id=rule_id, status="unknown" if condition is None else "triggered" if condition else "not_triggered",
                                        route=route if condition else "MANUAL_REVIEW" if condition is None else None,
                                        page=page, detail=f"{profile_id}: {detail}", evidence=evidence))
        if condition is None:
            missing.append(rule_id + ": " + detail)

    disposition = str(profile.get("ofac_disposition", "")).strip().casefold()
    alert = boolean_value(profile.get("ofac_alert"))
    check("MTB-OFAC-CONFIRMED", True if disposition == "confirmed_match" else False if disposition == "cleared" else None,
          "COMPLIANCE_HOLD", 2, "Confirmed OFAC match requires compliance escalation; alert alone is not a confirmed match.")
    if alert is True and disposition not in {"confirmed_match", "cleared"}:
        check("MTB-OFAC-ALERT", True, "MANUAL_REVIEW", 2, "Unverified OFAC alert requires compliance validation, not automatic denial.")
    freeze = boolean_value(profile.get("credit_freeze"))
    check("MTB-CREDIT-FREEZE", freeze, "MANUAL_REVIEW", 2, "Frozen or unavailable bureau information prevents automatic clearance.")
    identifiers_valid = all(profile.get(key) is not None and str(profile[key]).strip() for key in ("account_id", "loan_id")) and profile.get("borrower_role") in {"borrower", "guarantor"}
    check("MTB-IDENTITY", not identifiers_valid, "MANUAL_REVIEW", 1, "Account, request/loan ID and explicit borrower/guarantor role are required; rows are never joined by position.")
    if not identifiers_valid:
        missing.append("MTB-IDENTITY: validated account_id, loan_id and borrower_role mappings.")
    path = str(profile.get("requested_path", "")).strip().casefold()
    nfp = boolean_value(profile.get("is_nfp"))
    if path == "quick_app":
        check("MTB-QUICK-NFP", nfp, "FULL_UNDERWRITING", 1, "NFP entities do not qualify for Quick App; this is not a loan denial.")
    elif not path:
        check("MTB-REQUEST-PATH", None, "MANUAL_REVIEW", 1, "Requested underwriting path is missing.")
    product = str(profile.get("product", "")).strip().casefold()
    known = {"express_baloc", "conventional_baloc", "bil", "sba_bil", "term_loan", "sba_baloc"}
    check("MTB-PRODUCT-MAPPING", None if product not in known else False, "MANUAL_REVIEW", 1, "An explicit supported product mapping is required.")
    units = config.get("amount_unit_confirmed") is True
    money = {key: decimal_value(profile.get(key)) if units else None for key in ("requested_amount", "new_money", "tier_i", "tier_ii")}
    if not units:
        check("MTB-CURRENCY-UNITS", None, "MANUAL_REVIEW", 1, "Confirm source currency units before applying $100M/$250M/$25M boundaries.")
    if path == "quick_app":
        new, tier1 = money["new_money"], money["tier_i"]
        check("MTB-QUICK-NEW-MONEY", new > Decimal(config["quick_app_new_money_limit"]) if new is not None else None,
              "FULL_UNDERWRITING", 1, "Quick App total new request must be <= $100M; amounts above require full underwriting.")
        check("MTB-QUICK-TIER-I", tier1 > Decimal(config["quick_app_tier_i_limit"]) if tier1 is not None else None,
              "FULL_UNDERWRITING", 1, "Quick App Tier I group credit must be <= $250M.")
    if config.get("effective_tgc_formula_confirmed") is True:
        tier1, tier2 = money["tier_i"], money["tier_ii"]
        effective = max(tier1, tier2) if tier1 is not None and tier2 is not None else None
        check("MTB-TGC-NCINO", effective > Decimal(config["quick_app_tier_i_limit"]) if effective is not None else None,
              "FULL_UNDERWRITING", 1, "Confirmed effective TGC above $250M routes through nCino; exposures are not summed across unvalidated records.")
    else:
        check("MTB-TGC-FORMULA", None, "MANUAL_REVIEW", 4, "Policy owners must confirm max(Tier I, Tier II) and guarantor aggregation scope.")
    amount = money["requested_amount"]
    secured = boolean_value(profile.get("secured"))
    if product == "conventional_baloc":
        check("MTB-CONVENTIONAL-SECURITY", secured is False if secured is not None else None,
              "PRODUCT_INELIGIBLE", 2, "Conventional BALOC must be secured.")
        check("MTB-CONVENTIONAL-AMOUNT", amount <= Decimal(config["quick_app_new_money_limit"]) if amount is not None else None,
              "MANUAL_REVIEW", 2, "Conventional BALOC is described as above $100M; a smaller/reduced line needs path and collateral review.")
    if product in {"express_baloc", "bil"}:
        check("MTB-EXPRESS-SECURITY", secured if secured is not None else None, "PRODUCT_INELIGIBLE", 2,
              "Express BALOC/BIL is unsecured; collateralized BIL may need conversion to term loan.")
        check("MTB-EXPRESS-AMOUNT", amount > Decimal(config["quick_app_new_money_limit"]) if amount is not None else None,
              "MANUAL_REVIEW" if product == "express_baloc" else "PRODUCT_INELIGIBLE", 2,
              "Express/BIL limit is <= $100M; above-limit Express increases require policy-confirmed exception review.")
    if product == "bil":
        letter = boolean_value(profile.get("acceptance_letter"))
        collateral = boolean_value(profile.get("collateral_taken"))
        check("MTB-BIL-LETTER", not letter if letter is not None else None, "MANUAL_REVIEW", 2, "BIL acceptance letter is required.")
        check("MTB-BIL-COLLATERAL", collateral, "PRODUCT_INELIGIBLE", 2, "Collateral taken for BIL requires term-loan path review as applicable.")
    if product == "sba_bil":
        purpose = profile.get("loan_purpose")
        check("MTB-SBA-BIL-PURPOSE", purpose != "permanent_working_capital" if purpose else None,
              "PRODUCT_INELIGIBLE", 2, "SBA BIL is for permanent working capital; other SBA terms, flat fee, no PPP, fixed-rate P&I need confirmation.")
        for field in ("sba_memo", "cam"):
            value = boolean_value(profile.get(field))
            check("MTB-SBA-" + field.upper(), not value if value is not None else None, "MANUAL_REVIEW", 2, f"Required SBA documentation: {field}.")
    baloc = product.endswith("baloc")
    if baloc:
        for flag in ("is_startup", "is_investment_re"):
            special = boolean_value(profile.get(flag))
            condition = False if special is False else amount > Decimal(config["baloc_special_cap"]) if special is True and amount is not None else None
            check("MTB-CAP-" + flag.upper(), condition, "PRODUCT_INELIGIBLE", 2, f"{flag}: BALOC revolving cap $25M; not a general term-loan cap.")
        increase = boolean_value(profile.get("is_increase"))
        if increase is not False:
            try:
                opened = date.fromisoformat(str(profile.get("account_opened", "")))
                as_of = date.fromisoformat(str(profile.get("as_of_date", "")))
                early = as_of < six_month_anniversary(opened) if as_of >= opened else None
            except ValueError:
                early = None
            check("MTB-SIX-MONTH-INCREASE", early if increase is True else None, "MANUAL_REVIEW", 2,
                  "Line increases before six calendar months require policy exception review; an explicit evaluation date is required.")
        source, target = profile.get("conversion_from"), profile.get("conversion_to")
        if source or target:
            check("MTB-BALOC-CONVERSION", {source, target} == {"sba_baloc", "conventional_baloc"}, "PRODUCT_INELIGIBLE", 2,
                  "SBA/conventional BALOC conversion requires a new line, not an in-place change.")
        if boolean_value(profile.get("is_reduction")) is True and boolean_value(profile.get("previously_secured")) is True:
            check("MTB-RETAIN-COLLATERAL", secured is False if secured is not None else None, "MANUAL_REVIEW", 2,
                  "Previously secured lines retain collateral when reduced below the stated boundary.")
        if boolean_value(profile.get("scores_as_sba")) is True:
            check("MTB-SBA-DOUBLING", True, "MANUAL_REVIEW", 2, "Requested-line doubling for SBA consideration requires business policy validation; no automatic amount change.")
    if product == "term_loan":
        check("MTB-TERM-STIPULATIONS", None, "MANUAL_REVIEW", 2,
              "Pricing spread, COF, fixed-rate language, PPP stipulations and collateral applicability need approved mappings; under-$20M equipment note is not automatic approval.")
    for field, parameter, page in (("bankruptcies", "multiple_bankruptcies_threshold", 2), ("past_due", "high_past_due_threshold", 3)):
        value, threshold = decimal_value(profile.get(field)), decimal_value(config.get(parameter))
        if field == "past_due" and not units:
            value = None
        check("MTB-RISK-" + field.upper(), value >= threshold if value is not None and threshold is not None else None,
              "MANUAL_REVIEW", page, f"{field}: approved severity threshold and valid source values are required; no invented cutoff.")
    for field, parameter in (("sbss", "minimum_sbss"), ("credit_score", "minimum_credit_score")):
        value, minimum = decimal_value(profile.get(field)), decimal_value(config.get(parameter))
        check("MTB-SCORE-" + field.upper(), value < minimum if value is not None and minimum is not None else None,
              "MANUAL_REVIEW", 3, f"{field}: approved scoring minimum and valid source value are required.")
    check("MTB-POLICY-SIGNOFF", True, "MANUAL_REVIEW", 4, "Design proposal is not approved credit policy; authorized policy and lender signoff required.")
    triggered = [r.route for r in results if r.status == "triggered" and r.route]
    # Keep known routing reasons visible, while missing information prevents clearance.
    route = min(triggered, key=lambda route: PRIORITY[route]) if triggered else "MANUAL_REVIEW"
    if any(r.route == "COMPLIANCE_HOLD" and r.status == "triggered" for r in results):
        route = "COMPLIANCE_HOLD"
    return PolicyProfileResult(profile_id=profile_id, route=route, missing_information=missing, evidence=evidence), results


def evaluate_session(session: Session) -> PolicyEvaluation | None:
    documents = [doc for doc in session.documents if mtb_query_ids(doc.name, doc.metadata)]
    if not documents:
        return None
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    evaluation = PolicyEvaluation(policy_version=config["version"], source_document=config["source_document"],
                                  source_sha256=config["source_sha256"], recommended_route="MANUAL_REVIEW",
                                  limitations=["Draft design proposal, not approved credit policy (p.4). No final APPROVE/DENY is emitted.",
                                               "No unvalidated joins, borrower/guarantor role inference, or Query 2 indicator pivots.",
                                               "Amount units: actual USD dollars; effective TGC: max(Tier I, Tier II), user-confirmed 2026-10-20. Source exposures and relationship scope must still be validated.",
                                               "Risk thresholds and missing field mappings require business confirmation; confirmed units do not supply missing amounts.",
                                               "Financial ratios, bureau recency and exception authorities are not inferred from absent inputs."])
    queries = set().union(*(mtb_query_ids(doc.name, doc.metadata) for doc in documents))
    evaluation.missing_information.extend(f"Required workbook: MTB Query {query}" for query in sorted({1, 2, 4} - queries))
    for query in sorted(queries):
        if sum(query in mtb_query_ids(doc.name, doc.metadata) for doc in documents) > 1:
            evaluation.missing_information.append(f"Multiple workbooks identify as MTB Query {query}; reconcile duplicates before any joins.")
    source = Path(__file__).parent / "data" / config["source_document"]
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != config["source_sha256"]:
        evaluation.missing_information.append("Source PDF is missing or differs from the reviewed version; revalidate policy mapping.")
        evaluation.limitations.append("Source integrity mismatch: configured draft checks must not be treated as verified policy.")
    for doc in documents:
        records = doc.metadata.get("records", [])
        if not records:
            evaluation.missing_information.append(f"{doc.name}: no data records; provide populated borrower/request data.")
        for record in records:
            values, sources = record["values"], record["sources"]
            profile = {field: values[field] for field in FIELDS if field in values}
            for canonical, original in ALIASES.items():
                if canonical not in profile and original in values:
                    profile[canonical] = values[original]
            evidence = [f"{doc.name} / {sources.get(key, 'unmapped cell')}: {key}={value}" for key, value in values.items()]
            profile_id = f"{doc.name}/{record['sheet']}/row-{record['row']}"
            outcome, results = evaluate_profile(profile, config, profile_id, evidence)
            evaluation.profiles.append(outcome)
            evaluation.rule_results.extend(results)
            evaluation.missing_information.extend(f"{profile_id}: {item}" for item in outcome.missing_information)
    evaluation.rule_results.append(PolicyRuleResult(rule_id="MTB-DRAFT-POLICY", status="triggered", route="MANUAL_REVIEW", page=4,
                                                    detail="Policy-owner validation is mandatory; hold/routing outcomes are not lending denial.",
                                                    evidence=[f"{config['source_document']} pp.1–4; SHA-256 {config['source_sha256']}"]))
    if not evaluation.profiles:
        evaluation.rule_results.append(PolicyRuleResult(rule_id="MTB-NO-RECORDS", status="unknown", route="MANUAL_REVIEW", page=1,
                                                        detail="No populated records available for schema validation, identity joins, or underwriting checks.",
                                                        evidence=[f"{doc.name}: {doc.metadata.get('data_row_count', 0)} records" for doc in documents]))
    if evaluation.profiles:
        evaluation.recommended_route = min((p.route for p in evaluation.profiles), key=lambda route: PRIORITY[route])
    evaluation.missing_information = sorted(set(evaluation.missing_information))
    return evaluation