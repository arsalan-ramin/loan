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
| `TEST` (new-session default) | `SIMULATED_REVIEW`; only qualified developer-injected synthetic fixtures can yield `SIMULATED_APPROVE_PATH` or `SIMULATED_DECLINE_PATH` | `NO_OPERATIONAL_ACTION` |
| `PRODUCTION` (explicit opt-in) | `DATA_QUALITY_HOLD` for graph/identity/conflicting-data defects; otherwise `POLICY_REVIEW_REQUIRED` | `MANUAL_REVIEW` |

`human_review_required` is always true. New and legacy sessions with no stored mode default to `TEST`; an explicitly stored `PRODUCTION` selection is preserved. Model validation rejects production simulated paths, operational approval/denial and mode/action inconsistencies. Missing inputs/ambiguous selections force synthetic review; a failed draft diagnostic is not a loan denial. Production ignores synthetic criteria and retains the two default conflicts. The default public TEST configuration is not synthetic-ready and produces `SIMULATED_REVIEW` for explicit loans, even with otherwise complete fixture data. TEST is simulation-only and cannot take operational action; developer-only fixture overrides are not available through the public API/UI.

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

The manifest supports **exact canonical sheets** and **explicit flattened aliases across the three recognized MTB source workbooks**: Query 1 (Loans, Accounts & Financials), Query 2 (SBSS & Entity Relationships), and Query 4 (Credit Reports). The supplied relationship map specifies the SOQL joins: Query 1 and Query 2 Account IDs come from `SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r.LLC_BI__Account__c`; Query 4 Account IDs come from `nFUSE__Account__c`. These query-specific keys are matched exactly after case/punctuation normalization, and their Account ID values must agree across files to connect records. Other Account-looking columns (including generic `ACCOUNT_ID`, a different object’s `LLC_BI__Account__c`, or a nested `...Account__r.Id`) are retained as source attributes but are not substituted for these SOQL join keys. Generic `Id`, account names, row order, and fuzzy header guesses do not create joins. Canonical worksheet mappings retain their own explicit manifest aliases. Attributes without explicit account/loan links remain source evidence and do not create associations.

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

The supplied data-model screenshot's explicit ERD labels are also accepted as aliases for corresponding canonical graph IDs/foreign keys, including canonical Loan `ACCOUNT_ID`, `LOAN_ID`, `LCResponse_ID`, `LCREntity_ID`, `LCRDataSourceAnalysis_ID`/`LCDataSourceAnalysis_ID`, and characteristic `LCCharacteristic_ID`, `Characteristic_Key`, `Characteristic_Value`. A Loan's explicit Account foreign key supplies its borrower link; an optional `LLC_BI_LEGAL_ENTITIES__C` junction represents additional borrower/guarantor links only when its role is explicitly mapped. For flattened Query 1/2 rows, the Entity Involvement Account key is associated to the Loan only with the explicit borrower/guarantor role; the Account FK alone does not force the role to borrower. Query 4 report Account links are account-scoped by their own exact `nFUSE__Account__c` key. The credit-report labels `CREDIT_SCORE` and `NO OF JUDGEMENTS` are retained as raw score/judgment-count fields. A generic credit score is **not** personal FICO without explicit report category and party role; a judgment count is not the separately named maximum-delinquency-public-records code. These aliases support both exact canonical table names and recognized flattened MTB rows. They do not infer missing IDs, relationships, applicant roles, or business meaning.

### Observed flattened SOQL export contract

The uploaded `*_Validated_Revision_SOQL.xlsx` workbooks were inspected directly. Their fully qualified SOQL paths are mapped by exact normalized terminal-field allowlists; the mapper does not fuzzy-match similar labels. Generic root `Id` is scoped by each query's verified root signature: Query 1 → EntityInvolvement/junction, Query 2 → Indicator, Query 4 → CreditReport. It is **not** broadcast to every entity in a row. Nested `...LLC_BI__Loan__c` and `...LLC_BI__Account__c` are explicit foreign keys; `...LLC_BI__Borrower_Type__c` supplies the explicit party role. Query 1 and Query 2 therefore resolve loan/account borrower links directly.

