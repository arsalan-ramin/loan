# TEST screenshot demo v1 — backend contract

Reprocess in `TEST` mode to populate
`underwriting.loan_results[].normalized_data.demo_decision`. Historical saved
reports need reprocessing; the demo is not retroactively computed by commentary.
No provider request is needed to evaluate this local deterministic trace.

## Rendering contract

- `version`: `screenshot-test-demo-v1`
- `mode`: `TEST`
- `label`: `Approve`, `Decline`, or `Refer` (always display **simulation only**)
- `simulation_only`: `true`
- `authorization`: `NOT_ISSUED`
- `human_review_required`: `true`
- `final_action`: `NO_OPERATIONAL_ACTION`
- `selection_basis`: `FIRST_ACCOUNT_MATCH_FOR_TEST_DEMO`
- `selected_report_id`: first explicitly borrower-account-linked report in graph
  insertion (source workbook/row) order, or null. This legacy scalar stays
  borrower-specific, not a representative guarantor report.
- `report_date_checked`, `report_completion_checked`, `freshness_asserted`: `false`
- `candidate_route`: `SBA`, `CONVENTIONAL`, or null; candidate, not program eligibility.
- `branch`, `rationale`: reached branch and short business explanation.
- `criteria`: list of `criterion`, normalized `value`, `status`, `evidence_refs`,
  `detail`, and `knockout`. Six knockout statuses use PASS/FAIL/UNKNOWN;
  additional branch inputs use OBSERVED/UNKNOWN, not invented clearance.
- `normalized_facts`: normalized scores, explicit EBITDA/amount, age, bankruptcy
  clearance/count, ownership, NAICS and vehicle flag; missing facts stay null.
- `fico_aggregation`: `basis=LINKED_BORROWER_GUARANTOR_ACCOUNT_AVERAGE` and
  `account_count` (all included unique accounts, including unresolved ones).
- `fico_account_audit`: local-only per-account `account_id`, validated `party_roles`,
  `selected_report_id`, `report_status` (`FIRST_ACCOUNT_MATCH`/`NO_ACCOUNT_MATCH`),
  `score_status`, normalized `score` or null, and `evidence_refs`. Score statuses
  distinguish `VALID_PERSONAL_FICO`, `NO_ACCOUNT_MATCH`, `CONFLICTING_REPORT`,
  `UNTYPED_PERSONAL_FICO`, and `MISSING_OR_INVALID_PERSONAL_FICO`.
- `source_evidence`: demo-specific source cells for its criteria. Resolve criterion
  references against this list; do not replace the strict v4 evidence list.
- `missing_inputs`, `conflicts`: demo-only unresolved inputs/conflicts.
- `reference_date`: explicit application as-of date or null;
  `reference_date_basis`: `EXPLICIT_APPLICATION_AS_OF_DATE` or `UNAVAILABLE`.

No report date/as-of prerequisite applies to demo report selection. Age and
positive-bankruptcy discharge recency still require their explicit date inputs;
missing dates make those checks UNKNOWN individually. No fallback date is used.
Explicit zero bankruptcies need no discharge date.

### Linked-account personal FICO

FICO is the arithmetic average across **all explicitly loan-linked borrower and
guarantor accounts**, each account counted once even with repeated associations.
Only validated borrower/guarantor graph roles participate; unknown roles, owners,
unrelated accounts and accounts linked only to other loans do not. Guarantors
retain their roles and are never substituted for a borrower.

For each included account, select its first account-matching report in source
order, without date/completion checks. Never skip an unusable first report for a
later, higher or newer score. Canonical `personal_fico` is explicitly typed and
accepted without a category/role inference. Otherwise generic `fico` requires
`personal`, `consumer` or `personal_fico` category **and an explicit report role
matching that account's validated borrower/guarantor role**. A graph role alone
does not type a generic score. Business/untyped generic scores are excluded.

Each raw score must be integral and within 300–850; the resulting average may be
fractional and is not rounded before comparison with 650 or 760. Any included
account with a missing report, missing/invalid/untyped score, or conflicting score
or required typing field makes the entire average UNKNOWN—never a partial
average. A supplied null/poisoned canonical personal score cannot fall back to a
generic score. Relationship and report/score references for both known and
unresolved parties remain available in the local audit and criterion evidence.

Borrower-absent (including multiple-guarantor-only) loans may have known FICO.
Borrower-specific report checks and age remain UNKNOWN; guarantor DOB/report
data do not supply them. The missing unique borrower relationship still prevents
approval. Known knockouts, including low average FICO, can still produce a
simulation Decline without borrower identity. No borrower-specific input is
invented from a guarantor.

## Tree

Any known knockout (public record codes 1/2/3, negative explicit EBITDA, verified
discharge less than five calendar years, under 18, average personal FICO below 650, SBSS
below 160) produces **Decline** even when unrelated inputs remain unknown.
UNKNOWN never clears **Approve**.

- SBSS 160–189: ownership at least two years → SBA; otherwise FICO at least 760
  → SBA; otherwise FICO at least 650 and below 760 and amount at most USD100K → SBA.
  Amount above USD100K through USD100M → Refer, retaining
  `REQUEST_THRESHOLD_100M_VS_100K`. Amount above USD100M → Decline.
- SBSS 190–199 → SBA.
- SBSS at least 200: ownership below two years → SBA; otherwise configured listed
  high-risk NAICS prefixes → SBA, other valid NAICS → conventional.
- Vehicle-only true overrides SBA to conventional; missing vehicle flag when
  SBA is reached → Refer.

Requested application path, group-credit and real-estate inputs are not demo
tree gates. No underwriting-program equivalence, EBIT-to-EBITDA conversion,
characteristic pivots, report freshness or approved policy is inferred.

## Isolation and explanation

PRODUCTION never evaluates or attaches this trace. The strict `_select_report`
and `_select_response`, v4 recommendation path, rule results, conflicts, source
evidence, final action and `decision_status` remain unchanged. Label the existing
trace separately as **STRICT**; never blend its blockers into the demo label.

Commentary receives a bounded allowlist of normalized demo facts, selection basis,
label, check statuses and evidence IDs, not raw reports/workbook rows/source-cell
values. Bureau facts beyond normalized scores are represented as check statuses.
FICO aggregation metadata is restricted to the constant basis and an integer
account count from 0 through 1,000,000; per-account audit, account/report IDs and
raw source-cell values are not extra provider metadata. Existing context budgets
remain unchanged. Valid fractional averages are preserved; out-of-range FICO is
not sent. Older saved demo snapshots without aggregation metadata remain
explainable as saved facts, without guessing a count, reconstructing an average,
or adding linked parties retroactively; reprocess to obtain the new audit.
Commentary explains demo first but retains `recommended_route=MANUAL_REVIEW`,
`decision_recommendation=null` and human review. No real approval, denial, offer,
adverse action or provider call is issued by the demo.