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
  `detail`, and `knockout`. Four knockout statuses use PASS/FAIL/UNKNOWN;
  EBITDA/public-record diagnostics and additional branch inputs use OBSERVED/UNKNOWN,
  with `knockout=false`, not invented clearance.
- `normalized_facts`: normalized scores, explicit EBITDA/amount, age, bankruptcy
  clearance/count, ownership, NAICS and vehicle flag; missing facts stay null.
- `fico_aggregation`: `basis=LINKED_BORROWER_GUARANTOR_ACCOUNT_AVERAGE` and
  `account_count` (all included unique accounts, including unresolved ones).
- `age_aggregation`: `basis=LINKED_BORROWER_GUARANTOR_MIN_COMPLETED_CALENDAR_AGE`
  and `account_count` (all included unique accounts, including unresolved ones).
- `age_account_audit`: local-only per-account `account_id`, validated `party_roles`,
  completed integer `age_years` or null, `status` (PASS/FAIL/UNKNOWN), `age_status`,
  and `evidence_refs`. Age statuses distinguish `VALID_COMPLETED_CALENDAR_AGE`,
  `NO_ACCOUNT_MATCH`, `CONFLICTING_ACCOUNT`, `MISSING_OR_INVALID_DOB`,
  `MISSING_OR_INVALID_AS_OF_DATE`, `FUTURE_DOB`, and `AGE_OUT_OF_RANGE`.
  No raw DOB is copied into this audit; demo DOB evidence retains provenance
  with its value redacted to null. Original graph/source data are not modified.
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
- `age_reference_date`: actual calendar date used for age, or null;
  `age_reference_date_basis`: `EXPLICIT_APPLICATION_AS_OF_DATE`,
  `CURRENT_UTC_DATE_FOR_AGE`, or `UNAVAILABLE`. The fallback date is persisted
  with the trace, not recomputed when viewing/explaining a saved report.

No report date/as-of prerequisite applies to demo report selection. Age alone
uses the current UTC calendar date when the loan request/as-of date is absent.
Positive-bankruptcy discharge recency still requires verified discharge and
explicit loan as-of dates; it never uses the age fallback. Missing dates make
that check UNKNOWN. Explicit zero bankruptcies need no discharge date.

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
Borrower-specific report checks remain UNKNOWN; guarantor report data do not
supply them. Linked guarantors' own DOBs can establish their ages independently.
The missing unique borrower relationship still prevents
approval. Known knockouts, including low average FICO, can still produce a
simulation Decline without borrower identity. No borrower-specific input is
invented from a guarantor.

### Linked-account age

Age uses the same deduplicated explicitly linked borrower/guarantor Account set
as FICO. Each person's completed calendar age uses **only that Account's `dob`**
and the loan's explicit valid `as_of_date`. Only when that loan date is absent,
use `datetime.now(timezone.utc).date()` once for all linked parties and record
the actual date with basis `CURRENT_UTC_DATE_FOR_AGE`. `evaluate_demo` accepts an
optional keyword `age_reference_date` (a calendar `date`) for deterministic
fallback tests; it never overrides a supplied date. Omitted, null and empty
cells are absent under the existing graph normalization. Malformed dates,
timestamps and conflicting date evidence remain UNKNOWN, with no fallback.
Never use another person's DOB, a loan/extension/report DOB, report date or
creation timestamp. The age-only date never changes report selection or
bankruptcy discharge recency, and is never added to the strict graph.
Birthdays are calendar anniversaries; February 29 becomes February 28 in a
non-leap year. Future DOBs, invalid dates, poisoned/conflicting Accounts or DOBs,
and completed ages outside 0–150 are unresolved. Duplicate Accounts are merged
by the graph; conflicting copies never receive source precedence.

Ages are **never averaged**. If every included account is valid, `age_years` is
the minimum completed integer age, and the check is PASS only if all are at least
18. Any known age below 18 makes the check FAIL and the simulation Decline even
when other parties remain unresolved; the scalar is then the minimum known
underage age. Otherwise any unresolved party (or no linked parties) makes the
aggregate UNKNOWN with a null scalar. A known underage FAIL does not erase gaps:
`missing_inputs` retains `age_years` and the complete local per-account audit
retains every unresolved party and available relationship/Account/date evidence.
Guarantor-only loans can have known ages without inventing a borrower or filling
other borrower-specific fields.

## Tree

Any known knockout (verified bankruptcy
discharge less than five calendar years, under 18, average personal FICO below 650, SBSS
below 160) produces **Decline** even when unrelated inputs remain unknown.
UNKNOWN on these four knockout checks never clears **Approve**.

EBITDA requirements are explicitly excluded from the TEST demo decision flow,
despite the screenshot's top knockout list. `ebitda_usd` mirrors the public-record
diagnostic: valid explicit normalized EBITDA (including negative or zero) is
OBSERVED; missing, invalid or conflicting EBITDA is UNKNOWN, always with
`knockout=false`. Neither condition blocks either SBA or conventional simulation
approval, enters `missing_inputs`, causes Decline or generates a demo match for
`EXPENSES_EXCEED_INCOME`. No EBIT or net-income substitution is permitted.
OBSERVED is not PASS, financial clearance, income verification or evidence that
expenses do not exceed income; excluding a requirement implies no clearance.
Only age, bankruptcy, FICO and SBSS are knockout failure/approval gates, and the
approval rationale counts four checks. Identity and applicable tree-branch inputs
still apply, including the existing requested-amount decline branch.
Strict TEST/PRODUCTION EBITDA checks and the expanded decline-reason catalog
remain unchanged.

Public-record requirements are explicitly excluded from the DEMO decision flow,
even though they appear in the screenshot's top knockout box. `public_record_code`
remains an observed diagnostic: valid codes 0/1/2/3 are OBSERVED, unavailable or
invalid codes are UNKNOWN, always with `knockout=false`. Neither unknown nor
positive codes block approval, enter `missing_inputs`, cause Decline or produce
`matched_reasons`. Failure and approval gates consider knockout checks only.
This exclusion does not change strict underwriting public-record checks.

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
FICO and age aggregation metadata are restricted to their constant basis and an integer
account count from 0 through 1,000,000; per-account audit, account/report IDs and
raw source-cell values are not extra provider metadata. Existing context budgets
remain unchanged. Valid fractional averages are preserved; out-of-range FICO is
not sent. Ages are bounded integers 0–150. A known underage age remains FAIL in
bounded context even if a stale status says UNKNOWN; unresolved-party gaps are
preserved alongside that FAIL. An adult scalar never promotes UNKNOWN to PASS.
Raw DOBs and per-account age audits are excluded from provider context.
The recorded age-reference date and allowlisted basis are included without
consulting the clock. EBITDA and public records stay diagnostic in bounded context even
when a legacy saved status says FAIL or `knockout=true` (or omits the flag):
valid values become OBSERVED, otherwise UNKNOWN, always with `knockout=false`.
Their missing inputs and matches
are excluded. Explicit `knockout=false` is respected when reconciling checks
and generating matched reasons. Saved statuses and labels are not rewritten;
reprocess historical reports to obtain the updated demo decision.
Older saved demo snapshots without aggregation metadata remain
explainable as saved facts, without guessing a count, reconstructing an average,
or adding linked parties retroactively; reprocess to obtain the new audit.
Commentary explains demo first but retains `recommended_route=MANUAL_REVIEW`,
`decision_recommendation=null` and human review. No real approval, denial, offer,
adverse action or provider call is issued by the demo.