Recognized Query 1 nested attributes include the loan's `LLC_BI__Product__c`/`LLC_BI__Product_Type__c` and `LLC_BI__Amount__c`; account `Name`, `Birth_Date__c`, `Number_of_Years_as_Current_Owner__c`, `NAICS_Code_Formula__c`, revenue/net-income and SBSS financial fields; nested `nFUSE__Credit_Report__r.Id`, status, report category, score, past-due and related credit fields; and characteristic key/value. Query 1 root `Id` is the EntityInvolvement/junction record ID, not a characteristic ID. Characteristic has no source record ID or analysis FK in this extract, so it is stored under a local provenance key. When the same Query 1 row explicitly supplies a Loan ID, Account ID and recognized Entity Involvement party role, a characteristic may be grouped as that row's source observation; this does not fabricate an analysis or SBSS-response relationship. Query 2 recognizes `SCOC__LCResponse__r.SCOC__SBSS7Score__c`, plus root indicator `Id`, `SCOC__IndicatorKey__c` and `SCOC__IndicatorValue__c`. The SBSS value is attached to the explicit Loan FK. Because these exports do not supply a scalar response record ID, the engine does not fabricate one. Query 4 maps root `Id` to CreditReport and `nFUSE__Account__c` to its Account FK, with `nFUSE__Credit_Score__c`, `nFUSE__Status__c`, report category, and other known report attributes.

Query 1 `SCOC__CharacteristicKey__c` / `SCOC__CharacteristicValue__c` pairs are exposed together in the local report as raw observations when the same row explicitly identifies a Loan, Account and party role. Values such as `FileEstablishedDate=19960700` remain lexical text: the application does not parse them, infer policy fields, or use them as report/as-of dates. Observations are grouped only by explicit account/loan links and response/report IDs when those links are present and validated; missing IDs remain null and observations are never overwritten by key. Raw characteristic values are excluded from Anthropic report/chat context under the external-processing boundary. They are displayed locally for an authorized human reviewer; no LLM-derived field mapping or lending decision is produced.

Every loan report includes the fixed `decision_status` value **Human Review**. This application cannot issue `Approved` or `Denied`; underlying candidate routes and diagnostics remain separate from this status and authorization is always `NOT_ISSUED`.

Each root `Id`, nested entity ID and foreign key retains its source-cell evidence. Query 2 Indicator has its root ID and key/value data but the inspected extract omits the `DataSourceAnalysis` foreign key; Query 1 Characteristic omits both a characteristic record ID and analysis FK. These rows are retained under local provenance and shown as unresolved relationships, never joined by order/key. `nFUSE__Credit_Score__c` is only eligible as personal FICO when the selected report is explicitly Personal and its borrower/guarantor role is supported; the screenshot's average uses typed scores from all explicitly linked parties and is unknown if a required party score is missing.

The inspected revision has no explicit loan as-of date, credit-report date, scalar SBSS response ID/score date, EBITDA, maximum-delinquency public-record code, or bankruptcy discharge date. EBIT, revenue, filing date, `Number_of_Public_Records__c`, and a discharged boolean are not substituted for those canonical inputs. Mapping diagnostics therefore report **unmapped source headers**, **unavailable canonical inputs**, and **unresolved relationships** separately from per-loan missing values.

The inspected Query 1 nested credit score and Query 4 root credit score disagree for **11 of 15 shared report IDs**. This is treated as `CONFLICTING_DUPLICATE:CreditReport:fico`; neither export is preferred. The conflict holds the batch until corrected source data is supplied. No normalization change can repair contradictory source records.

Relationships follow the ERD cardinalities: Account→Loan is one-to-many through `Loan.ACCOUNT_ID`; the legal-entity junction is many-to-many and supports explicitly typed additional parties; Account→CreditReport, Loan→LoanExtension, Loan→LCRESPONSE, Account→LCREntity, response→LCREntity, entity→DataSourceAnalysis, and analysis→Indicator/Characteristic are one-to-many, except Loan→LoanExtension is constrained to at most one extension per loan. Multiple loan responses are accepted; use an explicit `Loan.response_id` when supplied, otherwise select the unique latest response only when each candidate has a valid `score_date` and the latest date is unambiguous. Without those dates, an ambiguous set stays review. Each report remains account-scoped, and each characteristic/indicator stays attached to its own analysis/entity/response/account. Flattened examples include `Loan_Request_Id`/`LLC_BI__Loan__c`, `LLC_BI__Account__c`, `SCOC__LCResponse__r.Id` or a valid scalar `SCOC__LCResponse__r`, and explicit `borrower_role`/`LLC_BI__Borrower_Type__c`. The checked-in manifest is the full allowlist, not a claim that every source field has confirmed business semantics.

