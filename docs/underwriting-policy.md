# MTB v4 underwriting — verified draft diagnostics

## Source, version and authority

Verified on **2026-10-20** against the implementation and local source:

- Source: [backend/data/LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf](../backend/data/LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf), **three pages (1–3)**.
- SHA-256: **9ed35356fdf8d7f638b6eb77c463df71f9a6dd17234a5fe31be251fda8ac18e7**.
- Executable configuration: [backend/underwriting_config.json](../backend/underwriting_config.json), version `v4-USD-verified-draft-2`.
- Entry point: `evaluate_underwriting(documents, mode="PRODUCTION", *, policy_config=None)` in [backend/underwriting_engine.py](../backend/underwriting_engine.py). Models and serialization: [backend/underwriting_models.py](../backend/underwriting_models.py); graph: [backend/entity_graph.py](../backend/entity_graph.py).

**This is a verified draft, not approved production credit policy.** A pinned hash, user confirmation of units, `PRODUCTION` mode or a synthetic fixture's `approved` flag does not confer lending authority. There is no integrated lender authorization/signoff, approval/denial, funding, adverse-action, nCino or compliance-clearance workflow. An authorized human must review results outside this application.

The engine verifies the local PDF's existence and bytes against the pinned hash in the module's backend data directory. Merely uploading a PDF, setting a config assertion or changing `FOLIO_DATA_DIR` cannot substitute the source. Missing/mismatched source yields `source_available=false`; page-dependent checks become `UNKNOWN`. Source availability alone never clears unresolved policy gates. Page citations are engine-supplied draft references (currently p.2 for page-dependent diagnostics); null page means a data diagnostic, **not an inherited old p.4 citation**.

[backend/policy.py](../backend/policy.py) and [backend/policy_config.json](../backend/policy_config.json) retain older evaluator APIs for compatibility/tests only. New recognized MTB reviews execute v4, not the old evaluator. Do not use the old four-page source's rules or routing precedence to describe v4 results.

## Authoritative report and modes

[backend/agents.py](../backend/agents.py) attaches an `UnderwritingBatch` at `report.underwriting`. It contains mode, USD currency, version, source availability, batch data-quality issues and one `LoanRecommendation` per explicit Loan node. Empty templates or unidentified rows produce no invented loans and include `NO_EXPLICIT_LOAN_RECORDS` when appropriate.

Each loan has its borrower ID, exact amounts, separate normalized nonmoney data, rule trace, source evidence, missing inputs, conflicts, deterministic explanation, path and final action. Evidence retains workbook, sheet, row, field, record ID where supplied, lexical value and reference ID. Rule states are `PASS`, `FAIL`, `UNKNOWN`, `NOT_APPLICABLE`; none is an operational decision. Unknown is not zero/false, and not-applicable is not a passed gate.

| Mode | Recommendation paths | Required final action |
| --- | --- | --- |
| `PRODUCTION` (default) | `DATA_QUALITY_HOLD` for graph/identity/conflicting-data defects; otherwise `POLICY_REVIEW_REQUIRED` | `MANUAL_REVIEW` |
| `TEST` | `SIMULATED_REVIEW`; only qualified developer-injected synthetic fixtures can yield `SIMULATED_APPROVE_PATH` or `SIMULATED_DECLINE_PATH` | `NO_OPERATIONAL_ACTION` |

`human_review_required` is always true. Model validation rejects production simulated paths, operational approval/denial and mode/action inconsistencies. Missing inputs/ambiguous selections force synthetic review; a failed draft diagnostic is not a loan denial. Production ignores synthetic criteria and retains the two default conflicts. The default public TEST configuration is not synthetic-ready and produces `SIMULATED_REVIEW` for explicit loans, even with otherwise complete fixture data.

MTB document status is always `needs_review`; optional text checks cannot authorize lending. `policy_evaluation` in a new MTB report is a **read-only compatibility adapter** of v4 with aggregate `MANUAL_REVIEW`, null decision recommendation and profiles for explicit loans only. It is not a second execution of legacy rules. [frontend/src/App.tsx](../frontend/src/App.tsx) prefers `report.underwriting`; [frontend/src/UnderwritingReport.tsx](../frontend/src/UnderwritingReport.tsx) renders per-loan paths/actions, exact amounts, missing inputs, conflicts, deterministic explanations and resolved source cells. Old reports without v4 may still render the legacy card; reprocess to obtain v4, rather than assuming stored history was rewritten.

## Currency and exact money contract

Actual USD dollars were confirmed on 2026-10-20. `100000000.00` means USD 100 million, never USD 100 thousand. Config defaults are `source_currency="USD"`, `amount_scale="actual_dollars"`; explicit row overrides must match. Missing defaults/invalid row units hold data rather than inferring a scale.

