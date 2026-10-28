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
- `selected_report_id`: first explicitly account-linked report in graph insertion
  (source workbook/row) order, or null; wrong accounts are excluded.
- `report_date_checked`, `report_completion_checked`, `freshness_asserted`: `false`
- `candidate_route`: `SBA`, `CONVENTIONAL`, or null; candidate, not program eligibility.
- `branch`, `rationale`: reached branch and short business explanation.
- `criteria`: list of `criterion`, normalized `value`, `status`, `evidence_refs`,
  `detail`, and `knockout`. Six knockout statuses use PASS/FAIL/UNKNOWN;
  additional branch inputs use OBSERVED/UNKNOWN, not invented clearance.
- `normalized_facts`: normalized scores, explicit EBITDA/amount, age, bankruptcy
  clearance/count, ownership, NAICS and vehicle flag; missing facts stay null.
- `source_evidence`: demo-specific source cells for its criteria. Resolve criterion
  references against this list; do not replace the strict v4 evidence list.
- `missing_inputs`, `conflicts`: demo-only unresolved inputs/conflicts.
- `reference_date`: explicit application as-of date or null;
  `reference_date_basis`: `EXPLICIT_APPLICATION_AS_OF_DATE` or `UNAVAILABLE`.

No report date/as-of prerequisite applies to demo report selection. Age and
positive-bankruptcy discharge recency still require their explicit date inputs;
missing dates make those checks UNKNOWN individually. No fallback date is used.
Explicit zero bankruptcies need no discharge date. Generic credit score requires
personal category and borrower role; canonical personal FICO is explicitly typed.
Conflicting copies of a node's score remain unknown, not workbook-precedence wins.

## Tree

Any known knockout (public record codes 1/2/3, negative explicit EBITDA, verified
discharge less than five calendar years, under 18, personal FICO below 650, SBSS
below 160) produces **Decline** even when unrelated inputs remain unknown.
UNKNOWN never clears **Approve**.

- SBSS 160–189: ownership at least two years → SBA; otherwise FICO at least 760
  → SBA; otherwise FICO 650–759 and amount at most USD100K → SBA.
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
Commentary explains demo first but retains `recommended_route=MANUAL_REVIEW`,
`decision_recommendation=null` and human review. No real approval, denial, offer,
adverse action or provider call is issued by the demo.