# Agent — document intelligence workspace

A **FastAPI + React/TypeScript** application with local **SQLite and ChromaDB** persistence, deterministic document extraction and rule decisions, and **Anthropic Claude** chat and report explanations. Chat context and document evidence are sent to Anthropic; this is not an offline-only application.

## Quick start

For step-by-step UI/backend startup commands, first-time setup, stopping/restarting
and troubleshooting, see [docs/RUNNING.md](docs/RUNNING.md).

Requirements: **Python 3.11–3.13**, **Node.js 22.12+** and npm. Dependencies are already installed in this workspace. The Python environment is `.venv`.

Configure Anthropic before chatting:

1. Manually copy [backend/.env.example](backend/.env.example) into a local backend environment file named `.env`. No local environment file is assumed to exist; do not overwrite an existing one.
2. In your editor, set `ANTHROPIC_API_KEY` to your own Anthropic API key (the example leaves it blank). **Never paste the key into chat**, frontend code, or a committed file. Credentials stay backend-only; never use a `VITE_` variable for them.
3. The example sets `ANTHROPIC_MODEL=claude-sonnet-5-5`. Change `ANTHROPIC_MODEL` if you need another model available to your account.
4. Choose **Terminal → Run Task → Agent: Start** to run both services. If the API is already running, terminate and restart **Agent: API** after changing the environment. Individual tasks **Agent: API** and **Agent: UI** are also available.

Chat requires a valid API key, model access, and billable network access to Anthropic. Without a working provider, deterministic reports remain available with an explicit explanation warning; chat does not silently fall back to local answers.

- React workspace: http://127.0.0.1:5174
- FastAPI interactive API documentation: http://127.0.0.1:8010/docs
- API health: http://127.0.0.1:8010/api/health

These ports intentionally avoid another local project using ports 8000 and 5173. The frontend proxies `/api` to the backend. If changing ports, update the VS Code tasks, Vite proxy, and backend CORS settings together. Always run tasks from the repository root.

For a fresh checkout, create a Python virtual environment, install [backend/requirements.txt](backend/requirements.txt) into it, and run npm install inside [frontend](frontend). Then use the tasks above. On non-Windows systems, change the task interpreter path from `.venv/Scripts/python.exe` to `.venv/bin/python`.

## Using the workspace

1. Open the UI and ask the assistant a question or create a session. The assistant asks for the required documents first.
2. Upload XLSX, PDF, DOCX, UTF-8 TXT, Markdown, CSV, or JSON files, or select **Use 3 Excel files from data folder** in a new/empty session. You can also try [examples/sample-policy.txt](examples/sample-policy.txt). The backend imposes **no per-file byte cap or general document-count cap**. Nonempty files are required. The MTB contract still requires exactly three XLSX source workbooks (Query 1/2/4) for a complete review, permits incremental uploads up to that set, and forbids mixing XLSX with other document formats. Frontend labels and client-side restrictions are separate and unchanged by the backend limit removal.
   - **Stored loan data:** once a session holds a complete Query 1/2/4 set (exactly three XLSX workbooks, one per query), the backend **clears all previously stored data and files** and indexes the new set in the background into `loan_dataset.sqlite3` (workbook copies in `loan_dataset/`) under the data directory, keyed by loan ID and borrower account ID. An incomplete or unrecognized set never clears existing stored data. When indexing finishes, any user can select **Use stored loan data** to start a session from the stored workbooks without uploading again. Loan loading uses the index whenever a session's workbooks are byte-identical to the stored set and otherwise falls back to streaming the XLSX files, with identical results. If the mapping configuration or loader code changes, the store is marked stale and re-indexed automatically at the next API start.
