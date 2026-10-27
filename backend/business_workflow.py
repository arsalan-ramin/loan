"""User-supplied existing-process diagram: ordered, non-authorizing diagnostics."""

from decimal import Decimal

from .underwriting_models import exact_decimal


def interpret_business_workflow(*, profile, identity_valid, graph_issues,
                                knockout_results, strategy):
    """Never substitute Tier I exposure, loan amount or purpose for eligibility."""
    def boolean(value):
        if type(value) is bool:
            return value
        return {"true": True, "false": False, "yes": True, "no": False}.get(
            value.strip().casefold()) if isinstance(value, str) else None

    try:
        group_credit = exact_decimal(profile.get("group_credit_usd"))
        if (group_credit < 0 or profile.get("currency", "USD") != "USD"
                or profile.get("amount_scale", "actual_dollars") != "actual_dollars"):
            group_credit = None
    except ValueError:
        group_credit = None
    quick = str(profile.get("requested_path", "")).casefold() == "quick_app"
    path_known = str(profile.get("requested_path", "")).casefold() in {"quick_app", "full_underwriting"}
    re_secured = boolean(profile.get("real_estate_secured"))
    excluded = (path_known and not quick) or re_secured is True or (
        group_credit is not None and group_credit > Decimal("250000"))
    eligible = ("OUT_OF_SCOPE" if excluded else "PASS" if quick and
                re_secured is False and group_credit is not None else "UNKNOWN")
    missing = []
    if not path_known:
        missing.append("requested_path")
    if group_credit is None:
        missing.append("explicit_group_credit_usd")
    if re_secured is None:
        missing.append("explicit_real_estate_secured")
    failed = [r["rule_id"] for r in knockout_results if r["status"] == "FAIL"]
    unknown = [r["rule_id"] for r in knockout_results if r["status"] == "UNKNOWN"]
    candidate = "REFER"
    knockout_status = remaining_status = "NOT_EVALUATED"
    if eligible == "PASS" and identity_valid is True and not graph_issues:
        knockout_status = "CANDIDATE_MATCH" if failed else "UNKNOWN" if unknown or not knockout_results else "PASS"
        if failed:
            candidate = "DECLINE_CANDIDATE"
        elif not unknown and knockout_results:
            remaining_status = "DIAGNOSTIC_ONLY"
            assessment = strategy.get("credit_assessment")
            candidate = {"SIMULATED_APPROVAL_CANDIDATE": "APPROVE_CANDIDATE",
                         "SIMULATED_DECLINE_CANDIDATE": "DECLINE_CANDIDATE"}.get(assessment, "REFER")
    return {
        "source": "User-provided existing-process diagram; not approved lender policy",
        "eligibility": eligible,
        "group_credit_usd": str(group_credit) if group_credit is not None else None,
        "group_credit_limit_usd": "250000",
        "real_estate_secured": re_secured,
        "missing_inputs": missing,
        "recommendation_candidate": candidate,
        "observed_knockout_matches": failed,
        "interpretation": ("Ordered flow stopped before credit-strategy assessment. Any matched standalone risk diagnostics are observations only, not a workflow decline recommendation."
                   if knockout_status == "NOT_EVALUATED" else
                   "Eligibility and identity guards satisfied; interpret knockout results before remaining strategy."),
        "authorization": "NOT_ISSUED",
        "human_review_required": True,
        "policy_conflicts": ["DIAGRAM_250K_GROUP_CREDIT_VS_V4_250M_TIER_I",
                             "REQUEST_THRESHOLD_100M_VS_100K", "INCREASE_WAIT_6_VS_12_MONTHS"],
        "stages": [
            {"stage": "1. Input and eligibility", "status": eligible,
             "summary": "Quick App; explicit group credit <= USD 250,000; explicitly not real-estate secured. No Tier I, requested-amount or investment-purpose substitution."},
            {"stage": "2. Data preparation and explicit joins",
             "status": "PASS" if identity_valid is True and not graph_issues else "UNKNOWN",
             "summary": "One feature set per explicit loan. Only supported IDs link accounts, reports and responses; unmapped fields remain UNKNOWN."},
            {"stage": "3. Knockout criteria", "status": knockout_status,
             "summary": "Evaluate supplied knockout diagnostics first; any match is a candidate only. Missing evidence never passes."},
            {"stage": "3. Remaining existing strategy", "status": remaining_status,
             "summary": "Evaluate only after eligibility, identity and knockout evidence are complete. Draft source conflicts remain unresolved."},
            {"stage": "4. Recommendation output", "status": candidate,
             "summary": "Candidate/rationale only. Approved adverse-action reason mapping unavailable; no notice, approval or denial issued."},
            {"stage": "5. Historical outcome validation", "status": "NOT_IMPLEMENTED",
             "summary": "No matched human-decision dataset or confusion matrix is available. File validation is not accuracy validation."},
            {"stage": "6. Underwriter review", "status": "REQUIRED",
             "summary": "An authorized underwriter makes the final decision outside this application. Nothing is auto-decided."},
        ],
    }