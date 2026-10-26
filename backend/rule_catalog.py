"""Readiness for the v4 executable contract, never evaluated loan outcomes.

IDs match engine rule results except explicitly described graph/model safeguards,
the variable-number guarantor family, and the request-conflict readiness entry.
Pages cite the three-page v4 guide unless the category explicitly names the
seven-page implementation guide. Guide entries are readiness-only diagnostics,
not additional emitted v4 rule results.
"""

from .underwriting_engine import load_underwriting_config
from .expanded_spec import REASONS, REGISTRY
from .underwriting_models import EXPANDED_SPEC_DOCUMENT


def guideline_rules() -> list[dict]:
    config = load_underwriting_config()
    definitions = [
        ("IDENTITY_EXPLICIT_RELATIONSHIP", "Explicit borrower and loan relationship", "Identity", 1, "enforced",
         "Require a unique explicit borrower link and existing account. No row-order/name joins or identity-validation shortcut."),
        ("NO_EXPLICIT_LOAN_RECORDS", "Explicit populated loan records", "Schema and provenance", 1, "enforced",
         "Batch graph safeguard: empty templates and source rows without explicit Loan records cannot create loan recommendations."),
        ("INVALID_RECORD_SHAPE", "Canonical schema and graph integrity", "Schema and provenance", 1, "enforced",
         "Graph safeguard: exact manifest sheets/aliases, scalar IDs and row provenance; conflicting aliases/duplicates, orphan links, multiple borrowers/extensions and malformed records hold the batch."),
        ("INVALID_USD_AMOUNT", "Exact USD currency and amount scale", "Currency", 1, "enforced",
         "Graph issue family INVALID_USD_AMOUNT:<table>:<field>: accept exact decimal text, integers or finite Decimal in USD/actual_dollars, never floats, booleans, formulas or inferred scales. Unknown is not zero."),
        ("POLICY_SOURCE_V4", "Pinned v4 source integrity", "Source integrity", 3, "enforced",
         f"Require local {config['source_document']} AND the engine-pinned SHA-256. Config assertion alone is insufficient; missing/unverified source makes page-dependent checks UNKNOWN, not approved policy."),
        ("HUMAN_REVIEW_REQUIRED", "Human review and production policy signoff", "Governance", 3, "enforced",
         "Engine/model safeguard, not a separate rule result: human_review_required is always true. PRODUCTION final action is MANUAL_REVIEW; TEST is NO_OPERATIONAL_ACTION. No production authorization or lender integration is verified."),
        ("PRODUCT_MAPPING", "Explicit supported product", "Products", 1, "enforced",
         "Only express_baloc, conventional_baloc, sba_baloc, bil, sba_bil and term_loan are classified. Missing/unsupported product remains UNKNOWN; no inferred product or lending approval."),
        ("QUICKAPP_NFP_DIAGNOSTIC", "Quick App nonprofit candidate", "Quick App candidates", 2, "conditional",
         "Only for an explicit Quick App request: explicit nonprofit status determines the candidate exclusion. Full underwriting is required by the draft, not an emitted operational route."),
        ("NEW_MONEY_100M_CANDIDATE", "Quick App new money ≤ USD 100M candidate", "Quick App candidates", 2, "conditional",
         "Quick App only: compare explicit new_money with USD 100,000,000. This is not requested_amount; the USD 100M versus USD 100K request-policy conflict remains unresolved."),
        ("TIER_I_250M_CANDIDATE", "Quick App Tier I ≤ USD 250M candidate", "Quick App candidates", 2, "conditional",
         "Quick App only: compare explicit Tier I with USD 250,000,000, not total group exposure. Group_Credit_Amount__c is not accepted as Tier I without confirmed scope."),
        ("PRODUCT_AMOUNT_DIAGNOSTIC", "Express/conventional BALOC amount candidate", "Product candidates", 2, "conditional",
         "Express BALOC ≤ USD 100M versus conventional BALOC > USD 100M. Emitted for Express/conventional/BIL only; BIL amount boundary remains UNKNOWN. No invented BIL limit."),
        ("PRODUCT_SECURITY_DIAGNOSTIC", "Express/conventional BALOC security candidate", "Product candidates", 2, "conditional",
         "Express BALOC unsecured versus conventional BALOC secured, with explicit security evidence. BIL is NOT_APPLICABLE; other products do not emit this check."),
        ("BALOC_SPECIAL_25M_CANDIDATE", "Startup/investment-RE BALOC USD 25M candidate", "Product candidates", 2, "conditional",
         "Only BALOC with startup/investment-real-estate status not explicitly false; compare USD 25,000,000 when an explicit flag is true. Unknown classification stays UNKNOWN; never a general term-loan cap."),
        ("INCREASE_6_MONTHS_DIAGNOSTIC", "Six-calendar-month increase diagnostic", "Increase candidates", 2, "conditional",
         "BALOC when increase is not explicitly false; explicit opened/as-of dates determine the six-month anniversary. Does not resolve the competing twelve-month interpretation."),
        ("INCREASE_12_MONTHS_DIAGNOSTIC", "Twelve-calendar-month increase diagnostic", "Increase candidates", 2, "conditional",
         "BALOC when increase is not explicitly false; explicit opened/as-of dates determine the twelve-month anniversary. Does not resolve the competing six-month interpretation."),
        ("SBSS_BUCKET_DIAGNOSTIC", "Valid SBSS diagnostic bucket", "Score features", 2, "conditional",
         "Explicit selected response SBSS, integer 0–300: <160, 160–189, 190–199 or ≥200. Buckets are features, not approved decision branches."),
        ("FICO_BAND_DIAGNOSTIC", "Typed personal FICO diagnostic band", "Score features", 2, "conditional",
         "Unique latest completed account-scoped report on/before explicit as-of date; personal FICO only, integer 300–850: <650, 650–759 or ≥760. No business/untyped score substitution or owner averaging."),
        ("GUARANTOR_{n}_CREDIT_DIAGNOSTIC", "Independent guarantor credit evidence", "Identity and credit evidence", 1, "conditional",
         "Engine rule-ID family GUARANTOR_1_CREDIT_DIAGNOSTIC, GUARANTOR_2_CREDIT_DIAGNOSTIC, etc.; emitted only for explicit guarantors. Select each party's own completed dated personal report; never substitute for borrower FICO or aggregate scores."),
        ("OWNERSHIP_2_YEARS_DIAGNOSTIC", "Two-year ownership diagnostic", "Ownership features", 2, "conditional",
         "Compare explicit nonnegative ownership_years with two years. No derived ownership or approved SBSS/FICO/amount conjunction."),
        ("EBITDA_NONNEGATIVE_DIAGNOSTIC", "Nonnegative EBITDA knockout candidate", "Knockout candidates", 2, "conditional",
         "Use explicit exact EBITDA ≥ zero only. No derivation from EBIT, revenue, net income or balances; a candidate failure is not an operational denial."),
        ("BANKRUPTCY_5_CALENDAR_YEARS_DIAGNOSTIC", "Five-calendar-year bankruptcy candidate", "Knockout candidates", 2, "conditional",
         "Explicit zero bankruptcies or valid count/discharge/as-of evidence for five calendar years. Missing/future discharge dates stay UNKNOWN; no invented multiple-bankruptcy cutoff."),
        ("MINIMUM_AGE", "Minimum-age knockout candidate", "Knockout candidates", 2, "age",
         f"Configured draft minimum_age={config['minimum_age']}; explicit DOB and as-of date required. Not an approved operational denial rule."),
        ("OFAC_CONFIRMED_MATCH", "Confirmed OFAC disposition diagnostic", "Compliance", 2, "conditional",
         "Only explicit confirmed_match or cleared establishes disposition. A confirmed match requires compliance/manual review, not autonomous lending denial."),
        ("OFAC_ALERT_CLEAR", "Unverified OFAC alert diagnostic", "Compliance", 2, "conditional",
         "Explicit alert flag only; an alert requires designated review and never establishes a confirmed match."),
        ("CREDIT_FREEZE_CLEAR", "Credit freeze diagnostic", "Compliance", 2, "conditional",
         "Explicit freeze flag only; frozen/unknown bureau evidence requires review, not automatic lending denial."),
        ("HIGH_RISK_NAICS", "Draft high-risk NAICS examples", "Knockout candidates", 2, "naics",
         f"Configured example prefixes: {', '.join(config['high_risk_naics']) or 'none'}. Require explicit valid NAICS; examples are not an exhaustive approved industry policy."),
        ("PERSONAL_FICO_650_CANDIDATE", "Personal FICO ≥650 knockout candidate", "Knockout candidates", 2, "conditional",
         "Exact average of typed personal FICO from explicitly linked borrower/guarantor accounts, as stated by the screenshot. Missing linked-party score makes the average unknown; never substitute business/untyped scores."),
        ("SBSS_160_CANDIDATE", "SBSS ≥160 knockout candidate", "Knockout candidates", 2, "conditional",
         "Selected response SBSS only; below 160 is a draft knockout candidate, not an operational denial."),
                ("MAX_DELINQUENCY_PUBLIC_RECORDS_KNOCKOUT_CANDIDATE", "Maximum delinquency public-record knockout candidate", "Knockout candidates", 2, "conditional",
                 "Screenshot diagnostic: explicit maximum-delinquency public-record code 1, 2 or 3 matches the knockout candidate; explicit 0 does not. Missing/invalid code is UNKNOWN. Candidate only, not an operational lending decision."),
        ("SBSS_160_189_BRANCH", "SBSS 160–189 unresolved conjunction", "SBSS branches", 2, "blocked",
         "Applicable only in the 160–189 bucket; the engine always returns UNKNOWN because the ownership/FICO/amount conjunction is unspecified."),
        ("SBSS_190_199_SBA_CANDIDATE", "SBSS 190–199 SBA candidate", "SBSS branches", 2, "conditional",
         "Applicable only in the 190–199 bucket: draft SBA candidate path only, not guaranteed eligibility or approval."),
        ("SBSS_200_OWNERSHIP_NAICS_CANDIDATE", "SBSS ≥200 unresolved ownership/NAICS conjunction", "SBSS branches", 2, "blocked",
         "Applicable only in the ≥200 bucket; the engine always returns UNKNOWN because the complete approved ownership/NAICS conjunction is unspecified."),
        ("REQUEST_THRESHOLD_POLICY", "Unresolved USD 100M versus USD 100K request policy", "Policy conflicts", 2, "blocked",
         "Readiness-only conflict entry, not a separately emitted engine rule: REQUEST_THRESHOLD_100M_VS_100K remains unresolved under QUICKAPP_POLICY. Neither requested_amount interpretation is approved; do not choose a boundary."),
        ("QUICKAPP_POLICY", "Quick App policy gate", "Policy gates", 2, "blocked",
         "Engine gate always UNKNOWN: verified candidate limits and nonprofit diagnostics do not resolve the request-threshold conflict or authorize production decisions."),
        ("PRODUCT_BALOC_POLICY", "Product/BALOC policy gate", "Policy gates", 2, "blocked",
         "Engine gate always UNKNOWN: candidate amount/security/cap diagnostics do not establish complete approved product criteria."),
        ("SBSS_FICO_BRANCH_POLICY", "Screenshot SBSS/FICO strategy gate", "Policy gates", 2, "blocked",
         "The simulated candidate branch follows only the supplied screenshot; production authority remains absent. Average only typed personal FICO from explicitly linked parties. The conflicting USD100K/USD100M amount interval stays POLICY_REVIEW."),
        ("OWNERSHIP_EBITDA_POLICY", "Ownership/EBITDA policy gate", "Policy gates", 2, "blocked",
         "Engine gate always UNKNOWN: separate two-year ownership and EBITDA features are not an approved decision conjunction."),
        ("BANKRUPTCY_POLICY", "Bankruptcy policy gate", "Policy gates", 2, "blocked",
         "Engine gate always UNKNOWN: the five-year candidate diagnostic does not establish a complete approved bankruptcy policy."),
        ("GROUP_EXPOSURE_POLICY", "Group exposure policy gate", "Policy gates", 2, "blocked",
         "Engine gate always UNKNOWN: effective TGC=max(Tier I, Tier II) requires both explicit amounts. No guessed relationship aggregation, approved exposure decision rule or nCino routing is implemented."),
        ("INCREASE_WAIT_POLICY", "Unresolved six-versus-twelve-month policy gate", "Policy conflicts", 2, "blocked",
         "Engine gate always UNKNOWN: INCREASE_WAIT_6_VS_12_MONTHS remains unresolved. Both calendar diagnostics are retained; neither waiting period is approved."),
        ("GUIDE_SOURCE_INTEGRITY", "Separate implementation-guide source integrity", "Implementation guide · source and stages", 5, "enforced",
         f"Readiness-only guide safeguard, not a v4 rule result: require local {config['implementation_guide_source_document']} and its pinned SHA-256. Missing/mismatched guide produces POLICY_REVIEW candidates, not a verified strategy tree."),
        ("GUIDE_STAGES_0_6", "Stages 0–6 and non-operational audit", "Implementation guide · source and stages", 5, "conditional",
         "Readiness-only guide diagnostics in normalized_data.implementation_guide_diagnostics: schema/identity, draft approval/mode, product completeness, knockouts, credit strategy, non-operational route, explanation/audit. Eligibility, credit candidate and NOT_ISSUED authorization remain separate; audit retains an input fingerprint. Stage statuses are not v4 rule outcomes."),
        ("GUIDE_SECTION_8_STRATEGY", "Screenshot SBSS/ownership/average-FICO decision tree", "Implementation guide · Section 8", 4, "conditional",
         "Readiness-only screenshot diagnostic: after source/identity checks and the six screenshot knockout candidates, evaluate only the screenshot SBSS/ownership/average-personal-FICO/request/NAICS tree. The 160–189, ownership-under-two, average-FICO-650–759 amount band conflicts (>USD100K knockout list versus <=USD100M chart); the overlap stays POLICY_REVIEW. No actual approval or denial is issued."),
        ("GUIDE_VEHICLE_OVERRIDE", "Screenshot vehicle-only conventional override", "Implementation guide · Section 8", 4, "conditional",
         "The screenshot says verified vehicle-only loans override all SBA candidate nodes to a conventional candidate. Require explicit vehicle_only=true; missing/unknown status prevents confirming the override and remains review. Candidate only, not actual approval."),
        ("GUIDE_OUT_OF_SCOPE", "Explicit out-of-scope checks", "Implementation guide · source and stages", 5, "enforced",
         "Readiness-only diagnostic contract: negative news, lawsuits, garnishment, SBA eligibility, LTV, collections/judgement thresholds and collateral terms remain NOT_EVALUATED. This marker is not a v4 rule status or a completed compliance check."),
    ]
    reasons = {
        "enforced": "Mandatory executable safeguard, not a claim of PASS or completed evaluation; optional document checks cannot disable it.",
        "conditional": "Diagnostic only for applicable loans/products with valid mapped evidence; page-dependent checks require their separately pinned source. Not a per-loan outcome or approved lending criterion.",
        "blocked": "Policy criteria/conflict resolution or a required configured parameter is unavailable; no executable approved decision is claimed. Review remains required.",
    }
    output = []
    for rule_id, name, category, page, gate, description in definitions:
        state = gate
        if gate == "age":
            state = "conditional" if config["minimum_age"] is not None else "blocked"
        elif gate == "naics":
            state = "conditional" if config["high_risk_naics"] else "blocked"
        output.append(dict(id=rule_id, name=name, category=category, page=page,
                           description=description, state=state, reason=reasons[state]))
    for rule_id, precedence, page, scope, predicate, inputs, action, codes in REGISTRY:
        output.append(dict(id="EXPANDED-" + rule_id, name=f"{rule_id} · {scope}",
            category="Expanded specification · rule registry", page=page,
            description=f"{EXPANDED_SPEC_DOCUMENT} p.{page}; expanded-draft-1, stage {precedence}. {predicate}. Required canonical inputs: {', '.join(inputs) or 'verified policy authority'}. Missing: {action}. Internal reason links: {', '.join(codes) or 'none'}. Separate from emitted v4 rule_results.",
            state="conditional", reason="Draft diagnostic registry, not approved policy or an evaluated per-loan outcome; inspect expanded_spec.rule_results."))
    for code, label, page, disposition, inputs in REASONS:
        output.append(dict(id="REASON-" + code, name=label,
            category="Expanded specification · complete reason inventory", page=page,
            description=f"{EXPANDED_SPEC_DOCUMENT} p.{page}. {disposition}; canonical inputs: {', '.join(inputs) or 'out of scope/not used'}. No approved adverse-action code; never an automatic operational denial.",
            state="blocked" if disposition in {"OUT_OF_SCOPE", "NOT_USED", "POLICY_REVIEW"} else "conditional",
            reason="Explicit inventory coverage only. Missing thresholds/scope remain REVIEW; out-of-POC and not-used reasons remain NOT_EVALUATED."))
    return output