3. For recognized MTB workbooks, **Review mode defaults to TEST — simulated paths only**. TEST cannot take operational action; with the public default configuration it produces `SIMULATED_REVIEW` unless explicitly enabled developer-only synthetic criteria are supplied. **PRODUCTION — manual review only** remains available as an explicit mode. Changing mode clears the report and current validation and requires reprocessing. The selector is disabled during processing or another pending action; errors retain the prior mode/report. **Edit document checks** manages optional text checks separately. Exact bundled examples are removed from MTB sessions; modified/custom checks are retained. **View guideline rules** displays the current v4 catalog and separately categorized guide/expanded diagnostics. Readiness counts are not evaluated loan outcomes.
4. For XLSX inputs, enter the exact **Loan number** and select **Load loan data**, or reply with `loan number L1` in chat (using your actual loan number). Uploads are saved without automatically parsing their contents. The local reader streams workbook rows and retains only rows connected to that loan through explicit IDs, including related borrower, guarantor, financial and credit-report evidence. It may scan the files more than once to resolve relationships regardless of row order, but never builds an in-memory dataset of all loans. Then select **Process documents**. Uploading and chatting never automatically start processing.
5. Follow the five-agent pipeline. Open **Review report** to inspect each finding and its evidence.
6. Select **Download HTML** for a standalone report, or **Download JSON / Download CSV** for completed v4 underwriting results. Exact USD values remain strings, including in nested CSV JSON cells. The UI downloads exports as untouched blobs rather than converting money through JavaScript numbers. Export errors retain the report and allow retry. Generic reports have HTML only.
7. Ask about metadata, the decision, phrases in your documents, or earlier messages. Reopen a saved session from the sidebar after restarting the app.
8. **Batch review** (sidebar) processes the whole stored Query 1/2/4 set in one run. **Start batch review** asks for confirmation, then gives every stored loan an **Approved / Decline / Refer** status from the deterministic TEST decision flow plus a Claude rationale, using the same bounded review as a single loan. That is **one paid Claude call per loan**. The screen polls a progress bar with per-status counts and lists results with a status filter. **Cancel** stops after in-flight loans finish. **Resume remaining** processes loans not yet reached. Each record shows a **Processing** status of **Succeeded** or **Failed**, and the filter can show only succeeded or failed records. A failed record has its own **Retry** button. You can also tick failed records (or all failed records on the page) and select **Retry selected**, or use **Retry all failed**. A retry reprocesses only those loans in place, shows them as **Retrying** with their own progress, and confirms the paid calls first. **Download Excel** exports `Loan ID`, `Account ID`, `Status` and `Rationale / result`. Results are saved in the stored-dataset database. Only the latest run is kept, and replacing the stored data clears it. A server restart marks a running batch as interrupted so it can be resumed. A provider configuration or authentication failure stops the run; other per-loan Claude failures are recorded and the run continues.

## Guided assistant workflow

The conversation now includes **Your next safe step** action cards:

1. After upload, the assistant asks for a **Loan number**. **Load loan data** validates only the requested loan's related records without Anthropic, Chroma indexing, a new report or any provider charge. An unknown loan is reported explicitly; no other loan is substituted. Enter another number to reuse the uploads for a different loan. **Validate files locally** reads only workbook headers until a loan is selected; afterward it runs scoped diagnostics and shows the first ten prioritized issues. Counts describe the selected scope, not the full portfolio. Generic inputs are labeled as not underwriting-validated. Existing reports remain untouched by validation.
2. Correct the source workbook, remove superseded uploads and add corrected files. Current validation is invalidated on file/rule/mode changes; one prior summary is retained for input-hash and issue-count comparisons. Counts are not credit outcomes or proof that a specific issue was resolved. There is no chat-to-evidence amendment or automatic field mapping.
3. **Prepare review** displays a confirmation card explaining external Anthropic processing, possible charges and report replacement. **Confirm and process** submits an allowlisted action bound to the current session revision; stale confirmations and concurrent processing are rejected. The existing direct **Process documents** control/API remains an explicit-start alternative. No upload, preflight, chat response or model output autonomously starts processing.
4. For completed underwriting reports, select a **Loan to explore**. **Why is this loan held?**, **Inspect loan rules** and **Show source cells** are deterministic local lookups of the saved report, including loans beyond the aggregate LLM context's first twenty. Bounded inspection cards retain report IDs and source-cell provenance; they are historical snapshots, not recalculated evidence. Open the full report for the complete trace.
5. **Draft a question for Claude** fills the composer with a selected-loan question but does not send it. **Send** makes the normal billable chat request with the report context focused on that loan. Current-session messages, document metadata and memory recall may still include other loans; this is a focus control, not a privacy/isolation boundary. Invalid or other-session loan IDs are rejected.
6. Record a note or acknowledgment against the selected loan/report. These persist with `user_assertion_not_verified_evidence` provenance, never clear a rule, resolve policy conflicts, verify a source or issue authorization. They are not a reviewer identity/signoff service; the session remains a local unauthenticated demo. Old-report notes remain stored but do not apply to a replacement report. At most 100 notes per session.