IDs must be explicit opaque lexical scalars (letters/digits followed by letters/digits/underscore/hyphen, maximum 128 characters); not objects, numbers, formulas or relationship repr strings. Canonical direct borrower IDs are explicit links. A flattened Account ID alone is not a borrower role. `Identity_Mapping_Validated` is ignored. No row-order, same-name, convenient-score or filename-based join is invented. Duplicate conflicts preserve evidence, poison the field and hold rather than overwrite; orphan links and multiple borrowers/extensions are reported. Batch graph defects intentionally hold **all** loans, not only a guessed affected subset. ID-less observations may use local provenance storage keys, never fabricated Salesforce record IDs.

**Report selection:** explicit ISO `as_of_date` is required. Reports are scoped to the account. Completed statuses are `completed`/`complete`; known incomplete statuses are `pending`, `in_progress`, `failed`, `cancelled`. Missing/unknown completion status prevents selection. Completed reports require valid unambiguous dates. Choose the unique latest completed report on or before as-of; ties, ambiguous/missing dates or no applicable report remain review conditions. There is no guessed current date or approved maximum report-age rule.

**Personal FICO:** the screenshot says average personal FICO. Compute an exact arithmetic mean only across the borrower's and explicitly linked guarantor accounts, each using its independently selected, account-scoped report. Accept explicitly typed `personal_fico`, otherwise require `report_category` personal/consumer/personal_fico **and** matching `report_role` borrower/guarantor before using `fico`. If any linked party's personal score is missing, untyped, ambiguous, or not account-linked, the average is unknown rather than a partial average. Business/untyped scores and unrelated accounts never substitute.

**Response/indicators:** choose the explicitly applicable response or a unique linked response; mismatched loans or missing borrower entity links remain unknown/held. Indicators/characteristics stay one-to-many, scoped through analysis → entity → selected response and borrower account. Both feature-mapping dictionaries default empty. No automatic business-key pivot; money, IDs and credit-score targets are not supported indicator substitutions. Conflicting observations remain conflicts.

**Explicit observation grouping contract:** `normalized_data.observation_groups` is built before optional feature interpretation. Each group carries `account_id`, `response_id`, `entity_id`, `analysis_id`, nullable `report_id`, parent `evidence_refs`, and separate one-to-many `indicators` / `characteristics` arrays. Every observation retains these parent IDs, its actual `record_id` (null for local provenance-only storage), lexical `key`/`value` and source-cell `evidence_refs`. Groups are isolated by the full parent tuple, not collapsed by characteristic key. Multiple analyses under one response remain separate. Only borrower entities whose explicit response belongs to this loan (or whose loanless response is explicitly referenced by this loan) participate. Historical/ambiguous-response groups remain local review data; they never replace the selected response.

The optional **canonical DataSourceAnalysis** fields `response_id` (exact alias `LCResponse_ID`) and `report_id` (exact alias `CREDIT_REPORT_ID`) must reference existing graph records. An explicit response must agree with the entity's response, and an explicit report's Account must agree with the entity's Account. Conflicting parent aliases/duplicates, invalid scalar IDs, orphan FKs and cross-account/response mismatches hold the batch and block the affected group. No report ID is inferred from Account, report dates, a nearby row, or the selected report. These optional FKs are not new flattened-export aliases; direct Characteristic report/response shortcuts are not supported. Canonical characteristic fields also accept exact `SCOC__CharacteristicKey__c` / `SCOC__CharacteristicValue__c` spellings.

