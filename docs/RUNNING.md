# Starting Agent: backend and UI

This guide covers local development on Windows with PowerShell and VS Code.
Run commands from the repository root (the folder containing `backend`, `frontend`,
and `.vscode`). Keep both services running while using the application.

## Requirements

- Python **3.11–3.13**.
- Node.js **22.12+ within version 22**, **24.x**, or **26+**, matching the frontend's supported engine ranges; npm included.
- VS Code (recommended).
- An Anthropic account, available model, backend API key and billable network
  access for Claude chat and report commentary. Local validation does not require
  an Anthropic call. Deterministic reports remain available if commentary fails.

## Fastest start: VS Code tasks

If dependencies and your environment are already configured:

1. Open the repository root in VS Code.
2. Select **Terminal → Run Task → Agent: Start**.
3. Open **http://127.0.0.1:5174/** in your browser.

Individual tasks:

| Task | Starts |
| --- | --- |
| **Agent: API** | FastAPI/Uvicorn at http://127.0.0.1:8010 |
| **Agent: UI** | React/Vite at http://127.0.0.1:5174 |
| **Agent: Start** | Both services |

Do not start a second copy if a service is already running. Task definitions are
in [../.vscode/tasks.json](../.vscode/tasks.json).

## First-time dependency setup

Open a PowerShell terminal in the repository root. Create the virtual environment
only if one does not already exist:

```powershell
py -3.13 -m venv .venv
```

Python 3.11 or 3.12 can be selected instead if installed. Install dependencies:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
npm --prefix frontend install
```

Virtual-environment activation is optional: the commands here use its interpreter
directly, avoiding PowerShell activation-policy problems. Do not recreate an
existing working environment unnecessarily.

## Configure Claude manually

1. In the VS Code Explorer, manually copy the backend environment example to a
   local file named `.env` inside the backend folder. Do not overwrite an existing
   environment file.
2. Open the local file in your editor and enter your own `ANTHROPIC_API_KEY`.
   **Never paste keys into chat, frontend code or version control.**
3. The default model is `claude-sonnet-5-5`. Set `ANTHROPIC_MODEL` only if you want
   another model available to your account.
4. Restart **Agent: API** after editing the environment.

Credentials are backend-only. Never use a frontend `VITE_` variable for secrets.
An environment variable already set in the API process takes precedence over the
local environment file. No credentials are needed for the UI itself.

Chat sends bounded session context, document excerpts and report context to
Anthropic. Report commentary also uses external processing and can incur charges.
Local SQLite/Chroma persistence does not make the app offline-only.

## Start manually: two terminals

Use this as an alternative to VS Code tasks, not in addition to running tasks.

### Terminal 1 — backend

From the repository root:

```powershell
& .\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8010
```

Wait for `Application startup complete` and the Uvicorn listening message.

- Health check: http://127.0.0.1:8010/api/health
- API documentation: http://127.0.0.1:8010/docs

The health endpoint should return `{"status":"ok"}`. This confirms the API is
running, **not** that Anthropic credentials, billing or model access work.

### Terminal 2 — frontend

From the repository root:

```powershell
npm --prefix frontend run dev
```

Open **http://127.0.0.1:5174/**. Vite forwards `/api` requests to the backend on
port **8010**. Starting the UI alone does not start the backend.

## Stop and restart

- **Manual terminals:** press **Ctrl+C** in each service's terminal.
- **VS Code tasks:** use **Terminal → Terminate Task** and select **Agent: API**
  and/or **Agent: UI**. Restart them through **Terminal → Run Task**.
- Restart the API after backend or environment changes; the supplied API task
  does not enable automatic reload. Vite normally updates frontend changes live.
- Do not stop unrelated applications on ports **8000** or **5173**. Agent uses
  **8010** and **5174** intentionally.

Stopping services does not delete saved sessions. Do not delete the backend data
directory to fix startup errors: it also holds source PDFs, workbooks and local
SQLite/Chroma state. Back up important data before any deliberate reset.

## Quick usage check

1. Confirm the UI says **Connected to your API**.
2. Create or reopen a session and upload your documents.
3. Select **Validate files locally** for deterministic preflight without a Claude call.
4. Review missing inputs and source-mapping issues; recognition is not validation.
5. Select **Prepare review**, review the external-processing disclosure, then
   **Confirm and process** when ready. The separate **Process documents** button
   remains an explicit-start alternative.
6. Open the report, select a loan, or use local evidence buttons. For Claude
   follow-ups, type in **Message Agent** and press **Send**; review notes only save notes.

No mode issues operational loan approvals or denials; authorized human review is
required. TEST is simulation-only, and PRODUCTION remains manual review.

## Tests and build

From the repository root:

```powershell
& .\.venv\Scripts\python.exe -m pytest backend/tests -q
npm --prefix frontend test
npm --prefix frontend run build
```

VS Code also provides **Agent: Backend tests** and **Agent: Frontend build** tasks.
The production build checks TypeScript and creates frontend assets; it does not
start either service or configure production hosting.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| UI shows API offline / fetch failure | Start the backend, verify the health URL, and use the UI on port 5174. |
| Port already in use | Stop your existing Agent task before restarting. Identify the owner before stopping any process; never kill unrelated apps. Vite uses a strict port and will not silently switch. |
| Python executable missing | Complete virtual-environment setup from the repository root. |
| `ModuleNotFoundError` | Install backend requirements using the virtual environment's interpreter, not an unrelated system Python. |
| `npm` not found / unsupported Node version | Install a supported Node.js version and reopen the terminal. |
| PowerShell blocks an npm script | Use `npm.cmd` instead of `npm` for the commands above; no global execution-policy change is required. |
| Anthropic configuration/authentication/model error | Manually check your backend configuration and account access, then restart the API. Do not share keys or secret-containing logs. |
| Claude output-limit or timeout warning | The deterministic report is still available. A retry may incur charges and is not guaranteed to succeed. Failed chat questions remain in the composer. |
| Three-file data-folder import returns 404 | It accepts only the original allowlisted filenames. Revised/sample workbooks must be uploaded manually; do not rename or fabricate mappings to bypass validation. |
| Browser still shows old UI | Refresh after confirming the Vite dev server is running. Backend changes require an API restart. |

For port changes, update the task arguments, Vite server/proxy and backend CORS
origins consistently. Preserve supported legacy `FOLIO_*` configuration names.

## Other operating systems

On macOS/Linux, create a virtual environment with a supported Python, use
`.venv/bin/python` instead of the Windows interpreter path and adapt the VS Code
task paths. The same repository-root Uvicorn and npm arguments and ports apply.

## Deployment boundary

This is a **local, single-user development application**, with no authentication
or authorization. Bind to loopback; do not expose it publicly. Production hosting,
HTTPS, access control, privacy/retention controls, durable jobs and lender authority
integration require separate work and review.