Local guidance is deterministic and explicitly labeled, not a silent fallback for failed Anthropic chat. The LLM can explain available actions but cannot dispatch tools, edit files, change mode/policy or authorize lending. Existing controls remain available; mode changes use the explicit mode selector, not model instructions.

## Agent architecture

### Three-file intake and account relationships

Upload **Query 1 (Loans, Accounts & Financials), Query 2 (SBSS & Entity Relationships), and Query 4 (Credit Reports)** for an MTB review. The files link only through matching explicit Account ID values. Supported headers include `LLC_BI__Account__c`, `ACCOUNT_ID`, `Account ID`, and `nFUSE__Account__c`; punctuation/case variants use a fixed allowlist. Generic `Id`, account names, row position, and fuzzy header guesses are not join keys. Every populated attribute and source-cell location on retained loan-related rows is extracted even when it has no mapped rule. Rows explicitly naming other loans are excluded even when the borrower account is shared. Local validation displays the column inventory and scoped diagnostics.

The fixed data-folder importer remains limited to the allowlisted original Query 1/2/4 filenames, never changes originals and does not start processing.

Files actually inspected on 2026-10-20 (local test data, not verified customer records):

| Workbook in backend data | Primary sheet | Columns | Data rows |
| --- | --- | --- | --- |
| [backend/data/Query_1_Revised_Test_Data.xlsx](backend/data/Query_1_Revised_Test_Data.xlsx) | MTB Query 1 | 60 | 30 |
| [backend/data/Query_2_Revised_Test_Data.xlsx](backend/data/Query_2_Revised_Test_Data.xlsx) | MTB Query 2 | 11 | 90 |
| [backend/data/Query_4_Revised_Test_Data.xlsx](backend/data/Query_4_Revised_Test_Data.xlsx) | MTB Query 4 | 32 | 30 |

The original fixed-name templates are currently absent from this workspace. Earlier 50/11/32-column, zero-row template descriptions are historical, not the current revised-file inventory. The revised exports are joined only by matching Account ID foreign keys; their 150 combined source rows do not imply 150 loans or linked borrowers. Recognition does not validate business mappings. Extraction records all populated attributes on the selected loan's related rows with sheet/row/cell provenance. README, instructions and guidelines sheets are reference-only and excluded from data totals. Empty templates yield no invented loans.

XLSX is supported; legacy XLS and macro-enabled XLSM are not. Formulas are preserved as text and **never calculated**. The backend imposes no worksheet, row, meaningful-cell, visited-cell, expanded-ZIP-byte or extracted-text cap. Excel metadata retains actual `meaningful_cell_count`, `visited_cell_count`, `expanded_size_bytes`, sheet/row counts and formula counts; `meaningful_cell_limit` and `visited_cell_limit` are `null` (unlimited). Counts cover all sheets, including headers and reference sheets. Visited counts sum the lengths of streamed rows, including padding, not the declared worksheet rectangle. Empty, styled-only and whitespace-only cells are not meaningful; zero, false and formulas are meaningful, even formulas whose calculated result would be empty. Whitespace-only data cells are omitted from record values and sources; surrounding whitespace in nonblank values is preserved, and header handling is unchanged. Uploaded XLSX files are hashed incrementally and parsed read-only from disk; the fixed local dataset importer streams source handles through the same chunked, atomic upload path without a bounded whole-file read or changes to originals. PDF extraction has no application page cap; generic extracted text and expanded DOCX size are uncapped as well. ZIP/XML/file-format validation, encrypted-PDF rejection, duplicate-header checks and source provenance remain in place. Local validation compiles header mappings with bounded document-local caches and indexes relationships rather than rescanning every loan for each relationship; loan search remains paginated, with no truncation of stored source workbooks. Header-only workbooks produce mandatory needs-review findings even when generic text checks pass.

**New MTB reviews use the deterministic v4 engine**, [backend/underwriting_engine.py](backend/underwriting_engine.py), models, graph and [backend/underwriting_config.json](backend/underwriting_config.json). The policy source is [backend/data/LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf](backend/data/LLM_Loan_Approval_Design_Guide_v4_USD_Copilot.pdf), **three pages**, SHA-256 **9ed35356fdf8d7f638b6eb77c463df71f9a6dd17234a5fe31be251fda8ac18e7**; version `v4-USD-verified-draft-2`. This remains the separately pinned v4 policy-evidence source; hash verification establishes integrity, **not production credit-policy authorization**.