Existing `normalized_data.indicators` / `characteristics` arrays retain selected-response semantics with additive parent IDs. If a group has an explicit report FK, only the selected borrower report may participate in these arrays or optional feature mappings. Unknown keys never pivot to features. Strings such as `N`, `19960700`, `false`, `0005` and surrounding whitespace are preserved exactly, not interpreted as flags, dates or numbers. Finite Excel numeric observation scalars are represented as lexical strings (`5.0` stays `"5.0"`); original Excel formatting/leading zeros already lost by numeric-cell extraction cannot be reconstructed. Native boolean observation cells become `"true"`/`"false"`; nonfinite/complex values have null display values. This exception is limited to observation display/evidence: float money remains rejected. Unscoped observations remain in the graph with provenance/mapping warnings, never assigned a guessed group or decision input. Group details are React-escaped local report data and included in JSON/HTML normalized diagnostics, **not sent to the LLM**. Existing stored reports require reprocessing; report dates and policy are unchanged.

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

The screenshot alone defines the simulated strategy criteria: its six knockout candidates (public-record codes 1–3, negative explicit EBITDA, bankruptcy discharged less than five years ago, age under 18, average personal FICO below 650, and SBSS below 160), followed by its SBSS/ownership/average-FICO/amount/NAICS tree. The average uses only explicitly linked borrower/guarantor accounts with typed personal scores; it remains unknown if any linked party's score is unavailable. In the SBSS 160–189 / ownership-under-two / average-FICO-650–759 branch, the screenshot contradicts itself: the knockout list says requested amount above USD 100K matches, while the chart says SBA candidate through USD 100M. Therefore amounts above USD 100K through USD 100M are `POLICY_REVIEW` in this branch; amounts at/below USD 100K and above USD 100M follow the matching screenshot labels as simulated candidates. The conflict remains `REQUEST_THRESHOLD_100M_VS_100K`. For any SBA candidate node, the screenshot explicitly says a verified vehicle-only loan is overridden to the conventional candidate; if `vehicle_only` is missing, the branch remains review. PRODUCTION remains `POLICY_REVIEW_REQUIRED`/`MANUAL_REVIEW`; no screenshot condition produces actual approval or denial.

## Complete implementation-guide diagnostics

The separate [seven-page implementation guide](../backend/data/MTB_LLM_Underwriting_Complete_Implementation_Guide.pdf) is pinned to SHA-256 **8a409bf6499129be8efe109d1a15f21ecfd95929b0674b993ab6d13c9c8e9467**. It remains a draft, independently verified by file and hash; it does not replace the three-page v4 source or confer authority. Batch fields `implementation_guide_source_document`, `implementation_guide_source_sha256` and `implementation_guide_available` record its identity and verification. Missing/mismatched guide evidence forces guide policy review.

Section 8 (p.4) supplies the simulated SBSS/ownership/average-personal-FICO/amount/NAICS tree after explicit source and identity checks plus the six screenshot knockouts. The affected 160–189 amount interval stays in policy review where the screenshot conflicts. The screenshot's vehicle-only note overrides any SBA candidate node with a conventional candidate only when `vehicle_only=true` is explicit; unknown status holds. V4 policy gates still remain `UNKNOWN`; screenshot candidate matches do not authorize lending actions.

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
| FICO/vehicle/request conflicts | Screenshot average uses all explicitly linked borrower/guarantor typed personal scores; any missing party score leaves average unknown. No other-account substitution. Explicit vehicle-only=true converts screenshot SBA candidate nodes to a conventional candidate; unknown vehicle status holds. The affected 160–189 amount branch retains USD100K/USD100M policy review; BALOC increase remains a separate diagnostic and does not change screenshot branch selection. |
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

### Selected-loan Claude rationale

The browser automatically runs local preflight after a successful upload, retaining the uploaded session if validation fails. Upload never starts paid processing. Validation retries, downloads, evidence tools and review notes are grouped under expandable sections. After report processing, **selecting a loan directly starts its Claude review** through the existing confirmed/revision-bound endpoint. The selector's adjacent, accessible disclosure identifies Anthropic, normalized context and potential per-selection charges; the explicit selection confirms that request, with no separate toggle or review button. Opening a report makes no request and starts with an unselected dropdown. Saved reviews are reused and attempts are deduplicated per loan/report while mounted. Failures do not retry automatically; retry requires explicit confirmation. Initial report processing still requires its separate explicit confirmation. Prepare review is hidden once a report exists.