- Input money accepts plain decimal lexical strings, integers or finite Python `Decimal`. Use text cells in XLSX for fractional money. Floats, booleans, exponents, formulas, currency signs, grouping separators and NaN/Infinity are rejected, not repaired.
- Exposure amounts cannot be negative. Explicit EBITDA, business net income and net worth may be negative; their sign is not silently discarded.
- JSON money is decimal **strings or null**, never binary floats. Frontend displays the supplied string without number coercion. CSV nests exact JSON and sanitizes spreadsheet-formula prefixes.
- Effective TGC is `max(tier_i, tier_ii)` only if **both** explicit values are present for the loan profile. No guessed zero, sum, cross-account aggregation or group-membership inference.
- EBITDA is explicit only. No unsupported derivation from EBIT, revenue, net income, cash, assets or liabilities; `ebitda_derivation` remains `blocked_without_approved_inputs`.

Accepted internal canonical aliases and public normalized output names (the latter are also accepted input aliases on the appropriate table):

| Canonical input alias | Public `_usd` key | Table |
| --- | --- | --- |
| `requested_amount` | `loan_requested_amount_usd` | Loan |
| `new_money` | `new_money_usd` | Loan |
| `tier_i` / `tier_ii` | `tier1_group_credit_usd` / `tier2_group_credit_usd` | Loan |
| computed `effective_tgc` | `total_group_credit_usd` | Output only; computed from both tiers |
| `ebitda` | `ebitda_usd` | Account or LoanExtension |
| `past_due` | `total_past_due_usd` | CreditReport |
| `estimated_annual_revenue` | `estimated_annual_revenue_usd` | Account |
| `business_net_income` | `business_net_income_usd` | Account |
| `cash_and_equiv` / `net_worth` | `cash_and_equiv_usd` / `net_worth_usd` | Account |
| `total_assets` / `total_liabilities` | `total_assets_usd` / `total_liabilities_usd` | Account |
| `available_credit` / `bankruptcy_amount` | `available_credit_usd` / `bankruptcy_amount_usd` | CreditReport |
| `credit_limit` | `credit_limit_usd` | Loan via manifest amount-field extension; no verified source-export alias |

Verified source aliases include Loan `LLC_BI_Amount__c`, Account `Estimated_Annual_Revenue__c`, `Business_Net_Income__c`, `SBSS7_cash_and_equiv__c`, `SBSS7_net_worth__c`, `SBSS7_total_assets__c`, `SBSS7_total_liabilities__c`, and CreditReport `nFUSE_Total_Past_Due__c`, `nFUSE_Available_Credit__c`, `nFUSE_Bankruptcy_Amount__c`. These are exact spellings; flattened mappings additionally accept specific double-underscore variants listed in the manifest. `Group_Credit_Amount__c` is **disabled as Tier I** until `group_credit_tier1_scope_confirmed=true`; confirmed currency does not confirm exposure scope. Unsupported capitalization/names are not fuzzy-matched.

## Account/Loan graph and evidence selection

The manifest supports **exact canonical sheets** and **explicit flattened aliases on recognized MTB workbooks**. Canonical sheet support at the engine boundary does not by itself change the application's MTB workflow detection; public uploads should use recognized MTB filenames or signatures.

| Canonical sheet | Identity and relationships |
| --- | --- |
| Account | `Id` or `account_id` |
| Loan | `Id` or `loan_id`; explicit `borrower_account_id` and optional `response_id` |
| EntityInvolvement | `Id` or `involvement_id`; `loan_id`, `account_id`, role `borrower`/`guarantor` |
| CreditReport | `Id` or `report_id`; `account_id`, completion status, `report_date` |
| LoanExtension | `Id` or `extension_id`; `loan_id` |
| LCRESPONSE | `Id` or `response_id`; `loan_id` or validated applicable-response/entity linkage |
| LCREntity | `Id` or `entity_id`; `account_id`, `response_id` |
| DataSourceAnalysis | `Id` or `analysis_id`; `entity_id` |
| Indicator / Characteristic | `Id` or appropriate observation ID; `analysis_id`, `key`, `value` |

Relationships are explicit: Loan–Account (direct borrower or involvement junction); CreditReport–Account; LoanExtension–Loan; Loan–LCRESPONSE; response–LCREntity–Account; entity–analysis–observations. Flattened examples include `Loan_Request_Id`/`LLC_BI__Loan__c`, `LLC_BI__Account__c`, `SCOC__LCResponse__r.Id` or a valid scalar `SCOC__LCResponse__r`, and explicit `borrower_role`/`LLC_BI__Borrower_Type__c`. The checked-in manifest is the full allowlist, not a claim that every source field has confirmed business semantics.