The revised SOQL exports use explicit per-query root-ID mappings: Query 1 root `Id` → Characteristic, Query 2 root `Id` → Indicator, Query 4 root `Id` → CreditReport. Nested loan/account foreign keys and allowlisted product, owner, DOB, NAICS, amount, SBSS and credit-report fields map to canonical fields while retaining source cells. Validation separates unmapped source headers from canonical inputs absent in the files and unresolved relationships. Repeated `CreditReport.Id` records with conflicting values remain conflicts; no export is preferred. Missing report/as-of dates, EBITDA, discharge dates, or scalar response IDs remain unavailable rather than inferred. See [docs/underwriting-policy.md](docs/underwriting-policy.md#observed-flattened-soql-export-contract) for field-level details.

The complete implementation guide is separately pinned as [backend/data/MTB_LLM_Underwriting_Complete_Implementation_Guide.pdf](backend/data/MTB_LLM_Underwriting_Complete_Implementation_Guide.pdf), **seven pages**, SHA-256 **8a409bf6499129be8efe109d1a15f21ecfd95929b0674b993ab6d13c9c8e9467**. The configured mapping places the Section 8 decision tree on page 4 and processing stages, out-of-scope items, and auditability on page 5. This is an implementation **draft**, not approved credit policy or authority to lend. Runtime verification for each PDF independently requires the local file and pinned hash to match. The older [backend/policy.py](backend/policy.py) and [backend/policy_config.json](backend/policy_config.json) remain for compatibility, not new MTB decision execution.

Amounts are **actual USD dollars**, parsed as exact `Decimal` and serialized under explicit `_usd` keys as decimal strings or `null`; no scale inference or float repair. Effective TGC is `max(Tier I, Tier II)` only when both explicit same-loan inputs exist. The **$100M versus $100K request boundary** and **6 versus 12 calendar-month increase wait** remain unresolved. Explicit Account/Loan/report/response graph relationships, not row-order joins, drive per-loan results. Missing/ambiguous evidence stays unknown, not zero. See [docs/underwriting-policy.md](docs/underwriting-policy.md) for the contract, mapping gaps, diagnostic rules and examples.

`report.underwriting` is authoritative. The frontend prefers it over `policy_evaluation`, which is now a compatibility summary for new MTB reports (aggregate `MANUAL_REVIEW`, null decision recommendation). Per loan, `eligibility_route`, `credit_assessment`, and `authorization` are separate outputs: a route is not an assessment; candidate assessments such as `SIMULATED_APPROVAL_CANDIDATE` are not approvals; and authorization is always `NOT_ISSUED`. `strategy_branch`, guide stages 0–6, and normalized features are guide diagnostics/candidates, **not credited rule outcomes**; v4 `rule_results` remain separate. **PRODUCTION** emits `DATA_QUALITY_HOLD` or `POLICY_REVIEW_REQUIRED`, always final action `MANUAL_REVIEW`. **TEST** emits simulation paths only, always `NO_OPERATIONAL_ACTION`; public TEST with default config yields `SIMULATED_REVIEW`, not synthetic approval. Neither mode performs lending approval/denial or integrates lender authority/signoff, adverse action, funding, nCino or compliance clearance.

The guide stages displayed by the report are: stage 0 schema/identity; stage 1 policy approval/mode; stage 2 product eligibility/completeness; stage 3 hard knockouts; stage 4 credit strategy; stage 5 non-operational route; and stage 6 explanation/audit. These diagnostic results do not replace the separately pinned v4 source checks or rule statuses.

### Expanded technical specification

The newest source is [backend/data/MTB_Underwriting_Expanded_Technical_Specification.pdf](backend/data/MTB_Underwriting_Expanded_Technical_Specification.pdf), **14 pages**, SHA-256 **a4622c26c642f9546f0df9fd1634ed735d25be36e8042451ba702ddd958a326b**, independently pinned as `expanded-draft-1`. It explicitly says **not approved credit policy** and that screenshot API names, coded values and missing thresholds need confirmation.

[backend/expanded_spec.py](backend/expanded_spec.py) adds a separate per-loan `expanded_spec` trace: **19 registry entries and all 43 screenshot-listed reasons**, source pages, predicates, scope, stage ordering, missing-data action, exception path, evidence references and internal reason codes. Its `PASS/FAIL/REVIEW/NOT_EVALUATED` states do not replace the four-state v4 rule contract. Defined diagnostics execute; unresolved predicates remain `REVIEW`; not-used and out-of-POC reasons remain `NOT_EVALUATED`. **Inventory coverage does not mean every reason is executable or every check passed.** See [the expanded coverage contract](docs/underwriting-policy.md#expanded-technical-specification-coverage) for remaining gaps.

Expanded routes distinguish `SBA_APPROVAL_CANDIDATE` and `CONVENTIONAL_APPROVAL_CANDIDATE`, decline candidates and data/policy/compliance/manual review. `authorized_decision` is always `NOT_ISSUED`; internal candidate reason codes are **not approved adverse-action codes**. Existing v4 outputs remain available and authoritative for final action; the expanded diagnostic does not unlock operational actions. UI/HTML show the full inventory and registry; JSON retains it and CSV adds an `expanded_spec` JSON cell. Older saved reports need reprocessing to obtain the new trace; history is not rewritten. Processing can send bounded normalized context to Anthropic and incur charges.

| Agent | Responsibility |
| --- | --- |
| Intake | Require documents; require enabled optional rules only for generic reviews, not recognized MTB reviews. |
| Extraction | Extract text and format-specific metadata, calculate SHA-256, word counts, and character counts. |
| Rules | Apply enabled checks to each file and record matching excerpts or missing evidence. |
| Decision | Generic document status aggregation; recognized MTB uses v4 per-loan diagnostics and always `needs_review`, never a financing decision. |
| Memory | Index document chunks and conversation messages in a persistent, session-isolated Chroma collection. |

The orchestrator executes specialized Python agents sequentially in a bounded background thread pool. FastAPI returns HTTP 202 immediately and React polls progress. **Extraction, rule evaluation, and aggregate decisions remain deterministic.** The backend `AnthropicLLM` uses the official Anthropic SDK for chat and additive report commentary, not autonomous rule or decision changes. Claude commentary never changes the decision or findings.

SQLite is the source of truth for sessions, chat, mode, documents, rules, pipeline state and reports. ChromaDB stores searchable document chunks and conversation entries with session metadata filters. Embeddings remain local, normalized, 384-dimensional stable feature-hashing vectors: **lexical similarity, not neural semantic understanding**. Chat sends bounded current-session context to Anthropic: the most recent **12 messages**, each capped at **2,000 characters**, recalled memory/document excerpts, rules, metadata and report context. V4 report commentary uses **one aggregate provider call**, bounded to **20 loans, 40 rules and 40 evidence IDs globally**, with normalized exact amounts but no raw documents, source-cell values or conversation. Generic report context uses at most 40 prioritized findings. These budgets do not truncate the authoritative saved report.

Reports add nullable `llm_analysis`, `llm_model`, and `llm_error` fields (default `null`). The frontend tolerates absent fields in legacy reports and displays Claude explanation, model, and warnings separately from deterministic findings, as escaped plain text. If Anthropic is unavailable, the deterministic report is still saved and downloadable, with `llm_error` explaining the missing commentary. Chat instead returns actionable **503 / 429 / 504 / 502** errors for configuration, availability, rate-limit, timeout, or provider failures, without silent fallback. Provider requests use a **60-second network timeout per attempt**, **2 transport retries by default (3 total attempts)**, and **8,192 maximum output tokens** for chat and report commentary; larger outputs may increase provider charges and latency. SDK retries are disabled. Only connection failures (including timeouts), rate limits, and HTTP 408, 409, or 5xx (including 529) are retried; authentication, permission, not-found, and other request errors fail immediately. Backoff is deterministic exponential (0.5, 1, 2, then capped at 4 seconds); a finite nonnegative numeric `Retry-After` header overrides it, capped at 30 seconds. Retries can increase total elapsed time and provider charges. All attempts share one invocation ID; content-free logs record retry category/delay and terminal attempt count/exhaustion.

## Troubleshooting Claude commentary

Rejected commentary now includes a diagnostic ID. The backend logs the failing
validation stage (JSON/schema, loan scope, rules, evidence, or reconciliation)
and safe field paths/error categories. Requests include the explicit output JSON
schema; a single complete JSON code fence is accepted, but unsupported citations,
invalid routes, and incomplete outputs are still rejected without automatic retries.
This no-auto-retry behavior applies to completed-response JSON/business validation,
truncation, empty responses, and abnormal stops, not the transient transport failures above.
Existing saved errors are historical and are not rewritten.

Application logs are written to `backend/data/logs/application.log` by default
(or `logs/application.log` under `FOLIO_DATA_DIR`) and to the API terminal.
Logs rotate at 5 MB with three backups. API timing records include the route
template, HTTP status, and request ID (`X-Request-ID`); provider records include
duration, stop category, response length, and available token counts. Commentary
diagnostics never log prompts, provider text, source values, credentials, query
strings, or customer/loan IDs. No new paid calls are made to diagnose saved errors.

## Rule semantics

Every enabled rule applies **independently to every document**. Matching is case-insensitive literal substring matching, not regular expressions or whole-word matching.

| Type | Value | Pass condition |
| --- | --- | --- |
| `required_text` | Literal phrase | Phrase occurs in extracted content. |
| `forbidden_text` | Literal phrase | Phrase does not occur in extracted content. |
| `min_words` | Integer from 1 to 1,000,000 | Whitespace-split word count reaches the threshold. |
| `required_metadata` | Exact top-level metadata key | Metadata value exists and is not blank. |

Example metadata keys: `filename`, `extension`, `size_bytes`, `sha256`, `word_count`, `character_count`, `extracted_at`; PDF/DOCX can provide `author`, `title`, and `subject`; PDF adds `pages`; CSV adds `columns` and `rows`; JSON adds `json_type` and, for objects, `json_keys`. File-format metadata is not independently verified. JSON object properties are **not** automatically promoted to metadata.

A violated optional rule produces its configured severity (`review` or `fail`). **For generic document reviews only**, any failure means **rejected**; otherwise a review means **needs_review**; otherwise **approved**. These are document statuses, not lending decisions. Recognized MTB always remains **needs_review**, independently of optional text checks, and may process with zero optional rules. Empty extracted text always creates a system review finding. Generic reviews require at least one enabled rule. Editing rules, mode or documents invalidates the report and requires a fresh run.

## API

Full paths below; schemas are at http://127.0.0.1:8010/docs.

| Method | Path | Contract |
| --- | --- | --- |
| GET | `/api/health` | Health status |
| GET / POST | `/api/sessions` | List / create; create accepts optional `title`, `mode` (default `TEST`; `PRODUCTION` must be explicit) |
| GET | `/api/sessions/{id}` | Session, progress and report |
| POST | `/api/sessions/{id}/messages` | `{ "content": "…" }` |
| POST | `/api/sessions/{id}/preflight` | No body; headers only before loan selection, loan-scoped validation afterward; no provider call |
| PUT | `/api/sessions/{id}/loan` | `{ "loan_id": "L1" }`; stream uploads and validate only this explicit loan's related records; 404 if absent |
| POST | `/api/sessions/{id}/assistant/inspect` | Exactly `loan_id`, optional `topic=blockers|rules|evidence`; local saved-report lookup |
| POST | `/api/sessions/{id}/assistant/process` | Exactly `confirmed=true`, `expected_updated_at`; revision-bound explicit start, returns 202 |
| POST | `/api/sessions/{id}/assistant/notes` | Exactly `loan_id`, `report_id`, `text`, optional `kind=note|acknowledgment`; unverified assertion |
| POST | `/api/sessions/{id}/documents` | Multipart `files`; MTB package has Query 1, Query 2, Query 4, linked by normalized Account ID columns |
| POST | `/api/sessions/{id}/dataset` | Fixed allowlisted Query 1/2/4 import into an empty session |
| GET | `/api/dataset` | Stored indexed Query 1/2/4 set: `status` (`empty|indexing|ready|failed|stale`), counts and files |
| GET | `/api/dataset/loans?q=&limit=&offset=` | Search stored loans by loan or account ID (indexed) |
| GET | `/api/dataset/accounts?q=&limit=&offset=` | Search stored borrower accounts by ID or name (indexed) |
| POST | `/api/sessions/{id}/stored-dataset` | Attach the ready stored set to an empty session; 409 if not empty or not ready |
| GET | `/api/batch` | Latest batch run: `status` (`none|running|completed|cancelled|failed|interrupted`), progress and per-status counts |
| GET | `/api/batch/results?offset=&limit=&status=` | Paged batch results with `processing` (`succeeded|failed`); `status` filters `Approved|Decline|Refer|Succeeded|Error` |
| POST | `/api/batch` | Start a batch review of every stored loan. `{"resume": true}` processes remaining loans, `{"failed_only": true}` retries every failed loan, `{"loan_ids": [...]}` retries the selected failed loans. Returns 202; 409 if running, no data or nothing to retry; 503 without Anthropic |
| POST | `/api/batch/cancel` | Cancel the running batch after in-flight loans finish |
| GET | `/api/batch/export` | Download the latest results as XLSX: Loan ID, Account ID, Status, Rationale / result |
| DELETE | `/api/sessions/{id}/documents/{document_id}` | Remove upload |
| PUT | `/api/sessions/{id}/rules` | `{ "rules": [...] }`, optional document checks only |
| PUT | `/api/sessions/{id}/mode` | Exactly `{ "mode": "TEST" }` or `{ "mode": "PRODUCTION" }`; clears report |
| POST | `/api/sessions/{id}/process` | No body; returns 202, poll the session until completed/failed |
| GET | `/api/sessions/{id}/report` | Standalone HTML |
| GET | `/api/sessions/{id}/underwriting?format=json` | Exact-decimal v4 batch JSON; default format is JSON |
| GET | `/api/sessions/{id}/underwriting?format=csv` | One row per explicit loan; separate route, credit-assessment candidate and authorization columns; nested JSON strings, formula-injection-sanitized cells |

CSV columns begin `loan_id,borrower_account_id,mode,policy_version,currency,eligibility_route,credit_assessment,authorization,strategy_branch,...`. Later columns include recommendation path, final action, exact normalized amounts, rule/evidence/issues, and both source documents with their availability flags and the implementation-guide SHA-256. Read `eligibility_route`, `credit_assessment`, and `authorization` separately.

Modes are strict uppercase. Create/mode requests reject extra `authority`, `approved`, `test_policy` or `policy_config` fields (422); the API cannot enable synthetic criteria or confer authority. Files/rules/mode changes during processing return 409. Underwriting export returns 409 without a completed v4 report (404 for unknown session; 422 for unsupported format). Empty batches export JSON with no loans and CSV headers only. Chat persistence does not overwrite worker progress; interrupted jobs become retryable failures on restart.

### Example public TEST invocation

This PowerShell example assumes **three locally prepared XLSX workbooks**, one each for Query 1, Query 2, and Query 4. Put the corresponding Account ID values in each source using a supported Account ID header variant, as detailed in [the policy guide](docs/underwriting-policy.md#public-test-walkthrough). The paths are placeholders; the API does not create workbooks or expose fixture overrides. No credentials are included. With default policy configuration, TEST remains review-only.

```powershell
$base = 'http://127.0.0.1:8010/api'
$session = Invoke-RestMethod -Method Post -Uri "$base/sessions" -ContentType 'application/json' -Body '{"title":"Illustrative synthetic TEST","mode":"TEST"}'
$id = $session.id
curl.exe --fail-with-body -X POST "$base/sessions/$id/documents" -F 'files=@C:/synthetic-fixtures/MTB_Query_1.xlsx' -F 'files=@C:/synthetic-fixtures/MTB_Query_2.xlsx' -F 'files=@C:/synthetic-fixtures/MTB_Query_4.xlsx'
# Replace L1 with the exact loan number present in your uploaded workbooks.
$selection = @{ loan_id = 'L1' } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "$base/sessions/$id/loan" -ContentType 'application/json' -Body $selection
Invoke-RestMethod -Method Post -Uri "$base/sessions/$id/process"
Invoke-RestMethod -Uri "$base/sessions/$id"
# Repeat the preceding GET until status is completed (stop and inspect error if failed).
# Run these downloads only after completion:
Invoke-WebRequest -Uri "$base/sessions/$id/underwriting?format=json" -OutFile 'synthetic-underwriting.json'
Invoke-WebRequest -Uri "$base/sessions/$id/underwriting?format=csv" -OutFile 'synthetic-underwriting.csv'
```

The [policy guide](docs/underwriting-policy.md#illustrative-exact-json) includes an exact JSON example and a separate developer-only synthetic TEST invocation. Internal test helpers are not a production ingestion interface.

## Configuration and storage

Follow the manual environment-file setup above; do not send credentials through chat. Existing process environment variables take precedence. Restart **Agent: API** after changes.

Agent retains the legacy `FOLIO_*` environment variable names and `folio_memory` Chroma collection for compatibility. Existing database/data paths, worker thread names, Python APIs, and the internal `folio-frontend` npm package name are unchanged. The branding rename does not rewrite saved conversations or uploaded content; historical text may still contain the former product name.

- `ANTHROPIC_API_KEY`: required backend-only credential for Claude chat; the example value is blank.
- `ANTHROPIC_MODEL`: optional override, default `claude-sonnet-5-5`.
- `ANTHROPIC_MAX_RETRIES`: optional transport retry count, default 2; integers are clamped to 0–5, malformed values safely use 2. Zero disables retries.
- `FOLIO_DATA_DIR`: defaults to the backend data directory; relative overrides resolve from the launch working directory.
- `FOLIO_CORS_ORIGINS`: comma-separated explicit browser origins, defaults to localhost/127.0.0.1 on port 5174.
- `BATCH_REVIEW_CONCURRENCY`: optional number of loans reviewed in parallel by **Batch review**, default 4, clamped to 1–16.

The retired `FOLIO_MAX_FILE_MB` and `FOLIO_MAX_XLSX_MB` settings are ignored, including for the fixed local dataset importer. CSV field size uses the platform's maximum representable size instead of Python's small default cap. Provider token/context budgets, paid-call/review limits, report scopes, pagination, multipart non-file field validation and the three-source MTB business contract are unchanged.

The backend creates a SQLite database, local Chroma persistence and uploads under its data directory; never commit these or customer uploads. Reports persist within sessions. There is no automatic retention policy or encryption at rest. Do not delete the whole data directory casually: it also contains source PDFs and workbook inputs. Back up needed data before any deliberate reset. V4 source verification specifically reads the pinned PDF from the module's backend data directory, not an uploaded policy document or an alternative `FOLIO_DATA_DIR`.

## Validation

Run **Agent: Backend tests** and **Agent: Frontend build** through VS Code tasks. Frontend tests use the npm `test` script in [frontend/package.json](frontend/package.json). Backend tests use pytest against real temporary SQLite and Chroma databases.

Verified on 2026-10-21: **544 backend tests passed; 91 frontend tests passed; frontend production build passed**. Provider calls are mocked; no live Anthropic credentials, lender authority or production lending integration were verified. The server catalog uses v4 rule IDs and separately labeled implementation-guide/expanded-spec diagnostics; readiness counts remain distinct from per-loan results. Guided local validation was also exercised in the browser without a provider call.

Covered workflows include upload-first enforcement, all rule types, asynchronous processing, conversation and restart persistence, session isolation, document retrieval, file removal/report invalidation, malformed files, HTML escaping, React upload/chat/rule editing, progress polling, and separate escaped Claude explanations/warnings with legacy-report compatibility. Frontend provider responses are mocked; these tests do not make billable Anthropic calls.

## Security and limitations

- **Local, single-user demo only. No authentication or authorization.** Session IDs are not access control. Bind to loopback; do not expose publicly or upload sensitive documents without adding authentication, isolation, encryption, retention controls, and a security review.
- **External data processing:** chat context, recalled messages, document excerpts, rules, metadata, and findings may be sent to Anthropic for chat and report commentary. Local persistence and local lexical embeddings do not mean data stays exclusively on-device. Confirm provider terms and data-handling requirements before using sensitive material. Claude output can be inaccurate; deterministic findings remain authoritative. The UI also loads fonts from Google Fonts.
- Run one API process/worker. In-process locking and jobs are not a distributed queue; production requires a separate job runner and durable queue. Parser workloads have no application size/page guards, sandbox or hard CPU timeout.
- No OCR, image understanding, digital-signature validation, malware scanning, or legal/compliance certification. Scanned PDFs require OCR elsewhere first. DOCX headers, footers, text boxes, and layout are not exhaustively extracted. UTF-8 is required for text files.
- Documents are treated as data, never executable instructions. Uploaded HTML-like text is escaped in the UI and HTML report. Nothing from uploads is executed. HTML reports contain no scripts or external assets.
- **Unrestricted local loading is not unlimited machine capacity.** Large or highly compressed inputs can exhaust RAM, disk, CPU or request time. Multipart files spool to disk and uploads copy in chunks, but extracted text, records, sessions and some format parsers still materialize data in memory. OS, filesystem, parser/library, browser and external proxy/server constraints can still apply. Add ingress limits, quotas, rate limiting and parser sandboxing before deployment; this local configuration deliberately does not enforce file resource caps.
- Conversation recall is restricted to the current session. Other sessions remain accessible in the sidebar but are not searched automatically. Large histories and extracted text are returned with session records; pagination is a production improvement.