Selected-loan requests now require a structured reconciliation for every UNKNOWN check included within the bounded context. Claude classifies each as mapped-but-blocked, missing/unverified, conflicting, or policy-unresolved, with a rationale, next step and up to three rule-scoped evidence IDs. Coverage must be exact (no duplicate/omitted checks); unsupported citations and mapped-but-blocked claims without citations are discarded. The UI presents these beside the rationale. These classifications are AI commentary, not verified evidence or rule clearance; no missing dates, EBITDA, report-selection contracts or policy conflicts are inferred away. Existing saved reviews without reconciliation remain readable; a newly confirmed provider request is needed for the new assessment and may incur charges.

After a completed v4 report, choose the loan and use **Review selected loan with Claude**. Selection itself makes no provider request. A separate confirmation discloses Anthropic processing and possible charges. `POST /api/sessions/{id}/assistant/loan-review` accepts exactly `loan_id`, `report_id`, `expected_updated_at`, and `confirmed=true`; it rejects stale revisions, missing/unrelated loans, processing sessions and extra configuration fields.

This is a separate loan-focused advisory explanation, **not an LLM lending decision engine**. Scope is narrowed to the selected loan before applying the existing 40-rule/40-evidence-ID budgets. Claude receives normalized features/amounts, fixed rule/strategy diagnostics, conflicts and source-integrity status, not raw Query 1/2/4 rows, unmapped attributes, source values, chat or retrieved memory. It cannot assess fields omitted by the mappings or choose between conflicting policy sources. Output must retain MANUAL_REVIEW, null decision recommendation/confidence and required human approval; unsupported loan/rule/evidence citations are discarded. Risk/supported factors, next evidence and rationale are shown as escaped commentary. This does not verify narrative factual accuracy independently.

Validated output is stored in `Session.loan_ai_reviews`, bound to the original loan/report, at most 20 reviews per session. It is not added to report findings, source evidence, underwriting exports, retrieval memory or rule inputs. Only reviews matching the current report and selected loan are displayed. Requests run outside the persistence lock and check the revision again before saving; concurrent changes discard the response (the provider call may still be billable). Provider failures return explicit HTTP errors and leave all authoritative reports unchanged. Existing aggregate report commentary remains one bounded call per processing run; this additional call occurs only on explicit selected-loan confirmation.

See [Business banking workflow alignment](business-banking-workflow.md) for the user-provided existing-process diagram, the required processing sequence, and explicit gaps. In particular, its USD 250K group-credit eligibility filter is not equivalent to the existing draft's USD 250M Tier I diagnostic. Four-query ingestion, explicit real-estate-security mapping, historical decision accuracy validation and approved adverse-action mappings are not currently implemented; do not claim full diagram compliance.

Large XLSX uploads are parsed in read-only mode directly from disk, with an incremental SHA-256 hash instead of a second full compressed-file byte copy. The parser accepts up to 5,000,000 visited cells and 512 MB expanded ZIP contents, retaining the 30-sheet and 50-million-character extracted-text limits. Local validation skips the redundant text dump but retains source rows and cell provenance. These are bounded capacities, not unlimited or constant-memory ingestion: extracted records and graph evidence still use memory. Compressed uploads retain the default 50 MB limit (`FOLIO_MAX_XLSX_MB` can configure it). Files exceeding a budget fail explicitly; rows are never silently truncated.

Missing field mappings do not block a review of a valid three-workbook set. Agent continues with the evidence it can map and leaves affected checks UNKNOWN. If no borrower accounts can be mapped, account selection is not required: Prepare review produces a diagnostic-only report, with no fabricated loan rows or relationships and no lending authorization. When borrower candidates exist, explicit account selection remains required. Unreadable files and missing/duplicate Query roles are separate input problems, not mapping warnings, and still require correction.

Guided conversation cards offer local preflight, extracted-attribute inspection, account discovery/selection, saved loan inspection, explicit process confirmation and report-bound review notes. `POST /preflight` scans all three source workbooks and preserves every populated row attribute with sheet/row/cell provenance without materializing the redundant full-workbook text dump; it does not run credit rules or call a provider. Exactly one Query 1, Query 2, and Query 4 workbook is required. Cross-file links use matching explicit Account ID column values normalized against a fixed header alias allowlist. Conflicting account-key aliases, missing IDs, and unmatched account values remain validation issues; row order, borrower names, and fuzzy header guesses are never joins. Generic non-MTB sessions retain their existing workflow.