IDs must be explicit opaque lexical scalars (letters/digits followed by letters/digits/underscore/hyphen, maximum 128 characters); not objects, numbers, formulas or relationship repr strings. Canonical direct borrower IDs are explicit links. A flattened Account ID alone is not a borrower role. `Identity_Mapping_Validated` is ignored. No row-order, same-name, convenient-score or filename-based join is invented. Duplicate conflicts preserve evidence, poison the field and hold rather than overwrite; orphan links and multiple borrowers/extensions are reported. Batch graph defects intentionally hold **all** loans, not only a guessed affected subset. ID-less observations may use local provenance storage keys, never fabricated Salesforce record IDs.

**Report selection:** explicit ISO `as_of_date` is required. Reports are scoped to the account. Completed statuses are `completed`/`complete`; known incomplete statuses are `pending`, `in_progress`, `failed`, `cancelled`. Missing/unknown completion status prevents selection. Completed reports require valid unambiguous dates. Choose the unique latest completed report on or before as-of; ties, ambiguous/missing dates or no applicable report remain review conditions. There is no guessed current date or approved maximum report-age rule.

**Personal FICO:** accept explicitly typed `personal_fico`, otherwise require `report_category` personal/consumer/personal_fico **and** matching `report_role` borrower/guarantor before using `fico`. A business bureau score, untyped credit score or guarantor's score cannot substitute for borrower personal FICO. Guarantors use independently account-scoped reports; no invented owner average or cross-account replacement.

**Response/indicators:** choose the explicitly applicable response or a unique linked response; mismatched loans or missing borrower entity links remain unknown/held. Indicators/characteristics stay one-to-many, scoped through analysis → entity → selected response and borrower account. Both feature-mapping dictionaries default empty. No automatic business-key pivot; money, IDs and credit-score targets are not supported indicator substitutions. Conflicting observations remain conflicts.

## Implemented diagnostic checks, not approved decision branches

| Diagnostic family | Actual treatment |
| --- | --- |
| Identity/source | Explicit unique borrower and existing account; local v4 source integrity check |
| Quick App | NFP diagnostic; inclusive USD 100M new-money and USD 250M Tier I candidate comparisons; request USD 100K and USD 100M features coexist, not a resolved limit |
| Product | Explicit `express_baloc`, `conventional_baloc`, `sba_baloc`, `bil`, `sba_bil`, `term_loan`; draft Express/conventional amount/security comparisons; BIL amount boundary unspecified |
| Special BALOC | Startup/investment-RE USD 25M candidate cap, not a general term-loan cap |
| Increase timing | Separate 6- and 12-calendar-month anniversary diagnostics from explicit `account_opened`/as-of dates; neither selects approved policy |
| SBSS/FICO | SBSS validity 0–300 and buckets <160, 160–189, 190–199, ≥200; personal FICO validity 300–850 and bands <650, 650–759, ≥760; candidate minima 160/650, not operational knockouts |
| Score branches | 160–189 conjunction unspecified; 190–199 SBA candidate only; ≥200 ownership/NAICS candidate incomplete. No invented score/amount/ownership conjunction |
| Ownership/EBITDA | Explicit two-year ownership comparison; explicit nonnegative EBITDA comparison, no derivation |
| Bankruptcy/age | Explicit zero bankruptcies or discharge at least five calendar years; minimum age 18 from explicit DOB/as-of. Future/absent dates unknown; calendar anniversaries clamp leap days |
| OFAC/freeze | Confirmed match distinct from alert; alert/freeze requires review, never automatic denial |
| NAICS | Lexical 2–6 digit codes; draft prefix examples `23`, `4451`, `45`, `8111`, `8121`, `8123`; not an exhaustive approved industry policy |
| Policy gates | `QUICKAPP_POLICY`, `PRODUCT_BALOC_POLICY`, `SBSS_FICO_BRANCH_POLICY`, `OWNERSHIP_EBITDA_POLICY`, `BANKRUPTCY_POLICY`, `GROUP_EXPOSURE_POLICY`, `INCREASE_WAIT_POLICY` remain `UNKNOWN` |

Both conflicts remain explicit: `REQUEST_THRESHOLD_100M_VS_100K` and `INCREASE_WAIT_6_VS_12_MONTHS`. Neither units confirmation nor passing candidate diagnostics resolves them. Legacy acceptance-letter/CAM/pricing/conversion/automatic nCino routing rules do not execute as new v4 policy.

## Complete implementation-guide diagnostics

