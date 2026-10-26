"""Fourteen-page draft coverage: deterministic diagnostics, never adverse action.

Screenshot API names are NOT approved aliases. Only explicit canonical fields
and already-validated graph observations are consumed. Unknown is never clean.
The v4 trace remains unchanged; this separately versioned result carries the
expanded specification's registry and entire screenshot reason inventory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from .entity_graph import EntityGraph, EntityNode, iso_date
from .underwriting_models import (
    DeclineReasonDiagnostic, ExpandedRuleResult, ExpandedSpecEvaluation,
    exact_decimal,
)

# Code, screenshot label, page, disposition, explicit canonical inputs.
# Codes are internal diagnostic identifiers, NOT approved adverse-action codes.
REASONS = (
    ("ADVERSE_MEDIA", "Adverse Media/Derogatory Public Information", 7, "OUT_OF_SCOPE", ()),
    ("AFFILIATE_DEBT", "Affiliated Businesses with Excessive Debt Obligation", 7, "MANUAL_REVIEW", ("affiliate_exposure", "affiliate_leverage")),
    ("ASSET_TERM_MISMATCH", "Age of corresponding asset does not match requested length of loan", 7, "MANUAL_REVIEW", ("asset_useful_life", "loan_term")),
    ("BANKRUPTCY", "Bankruptcy", 7, "DECLINE_CANDIDATE", ("bankruptcy_count", "bankruptcy_discharge_date", "as_of_date")),
    ("MARKET_DEMAND", "Borrower Business Model Concerns - Insufficient Demonstration of Market Demand", 7, "NOT_USED", ()),
    ("MARKET_SATURATION", "Borrower Business Model Concerns - Market Saturation", 8, "NOT_USED", ()),
    ("BROKER_REFERRAL", "Broker referred applications not accepted at M&T", 8, "POLICY_REVIEW", ("broker_referred",)),
    ("EXPENSES_EXCEED_INCOME", "Business expenses exceed business income", 8, "DECLINE_CANDIDATE", ("ebitda", "financial_period_start", "financial_period_end")),
    ("CLOSED_INCOMPLETE", "Closed for Incompleteness", 8, "MANUAL_REVIEW", ("application_complete", "application_expiry_date")),
    ("COLLECTION_JUDGMENT", "Collection action or judgment", 8, "POLICY_REVIEW", ("judgment_count", "judgments_satisfied", "collection_indicator")),
    ("WORKING_CAPITAL", "Deficient working capital", 8, "MANUAL_REVIEW", ("current_assets", "current_liabilities")),
    ("DEROGATORY_BUSINESS", "Derogatory business credit", 8, "POLICY_REVIEW", ("business_payment_history", "judgment_count", "lien_count", "collection_indicator")),
    ("DEROGATORY_PERSONAL", "Derogatory personal credit", 8, "POLICY_REVIEW", ("max_delinquency_public_records",)),
    ("MARKET_CONDITIONS", "Deteriorating Financial Market Conditions", 8, "NOT_USED", ()),
    ("EXCESSIVE_DEBT", "Excessive Debt Obligations", 8, "MANUAL_REVIEW", ("scoped_debt_obligations",)),
    ("CREDIT_CONCENTRATION", "Existing Credit Concentrations in Market", 8, "OUT_OF_SCOPE", ()),
    ("DEBT_TO_INCOME", "Excessive debt obligations in relation to income", 8, "MANUAL_REVIEW", ("ebitda", "financial_period_start", "financial_period_end")),
    ("LINE_PAYDOWN", "Existing M&T Bank line does not reflect adequate principal paydowns", 8, "NOT_USED", ()),
    ("FORECLOSURE_REPO", "Foreclosure or repossession", 8, "OUT_OF_SCOPE", ()),
    ("GARNISHMENT", "Garnishment", 8, "OUT_OF_SCOPE", ()),
    ("HEIGHTENED_RISK", "Heightened Level of Risk", 8, "POLICY_REVIEW", ("behavioral_program", "previous_pd_grade", "current_pd_grade")),
    ("INCOMPLETE_APPLICATION", "Incomplete information/credit application", 8, "DATA_REVIEW", ("application_complete",)),
    ("INCREASE_WINDOW", "Increase not allowed on accounts opened within the past 12 months", 8, "POLICY_REVIEW", ("is_increase", "account_opened", "as_of_date")),
    ("INSUFFICIENT_CAPITAL", "Insufficient capital for amount of credit requested", 8, "MANUAL_REVIEW", ("estimated_annual_revenue", "requested_amount")),
    ("FINANCIAL_CONDITION", "Insufficient financial condition", 8, "MANUAL_REVIEW", ("prior_revenue", "current_revenue", "prior_expenses", "current_expenses", "prior_profit", "current_profit", "prior_period_end", "current_period_end")),
    ("OPERATING_HISTORY", "Insufficient length of business operations", 8, "MANUAL_REVIEW", ("business_formation_date", "as_of_date")),
    ("USE_OF_FUNDS", "Intended use of funds is not appropriate for requested product/terms", 8, "OUT_OF_SCOPE", ()),
    ("TERM_SHEET_RATE", "Interest rate included on Term sheet is too low", 8, "OUT_OF_SCOPE", ()),
    ("EARNINGS_RECORD", "Lack of established earnings record", 8, "OUT_OF_SCOPE", ()),
    ("LIMITED_CREDIT", "Limited credit experience", 9, "MANUAL_REVIEW", ("account_count", "thin_file_indicator")),
    ("LTV", "Loan to Value Ratio", 9, "OUT_OF_SCOPE", ()),
    ("MARKET_AREA", "Outside of market area", 9, "MANUAL_REVIEW", ("governing_law_state", "collateral_state", "approved_footprint")),
    ("LAWSUIT", "Pending Lawsuit", 9, "OUT_OF_SCOPE", ()),
    ("SBA_INELIGIBLE", "SBA Ineligible", 9, "OUT_OF_SCOPE", ()),
    ("SLOW_BUSINESS_PAY", "Slow business credit pay", 9, "POLICY_REVIEW", ("business_payment_history",)),
    ("SLOW_TRADE_PAY", "Slow or past due in trade or loan payments", 9, "POLICY_REVIEW", ("past_due", "trade_payment_history")),
    ("SLOW_PERSONAL_PAY", "Slow personal credit pay", 9, "POLICY_REVIEW", ("guarantor_payment_history",)),
    ("TAX_LIENS", "Tax liens or judgements", 9, "POLICY_REVIEW", ("lien_count", "liens_satisfied", "tax_lien_indicator", "judgment_count", "judgments_satisfied")),
    ("RISK_RETURN", "Transaction Does Not Meet Risk/Return Thresholds", 9, "NOT_USED", ()),
    ("IDENTITY_VERIFICATION", "Unable to Verify Identity/Business or your Intent to Apply", 9, "DATA_REVIEW", ("identity_verified",)),
    ("INCOME_VERIFICATION", "Unable to verify income", 9, "DATA_REVIEW", ("income_verified",)),
    ("COLLATERAL", "Value or type of collateral not sufficient", 9, "MANUAL_REVIEW", ("collateral_valuation", "collateral_type", "approved_collateral_criteria")),
    ("CREDIT_COUNSELING", "No Reason: Credit Counseling Indicator", 9, "OUT_OF_SCOPE", ()),
)

# Registry IDs correspond to Section 20, not the earlier illustrative MTB IDs.
# Predicate text is auditable declarative metadata, never evaluated as code.
REGISTRY = (
    ("ID-01", 0, 12, "identity", "Unique primary borrower AND consistent explicit legal-entity involvement", ("identity", "borrower_account_id"), "DATA_REVIEW", ("IDENTITY_VERIFICATION",)),
    ("DQ-01", 0, 2, "freshness", "Explicit as-of, report and score dates; approved recency unavailable", ("as_of_date", "report_date", "score_date"), "DATA_REVIEW", ()),
    ("EL-01", 2, 12, "quick_app", "Explicit new money <=100M, Tier I <=250M, nonprofit excluded; request conflict retained", ("new_money", "tier_i", "is_nfp"), "DATA_REVIEW", ()),
    ("EL-02", 2, 12, "product", "Product-specific amount, security, documents, purpose and conversion checks", ("product", "requested_amount", "secured", "acceptance_letter_present", "sba_memo_present", "cam_complete", "loan_purpose", "existing_product", "new_line", "collateral_retained"), "MANUAL_REVIEW", ("COLLATERAL",)),
    ("EL-03", 2, 12, "baloc_increase", "BALOC increase six/twelve-month conflict is unresolved", ("is_increase", "account_opened", "as_of_date"), "POLICY_REVIEW", ("INCREASE_WINDOW",)),
    ("KO-01", 3, 12, "compliance", "Possible or confirmed OFAC match => compliance review, never automatic denial", ("ofac_alert", "ofac_disposition"), "COMPLIANCE_REVIEW", ()),
    ("KO-02", 3, 12, "credit_freeze", "Frozen or absent bureau evidence cannot be treated as clear", ("credit_freeze",), "MANUAL_REVIEW", ()),
    ("KO-03", 3, 13, "personal_fico", "Screenshot average of typed personal FICO for explicitly linked borrower/guarantor accounts >=650; any missing linked-party score makes the average unknown", ("fico",), "DATA_REVIEW", ()),
    ("KO-04", 3, 13, "sbss", "Selected SBSS >=160", ("sbss",), "DATA_REVIEW", ()),
    ("KO-05", 3, 13, "earnings", "Explicit EBITDA >=0 in a defined period; EBIT is NOT EBITDA", ("ebitda", "financial_period_start", "financial_period_end"), "POLICY_REVIEW", ("EXPENSES_EXCEED_INCOME",)),
    ("KO-06", 3, 13, "bankruptcy", "Verified discharge at least five calendar years before as-of, or explicit zero bankruptcies", ("bankruptcy_count", "bankruptcy_discharge_date", "as_of_date"), "DATA_REVIEW", ("BANKRUPTCY",)),
    ("KO-07", 3, 13, "public_record_codes", "Codes 1/2/3 require definitions; 0 is not an automatic clearance", ("max_delinquency_public_records",), "POLICY_REVIEW", ("DEROGATORY_PERSONAL",)),
    ("KO-08", 3, 13, "age", "Under-18 diagnostic; applicant applicability remains unresolved", ("dob", "as_of_date"), "POLICY_REVIEW", ()),
    ("STR-01", 4, 13, "strategy", "Section 19 SBSS/ownership/FICO/request/NAICS decision tree; separate SBA/conventional candidates", ("sbss", "fico", "ownership_years", "requested_amount", "naics", "vehicle_only"), "POLICY_REVIEW", ()),
    ("CR-01", 4, 13, "party_credit", "Judgments, liens and collections by borrower/guarantor; age/amount policy undefined", ("judgment_count", "judgments_satisfied", "collection_indicator", "lien_count", "liens_satisfied"), "POLICY_REVIEW", ("COLLECTION_JUDGMENT", "TAX_LIENS")),
    ("CR-02", 4, 13, "credit_experience", "Accounts <3 OR explicitly scoped thinFileIndicator=true is a review flag", ("account_count", "thin_file_indicator"), "MANUAL_REVIEW", ("LIMITED_CREDIT",)),
    ("FIN-01", 4, 13, "liquidity", "Explicit current assets >= current liabilities", ("current_assets", "current_liabilities"), "MANUAL_REVIEW", ("WORKING_CAPITAL",)),
    ("FIN-02", 4, 13, "capital", "Revenue/line-size review; 10% is illustrative, never a universal threshold", ("estimated_annual_revenue", "requested_amount"), "MANUAL_REVIEW", ("INSUFFICIENT_CAPITAL",)),
    ("POL-01", 1, 13, "governance", "Draft has no verified production policy signoff or lender authority", (), "POLICY_REVIEW", ()),
)


def number(value: Any) -> Decimal | None:
    try:
        return exact_decimal(value)
    except ValueError:
        return None


def flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {"y": True, "yes": True, "true": True, "n": False, "no": False, "false": False}.get(value.casefold())
    return None


def evaluate_expanded_spec(*, graph: EntityGraph, loan: EntityNode, profile: dict,
                          features: dict, refs: dict[str, list[str]], strategy: dict,
                          source_available: bool) -> ExpandedSpecEvaluation:
    """Retain full coverage, fail closed on missing scope/semantics/source.

    FAIL means an adverse diagnostic observation, not an issued denial. REVIEW
    means no approved predicate/data; NOT_EVALUATED covers not-used/out-of-POC.
    """
    as_of = iso_date(profile.get("as_of_date"))
    period_start = iso_date(profile.get("financial_period_start"))
    period_end = iso_date(profile.get("financial_period_end"))
    earnings_period = bool(period_start and period_end and as_of and period_start <= period_end <= as_of)
    n = lambda key: number(profile.get(key))
    known: dict[str, bool | None] = {}
    known["BANKRUPTCY"] = features.get("bankruptcy_discharge_at_least_5_years")
    ebitda = n("ebitda")
    known["EXPENSES_EXCEED_INCOME"] = ebitda >= 0 if earnings_period and ebitda is not None else None
    known["DEBT_TO_INCOME"] = known["EXPENSES_EXCEED_INCOME"]
    assets, liabilities = n("current_assets"), n("current_liabilities")
    known["WORKING_CAPITAL"] = assets >= liabilities if assets is not None and liabilities is not None else None
    accounts = n("account_count")
    valid_accounts = accounts is not None and accounts >= 0 and accounts == accounts.to_integral_value()
    thin = flag(profile.get("thin_file_indicator"))
    # Scoped observations may establish the literal thinFileIndicator meaning
    # from p.12; no interpretation of abbreviated characteristic names.
    observed = [flag(item.get("value")) for item in features.get("indicators", []) if item.get("key") == "thinFileIndicator"]
    if observed:
        thin_values = observed + ([thin] if thin is not None else [])
        thin = thin_values[0] if len(set(thin_values)) == 1 else None
        refs = {**refs, "thin_file_indicator": [ref for item in features.get("indicators", [])
            if item.get("key") == "thinFileIndicator" for ref in item.get("evidence_refs", [])]}
    known["LIMITED_CREDIT"] = False if thin is True or (valid_accounts and accounts is not None and accounts < 3) else True if thin is False and valid_accounts else None
    known["IDENTITY_VERIFICATION"] = flag(profile.get("identity_verified"))
    known["INCOME_VERIFICATION"] = flag(profile.get("income_verified"))
    known["INCOMPLETE_APPLICATION"] = flag(profile.get("application_complete"))
    public_record_code = n("max_delinquency_public_records")
    valid_public_record_code = (public_record_code is not None
                                and public_record_code == public_record_code.to_integral_value()
                                and public_record_code in {Decimal("0"), Decimal("1"), Decimal("2"), Decimal("3")})
    known["DEROGATORY_PERSONAL"] = (
        public_record_code == 0 if valid_public_record_code else None
    )
    formation = iso_date(profile.get("business_formation_date"))
    if formation and as_of and formation <= as_of:
        anniversary = (formation.replace(year=formation.year + 2, day=28 if formation.month == 2 and formation.day == 29 else formation.day)
                   if formation.year <= 9997 else None)
        known["OPERATING_HISTORY"] = as_of >= anniversary if anniversary else None
    else:
        known["OPERATING_HISTORY"] = None
    prior_end, current_end = iso_date(profile.get("prior_period_end")), iso_date(profile.get("current_period_end"))
    # Explicit year-over-year comparable dates, not two arbitrary balances.
    comparable = bool(prior_end and current_end and as_of and current_end <= as_of and current_end.year == prior_end.year + 1 and (current_end.month, current_end.day) == (prior_end.month, prior_end.day))
    metrics = [n(key) for key in ("prior_revenue", "current_revenue", "prior_expenses", "current_expenses", "prior_profit", "current_profit")]
    if comparable and all(value is not None for value in metrics):
        pr, cr, pe, ce, pp, cp = metrics
        assert pr is not None and cr is not None and pe is not None and ce is not None and pp is not None and cp is not None
        known["FINANCIAL_CONDITION"] = not (cr < pr or ce > pe or cp < pp)
    else:
        known["FINANCIAL_CONDITION"] = None

    def evidence(fields: tuple[str, ...]) -> list[str]:
        return sorted({ref for key in fields for ref in refs.get(key, [])})

    reasons = []
    for code, label, page, disposition, fields in REASONS:
        outcome = known.get(code)
        inactive = disposition in {"OUT_OF_SCOPE", "NOT_USED"}
        status = "NOT_EVALUATED" if inactive or not source_available else "REVIEW" if outcome is None else "PASS" if outcome else "FAIL"
        detail = ("Explicitly not used by Business Banking." if disposition == "NOT_USED" else
                  "Out of POC; no clearance claimed." if inactive else
                  "Expanded specification unavailable or hash mismatch; not evaluated." if not source_available else
                  "Adverse diagnostic observation; not an approved denial or adverse-action reason." if outcome is False else
                  "Defined diagnostic did not match on supplied evidence; not a credit clearance." if outcome is True else
                  "Missing verified inputs or approved scope/threshold/semantics; authorized review required. No fabricated denial logic.")
        if code == "EXPENSES_EXCEED_INCOME":
            detail += " EBIT is never substituted for EBITDA; explicit financial period required."
        if code == "INSUFFICIENT_CAPITAL":
            detail += " The 10% revenue illustration is not a threshold."
        if code == "INCREASE_WINDOW":
            detail += " INCREASE_WAIT_6_VS_12_MONTHS remains unresolved."
        if code == "BROKER_REFERRAL":
            detail += " Broker policy and applicable scope require confirmation."
        reasons.append(DeclineReasonDiagnostic(code=code, label=label, page=page, disposition=disposition,
            status=status, detail=detail, required_inputs=list(fields), evidence_refs=evidence(fields)))

    lookup = {item.code: item for item in reasons}
    observation_conflict = bool(observed and len(set(observed + ([flag(profile.get("thin_file_indicator"))] if flag(profile.get("thin_file_indicator")) is not None else []))) > 1)
    for group in ("indicators", "characteristics"):
        values_by_key: dict[str, set[str]] = {}
        for item in features.get(group, []):
            key = item.get("key")
            if isinstance(key, str):
                values_by_key.setdefault(key, set()).add(str(item.get("value")))
        observation_conflict = observation_conflict or any(len(values) > 1 for values in values_by_key.values())
    borrowers = graph.borrowers(loan.id)
    junctions = graph.related("EntityInvolvement", "loan_id", loan.id)
    explicit_junction = any(node.fields.get("account_id") in borrowers and str(node.fields.get("role", "")).casefold() == "borrower" for node in junctions)
    identity = features.get("identity_valid") is True and explicit_junction and not graph.issues and not observation_conflict
    report_date, score_date = iso_date(profile.get("report_date")), iso_date(profile.get("score_date"))
    dated = bool(as_of and report_date and score_date and report_date <= as_of and score_date <= as_of)
    guarantors = features.get("guarantors", [])
    fico = number(features.get("fico"))
    sbss = number(features.get("sbss"))
    baloc = str(profile.get("product", "")).casefold() in {"express_baloc", "conventional_baloc", "sba_baloc"}
    increase = flag(profile.get("is_increase"))
    outcomes: dict[str, tuple[str, str]] = {
        "ID-01": ("PASS" if identity else "REVIEW", "Primary account and explicit matching borrower junction required; graph defects hold all loans."),
        "DQ-01": ("REVIEW", "Dates present, but approved report/score recency limits are undefined." if dated else "Missing/future/ambiguous as-of, report or score date."),
        "EL-01": ("PASS" if strategy.get("eligibility_route") == "QUICK_APP" else "REVIEW", "Quick App diagnostics remain separate from unresolved requested-amount policy."),
        "EL-03": ("REVIEW" if baloc and increase is not False else "NOT_EVALUATED", "Six/twelve-month BALOC increase conflict; neither period is selected."),
        "KO-01": ("FAIL" if features.get("ofac_alert_clear") is False or features.get("ofac_confirmed_match") is True else "PASS" if features.get("ofac_alert_clear") is True and features.get("ofac_confirmed_match") is False else "REVIEW", "Possible match requires compliance review; no substantiated sanction inferred."),
        "KO-02": ("PASS" if features.get("credit_freeze_clear") is True else "FAIL" if features.get("credit_freeze_clear") is False else "REVIEW", "Freeze requires manual review; absent report is not clear."),
        "KO-03": ("REVIEW" if fico is None else "PASS" if fico >= 650 else "FAIL", "Screenshot criterion uses the exact arithmetic average of typed personal FICO for explicitly linked borrower/guarantor parties; unknown party scores prevent an average."),
        "KO-07": ("FAIL" if valid_public_record_code and public_record_code in {Decimal("1"), Decimal("2"), Decimal("3")}
                  else "PASS" if valid_public_record_code and public_record_code == 0 else "REVIEW",
                  "Screenshot diagnostic: maximum-delinquency public-record codes 1, 2, or 3 match a knockout candidate; zero does not match this candidate. This is not a production decision or clearance."),
        "KO-04": ("REVIEW" if sbss is None else "PASS" if sbss >= 160 else "FAIL", "Selected explicitly loan-scoped SBSS only."),
        "KO-05": (lookup["EXPENSES_EXCEED_INCOME"].status, lookup["EXPENSES_EXCEED_INCOME"].detail),
        "KO-06": (lookup["BANKRUPTCY"].status, "Five-calendar-year diagnostic; approved date policy remains unverified."),
        "KO-08": ("REVIEW", "Age diagnostic retained; applicability to business applicant versus individuals/guarantors unresolved."),
        "STR-01": ("PASS" if strategy.get("strategy_branch") and strategy.get("credit_assessment") == "SIMULATED_APPROVAL_CANDIDATE" else "FAIL" if strategy.get("credit_assessment") == "SIMULATED_DECLINE_CANDIDATE" else "REVIEW", "Section 19 candidate only; vehicle override and request-boundary conflict remain unresolved."),
        "CR-01": ("REVIEW", "Party-specific judgments/collections/liens lack approved age, amount and code definitions."),
        "CR-02": (lookup["LIMITED_CREDIT"].status, lookup["LIMITED_CREDIT"].detail),
        "FIN-01": (lookup["WORKING_CAPITAL"].status, lookup["WORKING_CAPITAL"].detail),
        "FIN-02": ("REVIEW", "Requested-line/revenue review is judgmental; 10% illustration is not a universal rule."),
        "POL-01": ("REVIEW", "Draft unsigned policy; authorized_decision=NOT_ISSUED, reviewer_id absent. No production authority integration."),
    }
    product = str(profile.get("product", "")).casefold()
    secured = flag(profile.get("secured"))
    amount = n("requested_amount")
    product_issues: list[str] = []
    if product == "bil":
        if secured is not False or amount is None or amount > Decimal("100000000"):
            product_issues.append("BIL requires unsecured term debt <=USD100M; collateral conversion needs review")
        if flag(profile.get("acceptance_letter_present")) is not True:
            product_issues.append("BIL acceptance letter required")
    elif product == "sba_bil":
        if profile.get("loan_purpose") != "permanent_working_capital":
            product_issues.append("SBA BIL permanent working capital purpose required")
        if flag(profile.get("sba_memo_present")) is not True or flag(profile.get("cam_complete")) is not True:
            product_issues.append("SBA memo and CAM evidence required")
    elif product == "term_loan":
        if flag(profile.get("fixed_rate_confirmed")) is not True or flag(profile.get("repayment_terms_confirmed")) is not True:
            product_issues.append("Term loan fixed rate/repayment stipulations require explicit evidence")
    elif product not in {"express_baloc", "conventional_baloc", "sba_baloc"}:
        product_issues.append("Unsupported or missing product")
    else:
        if secured is None or amount is None:
            product_issues.append("BALOC amount and security required")
        elif product == "express_baloc" and (secured or amount > Decimal("100000000")):
            product_issues.append("Express amount/security exception requires existing versus incremental exposure review")
        elif product == "conventional_baloc" and amount > Decimal("100000000") and not secured:
            product_issues.append("Conventional BALOC above USD100M requires secured collateral")
        existing = profile.get("existing_product")
        if existing in {"sba_baloc", "conventional_baloc"} and product in {"sba_baloc", "conventional_baloc"} and existing != product and flag(profile.get("new_line")) is not True:
            product_issues.append("SBA/conventional conversion requires a new line")
        if flag(profile.get("reducing_secured_line")) is True and amount is not None and amount < Decimal("100000000") and flag(profile.get("collateral_retained")) is not True:
            product_issues.append("Collateral retention required when reducing a secured line below USD100M")
        if features.get("baloc_special_cap_within_25m") is False and (flag(profile.get("is_startup")) is True or flag(profile.get("is_investment_re")) is True):
            product_issues.append("Startup/investment-real-estate BALOC exceeds USD25M diagnostic cap")
    outcomes["EL-02"] = ("REVIEW" if product_issues else "PASS", "; ".join(product_issues) or "Defined product diagnostics satisfied; not full underwriting or authorization.")
    results = []
    for rule_id, precedence, page, scope, predicate, inputs, action, codes in REGISTRY:
        status, detail = outcomes[rule_id]
        results.append(ExpandedRuleResult(rule_id=rule_id, scope=scope, precedence=precedence, page=page,
            predicate=predicate, required_inputs=list(inputs), missing_data_action=action,
            exception_path="Authorized policy/compliance review; never an invented automatic exception",
            decline_reason_codes=list(codes), status=status if source_available else "NOT_EVALUATED",
            detail=detail if source_available else "Expanded source missing/mismatched; not evaluated.", evidence_refs=evidence(inputs)))
    results.sort(key=lambda item: (item.precedence, item.rule_id))
    by_id = {item.rule_id: item for item in results}
    eligibility = strategy["eligibility_route"]
    route = strategy["credit_assessment"]
    if route == "SIMULATED_APPROVAL_CANDIDATE":
        route = ("CONVENTIONAL_APPROVAL_CANDIDATE"
                 if strategy.get("strategy_branch") == "SBSS_GE_200_OWNERSHIP_GE_2_OTHER_NAICS"
                 or str(strategy.get("strategy_branch", "")).endswith("VEHICLE_ONLY_CONVENTIONAL_OVERRIDE")
                 else "SBA_APPROVAL_CANDIDATE")
    if not source_available:
        eligibility, route = "POLICY_REVIEW", "POLICY_REVIEW"
    elif not identity or not dated:
        eligibility, route = "DATA_REVIEW", "DATA_REVIEW"
    missing = sorted({key for item in results if item.status == "REVIEW" for key in item.required_inputs if profile.get(key) is None})
    if not explicit_junction:
        missing.append("explicit_borrower_entity_involvement")
    if not earnings_period:
        missing.append("verified_ebitda_period")
    safe_input = {"profile": profile, "guarantors": guarantors, "graph_issues": graph.issues,
                  "strategy_branch": strategy.get("strategy_branch"), "reason_outcomes": known}
    fingerprint = hashlib.sha256(json.dumps(safe_input, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
    return ExpandedSpecEvaluation(source_available=source_available, eligibility_route=eligibility,
        simulated_credit_route=route, rule_results=results, decline_reasons=reasons,
        candidate_reason_codes=([item.code for item in reasons if item.status == "FAIL"]
            + [item.rule_id for item in results if item.status == "FAIL" and item.rule_id in {"KO-03", "KO-04"}]),
        missing_inputs=sorted(set(missing)), policy_conflicts=sorted(set(strategy.get("unresolved_conflicts", []))
            | ({"CONFLICTING_SCOPED_OBSERVATIONS"} if observation_conflict else set())
            | {"UNAPPROVED_DECLINE_REASON_CODES", "REPORT_SCORE_RECENCY_UNDEFINED", "FICO_AGGREGATION_UNDEFINED", "EBITDA_DEFINITION_PERIOD_UNAPPROVED"}),
        input_fingerprint_sha256=fingerprint)