Local `assistant/inspect` requests take a current-session loan ID and one allowlisted topic; results carry report ID, up to 12 prioritized rules and 12 source cells with exact lexical values. They persist as historical `local_assistant` messages, not fresh rule evidence. `assistant/process` requires `confirmed=true` and current `expected_updated_at`; it shares existing processing locks and rejects stale confirmation. The original bodyless process API remains supported. No model tool dispatch or automatic processing is added.

Review notes/acknowledgments are stored separately from `Report`, bound to report/loan IDs, labeled unverified assertions and never fed into graph/rule evaluation. Missing business fields still require corrected source uploads; policy decisions require an authorized external process, not chat assertions. `ChatRequest.loan_id` optionally focuses the bounded report context on one existing loan; same-session history/metadata/recall remain broader and are disclosed as such. Ordinary chat failures remain explicit; deterministic local actions do not masquerade as Claude output.

The MTB **Review mode** selector defaults missing mode to TEST. New sessions start in TEST; PRODUCTION requires an explicit selection. The selector is disabled during processing/pending operations, sends only mode, applies the returned invalidated session on success and keeps the prior state on failure. Mode changes clear reports/steps and require **Process documents** again. Upload/chat does not start processing. V4 reports offer HTML/JSON/CSV; generic reports offer HTML only. Export failures show an error, retain deterministic results and re-enable retry.

| Method and full path | Contract |
| --- | --- |
| `POST /api/sessions` | JSON `{ "title": "Optional title", "mode": "TEST" }`; omitted mode defaults TEST; select `PRODUCTION` explicitly for manual-review routing |
| `PUT /api/sessions/{id}/mode` | Exactly `{ "mode": "TEST" }` or `{ "mode": "PRODUCTION" }` |
| `POST /api/sessions/{id}/documents` | Multipart `files`; an MTB review accepts exactly three XLSX sources, one each for Query 1, Query 2, and Query 4 |
| `GET /api/sessions/{id}/accounts?q=prefix&offset=0&limit=50` | Paginated, local account/client search; requires MTB preflight |
| `PUT /api/sessions/{id}/account` | Exactly `{ "account_id": "..." }`; must be a discovered borrower candidate |
| `POST /api/sessions/{id}/dataset` | Copies the fixed allowlisted Query 1/2/4 files into an empty session; joins use explicit Account ID values |
| `POST /api/sessions/{id}/process` | No body, returns 202 after MTB validation/account-selection guard; poll `GET /api/sessions/{id}` |
| `GET /api/sessions/{id}/underwriting?format=json` | Batch JSON; JSON is also the default without query parameter |
| `GET /api/sessions/{id}/underwriting?format=csv` | One loan/row; nested amounts, rules, evidence and issues encoded as JSON strings |
| `GET /api/sessions/{id}/report` | Escaped standalone HTML |

Strict uppercase modes; create/mode reject extra fields with 422, including `authority`, `approved`, `test_policy`, `policy_config`. Mode/other edits while processing return 409. Export needs a completed v4 report (409 otherwise; 404 unknown session; 422 unsupported format). JSON preserves exact `Decimal` strings. CSV guards formula prefixes and yields headers only for empty batches; JSON retains empty-batch issues. API details are implemented in [backend/main.py](../backend/main.py), frontend download/mode contract in [frontend/src/api.ts](../frontend/src/api.ts).

### Public TEST walkthrough

**Illustrative synthetic workbook contract only — not actual data.** Prepare three XLSX workbooks, one for each source role below. Each canonical sheet has its exact headers and synthetic records. Cross-file relationships are linked by explicit matching Account IDs, never row position or name. These are **contract examples**, not claims that a source export already contains these canonical columns. Supply money as lexical text cells; the table describes worksheet rows, not a JSON upload interface to the graph.