The separate [seven-page implementation guide](../backend/data/MTB_LLM_Underwriting_Complete_Implementation_Guide.pdf) is pinned to SHA-256 **8a409bf6499129be8efe109d1a15f21ecfd95929b0674b993ab6d13c9c8e9467**. It remains a draft, independently verified by file and hash; it does not replace the three-page v4 source or confer authority. Batch fields `implementation_guide_source_document`, `implementation_guide_source_sha256` and `implementation_guide_available` record its identity and verification. Missing/mismatched guide evidence forces guide policy review.

Section 8 (p.4) supplies a **diagnostic** SBSS/ownership/personal-FICO/amount/NAICS tree after explicit source, identity, product, compliance and knockout guards. The affected 160–189 amount branch stays in policy review above USD 100K because the competing USD 100M interpretation is unresolved. The ≥200/two-year/high-risk branch does not invent a vehicle-only exception: true/unknown vehicle-only status requires policy review. V4 policy gates still remain `UNKNOWN`; guide candidate matches do not turn them into passes.

Each loan separates `eligibility_route`, `credit_assessment` and `authorization`. A `SIMULATED_APPROVAL_CANDIDATE` or `SIMULATED_DECLINE_CANDIDATE` is not an operational lending outcome, even in PRODUCTION. Authorization is always `NOT_ISSUED`; v4 mode/path/final-action restrictions remain unchanged.

Stages 0–6 (p.5) are recorded under `normalized_data.implementation_guide_diagnostics.stages`: schema/identity, draft approval/mode, product completeness, hard knockouts, strategy, non-operational route, and explanation/audit. Audit includes the guide fingerprint and normalized input fingerprint. An early stop can leave later stages pending; this is not a claim that every stage passed. Out-of-scope negative news, lawsuits, garnishment, SBA eligibility, LTV, collections/judgement thresholds and collateral terms remain `NOT_EVALUATED` in diagnostics, never an invented fifth v4 rule status.

The UI and escaped HTML display the separate outputs, guide verification, stage diagnostics, audit and out-of-scope markers. JSON retains full normalized data; CSV includes separate outputs and `implementation_guide_diagnostics` as a JSON-encoded cell. LLM commentary receives only bounded normalized strategy/stage features alongside the existing trace, never authority to alter any output.

## Expanded technical specification coverage

Source: [backend/data/MTB_Underwriting_Expanded_Technical_Specification.pdf](../backend/data/MTB_Underwriting_Expanded_Technical_Specification.pdf), **14 pages**, SHA-256 **a4622c26c642f9546f0df9fd1634ed735d25be36e8042451ba702ddd958a326b**. The source calls itself a screenshot-derived draft, not an approved policy or verified native workbook export. Its identity is independently checked; missing/mismatched source makes all expanded rules/reasons `NOT_EVALUATED` and routes to `POLICY_REVIEW`.

The additive `LoanRecommendation.expanded_spec` is versioned `expanded-draft-1`. [backend/expanded_spec.py](../backend/expanded_spec.py) contains the complete **43-reason** pp.7–9 inventory and **19-entry** rule registry covering Section 20 plus explicit freshness/product/authority guards. Every registry entry records ID, scope, precedence, source/page, version, predicate description, required inputs, missing-data action, exception path, reason links and evidence. `PASS/FAIL/REVIEW/NOT_EVALUATED` are confined to this separate contract, not silently substituted for v4 statuses. A `FAIL` is an adverse diagnostic observation, **not** an issued denial.

