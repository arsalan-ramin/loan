# Marketing AI

A standalone, light-blue document workspace. Upload campaign files, inspect saved metadata, and ask questions with links to the passages used in the answer. It does not depend on the loan application's API or data.

## Run locally

Use Python 3.11+ with SQLite FTS5 and Node.js 22+.

**Terminal 1** (PowerShell, from the repository root):

```powershell
Set-Location .\marketing-ai
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\backend\requirements.txt
.\.venv\Scripts\python.exe -m uvicorn marketing_backend.main:app --app-dir .\backend --host 127.0.0.1 --port 8011
```

**Terminal 2** (from the repository root):

```powershell
Set-Location .\marketing-ai\frontend
npm ci
npm run dev
```

Open [http://127.0.0.1:5174](http://127.0.0.1:5174). Use the same hostname on later visits to reopen your workspace.

## Try it

1. Upload a brief, deck, or results file. Supported formats: PDF, DOCX, PPTX, XLSX, CSV, TXT, Markdown, and JSON. Upload up to five files at a time, 12 MB each. Scanned documents need OCR before upload; embedded images and charts are not extracted.
2. Expand **View saved metadata** to inspect file details such as size, fingerprint, and upload time. Format-specific details may include page count, slide count, sheet names, or CSV columns.
3. Open **Assistant**. Ask “Who is the target audience for the spring campaign?” and then “What is its budget?” The assistant uses recent conversation context and cites the matching file and location. Use **Search in** to restrict retrieval to one file.

By default, the assistant returns cited excerpts using local text search. To enable synthesized answers, install and start Ollama with a **locally installed** model, set `MARKETING_OLLAMA_MODEL` to its name in Terminal 1, and restart FastAPI:

```powershell
$env:MARKETING_OLLAMA_MODEL = 'qwen2.5:7b'
.\.venv\Scripts\python.exe -m uvicorn marketing_backend.main:app --app-dir .\backend --host 127.0.0.1 --port 8011
```

The app sends retrieved passages only to the loopback Ollama endpoint when this mode is enabled. Confirm your Ollama model runs locally; do not select a cloud-backed model for private files. A citation links an answer to a retrieved passage, but does not guarantee that the model interpreted it correctly.

## Storage and scope

SQLite stores metadata, searchable extracted passages, and chat history in `data/workspace.sqlite3`. Original uploads are not retained. The app associates each browser workspace with an HTTP-only cookie and filters searches to that workspace. Removing a source also clears its workspace conversation because earlier messages may quote the removed file. This is a local prototype, not an authenticated or hardened multi-user deployment. Do not expose it on a shared network.

## Verify

- From `marketing-ai`: `.\.venv\Scripts\python.exe -m pytest -q backend`
- From `marketing-ai/frontend`: `npm run build`