| Workbook role | Canonical sheets | Illustrative records |
| --- | --- | --- |
| Query 1 — Loans, Accounts & Financials | Account; Loan | Account `Id=A1`, `dob=1980-10-20`, `is_nfp=false`, `ownership_years=2`, `naics=541100`, `ebitda_usd=0.00`; Loan `Id=L1`, `ACCOUNT_ID=A1`, `as_of_date=2026-10-20`, `product=express_baloc`, `requested_path=quick_app`, `loan_requested_amount_usd=100000.00`, `new_money_usd=100000`, `tier1_group_credit_usd=250000`, `tier2_group_credit_usd=249999.99`, `secured=false`, `is_startup=false`, `is_investment_re=false`, `is_increase=false` |
| Query 2 — SBSS & Entity Relationships | LCRESPONSE; LCREntity; DataSourceAnalysis; Indicator; Characteristic; EntityInvolvement | Response `Id=S1`, `loan_id=L1`, `sbss=200`, `score_date=2026-10-19`; entity/account relationships use `Account_ID=A1` and explicit response/entity/analysis IDs |
| Query 4 — Credit Reports | CreditReport | Report `Id=R1`, `ACCOUNT_ID=A1`, `status=completed`, `report_date=2026-10-19`, `personal_fico=760`, `ofac_disposition=cleared`, `ofac_alert=false`, `credit_freeze=false`, `bankruptcy_count=0`, `total_past_due_usd=0` |

Use string cells for IDs, dates, NAICS and numeric lexical inputs, boolean cells for flags; set `currency=USD` and `amount_scale=actual_dollars` explicitly if overriding config defaults. Account IDs must match across files; supported Account ID header variants are normalized. Entity/analysis/observation links also need their explicit IDs. Unlinked rows remain extracted source attributes but are not joined. See the [PowerShell API example](../README.md#example-public-test-invocation): create a TEST session, upload all three role files together, validate, select a borrower account, process, poll, then export. Public mode alone does not inject synthetic criteria, so this remains review-only with no operational action.

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

The inspected `MTB_Query_{1,2,4}_Validated_Revision_SOQL.xlsx` exports each contain 15 populated records. Their SOQL root IDs are mapped per query role, nested Loan and Account foreign keys are explicit, and the known source fields listed above normalize to canonical values. Fifteen rows are not assumed to equal fifteen eligible applications; names, row positions, and generic root IDs never create joins.

Remaining source-data gaps and conflicts:

- Query 1 provides explicit Loan/Account foreign keys and borrower role, product, amount, DOB, years as current owner, NAICS, revenue, net income, and balance-sheet values. It does not provide screenshot-required explicit EBITDA, new-money/group-credit tiers, as-of date, account-opened date, security/vehicle flags, or an approved requested-path alias. EBIT, revenue, and net income are not substituted for EBITDA. `Identity_Mapping_Validated` remains ignored.
- Query 2 provides a scalar SBSS value on a row explicitly linked to Loan/Account and explicit indicator key/value fields. It does not provide a scalar SBSS response ID, score date, DataSourceAnalysis/LCREntity IDs or Indicator/Characteristic `analysis_id`; therefore the score is retained as a loan-linked observation with no fabricated response ID, while unscoped indicators/characteristics remain preserved evidence and are not pivoted into decision inputs.
- Query 4 root `Id` is the CreditReport ID and `nFUSE__Account__c` is its Account FK. Status, report category and generic credit score are mapped. Query 4 does not provide report date; it also does not itself type the report party, though Query 1's explicit borrower-type path can supply party role when the same report ID is linked.
- The inspected files have **11 of 15 duplicate CreditReport IDs with differing credit-score values** between Query 1's nested report data and Query 4's root report rows. The graph flags `CONFLICTING_DUPLICATE:CreditReport:fico`, poisons that score and holds the batch; it does not choose one workbook's value.
- `Number_of_Public_Records__c` is retained but not mapped to `max_delinquency_public_records` without confirmation that its codes mean the screenshot's maximum-delinquency public-record codes. `nFUSE__Recent_Bankruptcy_Filing_Date__c` and `nFUSE__Bankruptcy_Discharged__c` do not establish a discharge date. `nFUSE__Total_Credit_Limit__c` remains unmapped to current credit limit pending scope/meaning confirmation.

Preflight and underwriting exports now separate **unmapped source headers**, **canonical inputs not present in the files**, **unresolved relationship mappings**, and per-loan missing/conflicting values. These statuses are intentionally not conflated: a source header can be preserved but unmapped, a canonical input can be absent, and a mapped field can still conflict or contain invalid values.

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