| Source area | Implemented diagnostic / conservative boundary |
| --- | --- |
| Identity/freshness pp.1–2, T11/T18 | Expanded candidates require the primary account **and** consistent explicit borrower EntityInvolvement; valid as-of, selected report date and selected response `score_date`. The legacy v4 direct-link contract remains unchanged. Recency limits are undefined and recorded as policy gaps, not invented. |
| Product requirements pp.2–3 | BIL unsecured/≤USD100M/acceptance letter; SBA BIL permanent-working-capital purpose/memo/CAM; term-loan fixed-rate/repayment evidence; BALOC security, new-line requirement for SBA/conventional conversion, collateral retention on reduction, startup/investment-RE diagnostic caps. Unsupported/missing evidence requires review. Express increase exceptions and SBA treatment remain policy-specific review, not automatic exceptions. |
| Knockouts/strategy pp.3–4,12–13 | Explicit FICO<650, SBSS<160, negative period-scoped EBITDA and recent bankruptcy produce simulated diagnostic candidates where evidence is complete. Underage is data/eligibility review. OFAC alerts require compliance review; freezes manual review. Missing source/identity/dates take precedence. SBA versus conventional candidates are distinguished, not authorized approvals. |
| FICO/vehicle/request conflicts | No personal-score average or borrower/guarantor substitution. Any participating guarantor requires policy review pending aggregation rules. Vehicle-only SBA override remains policy review. The affected 160–189 amount branch retains USD100K/USD100M policy review; BALOC increase retains six/twelve-month review. |
| Limited credit/working capital | Explicit account count<3 OR a scoped literal `thinFileIndicator` true is a review flag; complete clean inputs are required for a nonmatching diagnostic. Current assets/current liabilities compare as exact USD. Neither creates an approved denial criterion. |
| Financial condition/operations | YOY diagnostics require explicit comparable period-end dates and revenue/expense/profit inputs; operation age uses explicit formation date, never ownership years or an inferred January date from year-only data. EBITDA requires explicit start/end period; **no EBIT fallback** despite the screenshot suggestion. |
| Collections/liens/delinquency/revenue/collateral | Registry and inventory preserve required inputs and review action. Missing age/amount thresholds, delinquency code definitions, jurisdiction/footprint, collateral criteria and judgmental leverage rules are not executable denials. A 10% revenue example is never treated as universal policy. |
| Out-of-scope / not used | Every screenshot-listed item retained, including market-demand/saturation, risk-return and paydown not-used items; adverse media, foreclosure/repo, garnishment, lawsuit, LTV, SBA eligibility, loan purpose/rate and other out-of-POC items remain `NOT_EVALUATED`, never claimed clear. |
| Indicator/characteristic evidence | One-to-many rows stay scoped through analysis/entity/response/account. Conflicting values for a key conservatively hold expanded candidates even without timestamps; literal thin-file semantics only. Abbreviated characteristic names are retained, not guessed into business rules. |

New accepted inputs are **exact canonical fields on canonical sheets**, listed in `mapping_manifest.expanded_canonical_fields`. Examples: Account financial periods/current assets/current liabilities/prior-current metrics/formation and verification flags; Loan document/purpose/conversion flags; CreditReport account count/thin-file/judgment/lien fields; LCRESPONSE `score_date`. These are explicit contracts, not newly approved Salesforce aliases. Screenshot names in pp.9–12 remain unverified; no fuzzy mapping, no fabricated joins or source values. All added money uses exact `_usd` serialization. Unknown canonical fields and unsupported screenshot inputs do not establish clearance.

The expanded trace includes source and normalized-input hashes, candidate reason codes, missing inputs/conflicts, a prompt version, null reviewer ID and an explicit no-authorized-decision history marker. This is **not** an implemented reviewer authorization service, signed decision history or legally validated adverse-action system. Every `adverse_action_code` is null; internal codes cannot be used as legally approved denial codes. Existing session/report timestamps and LLM model fields remain persisted by the parent report. No full relational staging database, row timestamp precedence, null-rate/duplicate metrics dashboard, or native workbook dictionary verification is claimed.

The LLM receives at most **40 expanded reason summaries globally**, in addition to the existing 20-loan/40-v4-rule/40-evidence budgets, with no raw reason/source values. It cannot change reason codes, routes, authorization or evidence. The application keeps its existing single aggregate commentary call and deterministic report fallback; no additional schema-retry provider call is added (the provider transport retry remains configured separately). This intentionally differs from the draft's suggested extra structured-output retry to avoid unbounded/billable extra commentary attempts.

JSON exports include full `expanded_spec`; CSV adds that JSON-encoded column plus expanded source metadata. HTML and React display separate expanded eligibility, simulated credit route, authorization, all reason dispositions, registry and evidence. Old reports tolerate absent expanded data and require explicit reprocessing; saved history is not rewritten. **All reasons are represented, but unresolved/out-of-scope rules are deliberately not asserted executable.** Production remains manual review with no operational approve/deny.

## Public controls and API

Guided conversation cards offer local preflight, saved loan inspection, explicit process confirmation and report-bound review notes; see [the walkthrough](../README.md#guided-assistant-workflow). `POST /preflight` under a session parses detached uploads and runs deterministic diagnostics with no provider call, no new underwriting report and no modification to saved outcomes. Top-ten summaries are bounded and prioritize schema/identity/date blockers. The full report still requires processing.

Local `assistant/inspect` requests take a current-session loan ID and one allowlisted topic; results carry report ID, up to 12 prioritized rules and 12 source cells with exact lexical values. They persist as historical `local_assistant` messages, not fresh rule evidence. `assistant/process` requires `confirmed=true` and current `expected_updated_at`; it shares existing processing locks and rejects stale confirmation. The original bodyless process API remains supported. No model tool dispatch or automatic processing is added.

Review notes/acknowledgments are stored separately from `Report`, bound to report/loan IDs, labeled unverified assertions and never fed into graph/rule evaluation. Missing business fields still require corrected source uploads; policy decisions require an authorized external process, not chat assertions. `ChatRequest.loan_id` optionally focuses the bounded report context on one existing loan; same-session history/metadata/recall remain broader and are disclosed as such. Ordinary chat failures remain explicit; deterministic local actions do not masquerade as Claude output.

The MTB **Review mode** selector defaults missing legacy mode to PRODUCTION. It is disabled during processing/pending operations, sends only mode, applies the returned invalidated session on success and keeps the prior state on failure. Mode changes clear reports/steps and require **Process documents** again. Upload/chat does not start processing. V4 reports offer HTML/JSON/CSV; generic reports offer HTML only. Export failures show an error, retain deterministic results and re-enable retry.

| Method and full path | Contract |
| --- | --- |
| `POST /api/sessions` | JSON `{ "title": "Optional title", "mode": "TEST" }`; omitted mode defaults PRODUCTION |
| `PUT /api/sessions/{id}/mode` | Exactly `{ "mode": "TEST" }` or `{ "mode": "PRODUCTION" }` |
| `POST /api/sessions/{id}/documents` | Repeated multipart `files`; canonical XLSX contract below |
| `POST /api/sessions/{id}/dataset` | Empty session only; copies fixed original Query 1/2/4 filenames, no server-path/config input |
| `POST /api/sessions/{id}/process` | No body, returns 202; poll `GET /api/sessions/{id}` |
| `GET /api/sessions/{id}/underwriting?format=json` | Batch JSON; JSON is also the default without query parameter |
| `GET /api/sessions/{id}/underwriting?format=csv` | One loan/row; nested amounts, rules, evidence and issues encoded as JSON strings |
| `GET /api/sessions/{id}/report` | Escaped standalone HTML |

Strict uppercase modes; create/mode reject extra fields with 422, including `authority`, `approved`, `test_policy`, `policy_config`. Mode/other edits while processing return 409. Export needs a completed v4 report (409 otherwise; 404 unknown session; 422 unsupported format). JSON preserves exact `Decimal` strings. CSV guards formula prefixes and yields headers only for empty batches; JSON retains empty-batch issues. API details are implemented in [backend/main.py](../backend/main.py), frontend download/mode contract in [frontend/src/api.ts](../frontend/src/api.ts).

### Public TEST walkthrough

**Illustrative synthetic workbook contract only — not actual data.** Prepare three XLSX files named with the recognized original Query 1/2/4 pattern. Each canonical worksheet has its first row as the exact headers below and one synthetic record. These are **contract examples**, not claims that the revised source exports already contain these canonical columns. Supply money as lexical text cells; the table describes worksheet rows, not a JSON upload interface to the graph.

| Workbook role | Canonical sheets | Illustrative records |
| --- | --- | --- |
| Query 1 | Account; Loan | Account `Id=A1`, `dob=1980-10-20`, `is_nfp=false`, `ownership_years=2`, `naics=541100`, `ebitda_usd=0.00`; Loan `Id=L1`, `borrower_account_id=A1`, `as_of_date=2026-10-20`, `product=express_baloc`, `requested_path=quick_app`, `loan_requested_amount_usd=100000.00`, `new_money_usd=100000`, `tier1_group_credit_usd=250000`, `tier2_group_credit_usd=249999.99`, `secured=false`, `is_startup=false`, `is_investment_re=false`, `is_increase=false` |
| Query 2 | LCRESPONSE | `Id=S1`, `loan_id=L1`, `sbss=200` |
| Query 4 | CreditReport | `Id=R1`, `account_id=A1`, `status=completed`, `report_date=2026-10-19`, `personal_fico=760`, `ofac_disposition=cleared`, `ofac_alert=false`, `credit_freeze=false`, `bankruptcy_count=0`, `total_past_due_usd=0` |

Use string cells for IDs, dates, NAICS and numeric lexical inputs, boolean cells for flags; set `currency=USD` and `amount_scale=actual_dollars` explicitly if overriding config defaults. The simple response's explicit loan relationship suffices here; add entity/analysis/observation sheets only with real explicit links. See the [PowerShell API example](../README.md#example-public-test-invocation): create a TEST session, upload all three files, process, poll, then export. Public mode alone does not inject synthetic criteria, so this example remains `SIMULATED_REVIEW` with both conflicts and no operational action.

### Developer-only synthetic override

For **local tests only**, `policy_config` overlays a fresh in-memory default copy; it does not write configuration. Synthetic readiness requires `test_policy.synthetic=true`, `approved=true`, a nonempty label, explicit resolution of **both default conflicts plus any additional configured conflicts**, and a **nonempty** criteria list. The word `approved` is a fixture enablement flag, **not production policy signoff**.

Each criterion must contain exactly `rule_id`, `feature`, `op`, `value`; rule IDs must be safe `TEST-...` identifiers. Operators: `eq`, `ne`, `ge`, `gt`, `le`, `lt`, `in`. Criteria read normalized diagnostics/amounts; numeric comparisons use exact decimals, equality is type-sensitive. After graph/required-input/review guards, all passing criteria yield simulated approve, any failing criterion yields simulated decline; invalid/missing criterion inputs yield simulated review. Draft `UNKNOWN` gates remain separate: an approved-looking simulation is not proof those policy gates passed. PRODUCTION ignores synthetic criteria and cannot emit simulated outcomes.

This verified Python invocation uses existing test helpers **solely as a developer example**, not a production ingestion recommendation:

```python
from backend.tests.test_underwriting import document, rows, synthetic_policy
from backend.underwriting_engine import evaluate_underwriting

batch = evaluate_underwriting(
		[document(rows())], mode="TEST", policy_config=synthetic_policy()
)
loan = batch.loan_results[0]
assert loan.recommendation_path == "SIMULATED_APPROVE_PATH"
assert loan.final_action == "NO_OPERATIONAL_ACTION"
assert loan.human_review_required is True
print(batch.model_dump_json(indent=2))
```

That helper supplies explicitly labeled synthetic criteria and marks both conflicts resolved **only for the simulation**. It does not change the checked-in defaults or supply policy-owner approval. Overrides are **not exposed through the public API/UI**. Production callers use validated workbook evidence and the public session workflow, not internal test helpers.

### Illustrative exact JSON

**Synthetic illustration, not actual customer data, not a captured full engine export.** This is a model-valid individual `LoanRecommendation` showing exact decimal strings and null-vs-zero. Empty trace/evidence collections are abbreviated illustrative content, not a claim that the engine evaluated checks without provenance. Actual JSON exports wrap full loans in `UnderwritingBatch.loan_results`, include all normalized amount keys and retain full traces/issues.

```json
{
	"loan_id": "SYNTHETIC-L1",
	"borrower_account_id": "SYNTHETIC-A1",
	"currency": "USD",
	"normalized_amounts_usd": {
		"loan_requested_amount_usd": "100000.00",
		"total_group_credit_usd": "250000.00",
		"ebitda_usd": "0.00",
		"credit_limit_usd": null
	},
	"normalized_data": {},
	"mode": "TEST",
	"policy_version": "v4-USD-verified-draft-2",
	"rule_results": [],
	"source_evidence": [],
	"missing_inputs": [],
	"conflicts": [
		"REQUEST_THRESHOLD_100M_VS_100K",
		"INCREASE_WAIT_6_VS_12_MONTHS"
	],
	"recommendation_path": "SIMULATED_REVIEW",
	"final_action": "NO_OPERATIONAL_ACTION",
	"human_review_required": true,
	"explanation": "Illustrative synthetic example only; not actual customer data or a lending decision.",
	"llm_error": null
}
```

## Actual workbook mappings and pending business questions

Inspection found revised local test workbooks with Query 1 **60 columns/30 rows**, Query 2 **11/90**, Query 4 **32/30**, plus excluded README sheets. The original fixed-name templates are absent locally, so the data-folder import currently returns 404; upload revised files manually. Source-row counts are not borrower or loan totals. See [README.md](../README.md#three-workbook-dataset) for the inventory.

Mappings deliberately absent or unresolved in these actual export headers:

- Query 1 `Requested_Amount`, `Requested_Product`, `Collateral_Status`, `Tier_I_Group_Credit`, `Tier_II_Group_Credit`, `Nonprofit_Flag` are **not** canonical aliases. `Account_Opened_Months_Ago` is not an opening date; `Identity_Mapping_Validated` and `Synthetic_Test_Case` confer no identity/policy authority. `Birth_Date__c`, `Number_of_Years_as_Current_Owner__c`, `NAICS_Code_Formula__c` are not silently mapped to DOB, ownership years or NAICS. `Type`/`Name` do not identify product or role.
- Query 1 exposes `Group_Credit_Amount__c`, but Tier I scope is unconfirmed/disabled. Verified revenue/net-income fields are not EBITDA. Actual new money, scoped Tier II, requested-path semantics and source currency/scale declarations still need contractual mapping where not canonical.
- Query 2 `SCOC__LCRDataSourceAnalysis__r` and its nested relationship columns are not the manifest's `SCOC__DataSourceAnalysis__r.Id`, `SCOC__LCREntity__r.Id` and explicit junction keys. A generic `Id` is not automatically an indicator/analysis ID. Preserve observations; obtain real scalar foreign keys and business key definitions rather than parsing guessed joins/pivots.
- Query 4 generic `Id`/`nFUSE__Account__c` do not establish the manifest's flattened report/account identity. Supply canonical CreditReport columns or approved exact aliases. `nFUSE__Status__c`/`nFUSE__Report_Category__c` are not silently the normalized status/category contract; report date and applicable party role remain explicit requirements. `nFUSE__Credit_Score__c`/`Business_Score__c` cannot establish personal FICO without supported personal typing. Recent bankruptcy filing date/discharged flag is not an explicit discharge date. `nFUSE__Total_Credit_Limit__c` has no current credit-limit mapping.

Business-owner questions still pending:

1. Which request boundary is intended: USD 100M or USD 100K? Which increase wait: six or twelve calendar months, and what exceptions?
2. What signed policy revision, effective date, approval reference and authorized review/exception process govern production use?
3. What are the exact scalar IDs, borrower/guarantor role definitions, group-credit scopes, request/response applicability, product/flag aliases, duplicate handling and indicator/characteristic semantics for the real exports?
4. Which report categories/statuses/dates establish personal versus business evidence, report recency, multi-owner/guarantor aggregation and bankruptcy discharge? What is the documented FICO/SBSS/ownership/amount conjunction?
5. What approved source fields, period and formula establish EBITDA? What are approved past-due severity, bankruptcy, industry and other risk thresholds beyond draft candidate diagnostics?
6. What compliance, fair-lending, adverse-action, privacy, audit, authentication and lender-authority integrations are required? None is represented as completed here.

## Rule catalog and saved-report compatibility

`Session.policy_rules` uses [backend/rule_catalog.py](../backend/rule_catalog.py), which reads the **v4** configuration, not the legacy policy configuration. The read-only panel lists actual v4 rule IDs with three-page source references, explicit graph/model safeguards, a variable guarantor family and readiness-only conflict entries. Separately categorized **Implementation guide** entries cite that guide's p.4/p.5 and describe strategy/stage diagnostics; they are not fabricated emitted v4 rule results.

The authoritative evaluated trace is **`report.underwriting.loan_results[].rule_results`**, not catalog readiness counts or the compatibility adapter. `enforced`/`conditional`/`blocked` describe readiness only, never evaluated outcomes. Both unresolved policy conflicts and undefined guide exceptions remain blocked; source safeguards being enforced does not mean the PDFs verified successfully.

Workflow recognition remains centralized in [backend/datasets.py](../backend/datasets.py): sample/revised/case/space/hyphen filename variants, sheet labels and distinctive headers can identify MTB. Recognition does not authorize aliases/joins. Exact bundled text examples are removed on upload/startup; custom/modified checks remain optional. Recognized MTB may process without optional checks. Startup invalidates stale generic-only reports by the existing migration conditions, but does **not** automatically upgrade every saved legacy policy report to v4. Reprocess old reports; never rewrite historical conversation claims as evidence.

## LLM boundary, privacy and validation

[backend/llm.py](../backend/llm.py) makes at most **one aggregate commentary call per v4 report**, with bounded normalized context: up to 20 loans, 40 rules and 40 evidence IDs globally, no raw documents, raw source values or conversation. Normalized USD amounts and loan IDs are included: this still constitutes external data processing. Commentary must keep `recommended_route=MANUAL_REVIEW`, null decision recommendation/confidence and human approval true; referenced loans, FAIL rule IDs and evidence IDs must come from supplied context. Invalid commentary is discarded. Engine explanations, paths, actions, evidence and decisions are never changed. Report-level provider errors leave deterministic reports available with `llm_error`.

Chat is a **separate**, broader data path: up to 12 recent messages at 2,000 characters each, recalled current-session memory/document excerpts, metadata, rules and report context may go to Anthropic. Storage/lexical embeddings remain local; do not call the app offline-only. Credentials remain backend-only. Never read/print/request secrets or use frontend `VITE_` credentials. Users configure their key manually in an editor and restart the API. Legacy `FOLIO_*` variables and persistence names remain supported.

Verified 2026-10-21: **544 backend tests, 91 frontend tests passed; frontend production build passed**. All three source hashes/page counts, 43-reason inventory and 19-entry expanded registry, candidate branches, source/money/null/conflict guards, guide stage/candidate separation, HTML/CSV diagnostics and escaping, catalog/v4-ID consistency, default TEST review, developer synthetic approval and production manual-review paths were checked locally. Guided actions cover no-provider validation, revision-bound confirmation, concurrency locks, scoped evidence, restart persistence and unverified notes. Anthropic responses are mocked in tests; live credentials, production authority and operational lender integrations were **not** tested.