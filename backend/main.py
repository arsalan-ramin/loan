"""Run from the repository root: uvicorn backend.main:app --reload."""

import logging
import csv
import json
import os
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

from .agents import SUPPORTED, DecisionAgent, ExtractionAgent, IntakeAgent, MemoryAgent, RulesAgent
from .assistant_workflow import (
    ACCOUNT_INDEX_VERSION, GuidedProcessRequest, InspectRequest, ReviewNoteRequest,
    inspect_loan, is_loan_attribute_inventory_request, loan_attribute_inventory,
    preflight, require_loan,
)
from .llm import LLMError
from .loan_review import LoanReviewRequest, review_selected_loan
from .models import (AGENTS, AccountCandidate, AccountCandidatePage, AccountSelectionRequest,
                     ChatRequest, CreateSession, Document, Message, ModeRequest,
                     LoanCandidate, LoanCandidatePage, LoanSelectionRequest,
                     RulesRequest, Session, Step, ReviewNote, default_rules)
from .reports import render_report
from .storage import Store
from .underwriting_models import UnderwritingBatch

load_dotenv(Path(__file__).with_name(".env"))
logger = logging.getLogger(__name__)


def require_session(store: Store, session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(404, "Conversation not found.")
    return session


def editable(session: Session) -> None:
    if session.status == "processing":
        raise HTTPException(409, "Wait for the current review to finish before changing files or rules.")


def invalidate(session: Session, *, clear_selection: bool = True) -> None:
    session.report = None
    if session.preflight is not None:
        session.previous_preflight = session.preflight
    session.preflight = None
    if clear_selection:
        session.selected_account_id = None
        session.selected_loan_id = None
    session.error = None
    session.status = "ready" if session.documents else "awaiting_upload"
    session.steps = [Step(agent=agent) for agent in AGENTS]


def process_session(store: Store, session_id: str) -> None:
    session = require_session(store, session_id)
    findings = []

    def checkpoint() -> None:
        # Chat remains usable during processing; never overwrite concurrently saved messages.
        with store.lock:
            latest = require_session(store, session_id)
            session.messages = latest.messages
            store.save(session)

    try:
        for index, step in enumerate(session.steps):
            step.status = "running"
            step.detail = "Working on your documents…"
            checkpoint()
            if index == 0:
                step.detail = IntakeAgent().check(session)
            elif index == 1:
                extractor = ExtractionAgent()
                for doc in session.documents:
                    extractor.extract(doc, store.root / "uploads" / session.id / doc.id)
                if session.policy_rules:
                    # Renamed workbooks can become recognizable only after header extraction.
                    session.rules = [rule for rule in session.rules if rule not in default_rules()]
                step.detail = f"Extracted text and metadata from {len(session.documents)} document(s)."
            elif index == 2:
                findings = RulesAgent().evaluate(session)
                step.detail = f"Evaluated {len(findings)} evidence-backed checks."
            elif index == 3:
                session.report = DecisionAgent().decide(session, findings)
                step.detail = f"Prepared a {session.report.decision.replace('_', ' ')} decision and HTML report."
                if session.report.underwriting is not None:
                    batch = session.report.underwriting
                    step.detail = (f"V4 underwriting: {batch.mode}; {batch.policy_version}; "
                                   f"{len(batch.loan_results)} explicit loans; "
                                   f"{sum(len(loan.rule_results) for loan in batch.loan_results)} checks. "
                                   "Human review required; no operational lending decision.")
            else:
                step.detail = MemoryAgent(store).index(session)
            step.status = "completed"
            checkpoint()
        with store.lock:
            latest = require_session(store, session_id)
            session.messages = latest.messages
            session.status = "completed"
            assert session.report is not None
            message = Message(role="assistant", content=f"Your review is complete. {session.report.summary} Open the decision report to inspect the evidence or download a standalone HTML copy.")
            store.remember(session.id, message.id, message.content, "conversation", "assistant")
            session.messages.append(message)
            store.save(session)
    except Exception as exc:
        logger.exception("Review failed for session %s", session_id)
        with store.lock:
            latest = require_session(store, session_id)
            session.messages = latest.messages
            session.status = "failed"
            session.report = None
            session.error = str(exc)[:500] if isinstance(exc, ValueError) else "Processing failed. Check file contents and server logs, then retry."
            for step in session.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.detail = session.error
            session.messages.append(Message(role="assistant", content=f"The review could not finish: {session.error}"))
            store.save(session)


def create_app(data_dir: Path | None = None) -> FastAPI:
    root = data_dir or Path(os.getenv("FOLIO_DATA_DIR", str(Path(__file__).parent / "data")))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        store = Store(root)
        application.state.store = store
        application.state.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="folio")
        # In-process jobs cannot survive a restart. Make interrupted reviews explicitly retryable.
        for session in store.list():
            if session.policy_rules or (session.preflight is not None and session.preflight.recognized_mtb):
                # Remove only exact bundled examples; retain all user-authored or modified checks.
                cleaned = [rule for rule in session.rules if rule not in default_rules()]
                stale_generic_report = session.report is not None and session.report.policy_evaluation is None
                if cleaned != session.rules or stale_generic_report:
                    session.rules = cleaned
                    if session.status != "processing":
                        invalidate(session)
                        if stale_generic_report:
                            session.messages.append(Message(role="assistant", content="The previous generic document report did not evaluate MTB policy and is invalidated. These Excel inputs are now recognized as MTB workbooks. Select Process documents to run the page-cited guideline checks; word count is not lending approval."))
                    store.save(session)
            if session.status == "processing":
                session.status = "failed"
                session.report = None
                session.error = "Server restarted during this review. Select Start review to retry."
                for step in session.steps:
                    if step.status == "running":
                        step.status = "failed"
                        step.detail = session.error
                store.save(session)
        yield
        application.state.executor.shutdown(wait=True, cancel_futures=False)

    application = FastAPI(title="Agent document intelligence", version="1.0.0", lifespan=lifespan)
    origins = [s.strip() for s in os.getenv("FOLIO_CORS_ORIGINS", "http://localhost:5174,http://127.0.0.1:5174").split(",") if s.strip()]
    application.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type"])

    @application.get("/api/health")
    def health():
        return {"status": "ok"}

    @application.get("/api/sessions", response_model=list[Session])
    def list_sessions(request: Request):
        return request.app.state.store.list()

    @application.post("/api/sessions", response_model=Session, status_code=201)
    def new_session(request: Request, body: CreateSession):
        store: Store = request.app.state.store
        session = Session(title=body.title, mode=body.mode)
        greeting = Message(role="assistant", content=IntakeAgent().greeting())
        session.messages.append(greeting)
        with store.lock:
            store.remember(session.id, greeting.id, greeting.content, "conversation", "assistant")
            store.save(session)
        return session

    @application.get("/api/sessions/{session_id}", response_model=Session)
    def get_session(request: Request, session_id: str):
        return require_session(request.app.state.store, session_id)

    @application.post("/api/sessions/{session_id}/messages", response_model=Session)
    def send_message(request: Request, session_id: str, body: ChatRequest):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
        local_inventory = is_loan_attribute_inventory_request(body.content)
        origin = "local_assistant" if local_inventory else "conversation"
        if local_inventory:
            answer = loan_attribute_inventory(session, store.root, body.content, body.loan_id)
        else:
            chat_session = session
            if body.loan_id is not None:
                loan = require_loan(session, body.loan_id)
                chat_session = session.model_copy(deep=True)
                assert chat_session.report is not None and chat_session.report.underwriting is not None
                chat_session.report.underwriting.loan_results = [loan.model_copy(deep=True)]
            memory = MemoryAgent(store)
            # Network calls must never hold the global persistence lock.
            try:
                answer = memory.respond(chat_session, body.content)
            except LLMError as exc:
                raise HTTPException(exc.status_code, str(exc)) from None
        with store.lock:
            latest = require_session(store, session_id)
            if latest.updated_at != session.updated_at:
                raise HTTPException(409, "The conversation changed while your message was being handled. Please retry.")
            session = latest
            messages = [Message(role="user", content=body.content), Message(role="assistant", content=answer, origin=origin)]
            for message in messages:
                if message.origin == "conversation":
                    store.remember(session.id, message.id, message.content, "conversation", message.role)
            session.messages.extend(messages)
            store.save(session)
        return session

    @application.post("/api/sessions/{session_id}/documents", response_model=Session)
    def upload_documents(request: Request, session_id: str, files: list[UploadFile] = File(...)):
        store: Store = request.app.state.store
        created: list[Path] = []
        try:
            with store.lock:
                session = require_session(store, session_id)
                editable(session)
                if not files or len(files) + len(session.documents) > 20:
                    raise HTTPException(400, "Upload between 1 and 20 documents per conversation.")
                incoming_xlsx = any(Path(file.filename or "").suffix.casefold() == ".xlsx" for file in files)
                existing_xlsx = any(document.extension == ".xlsx" for document in session.documents)
                if incoming_xlsx or existing_xlsx:
                    total_xlsx = sum(document.extension == ".xlsx" for document in session.documents) + sum(
                        Path(file.filename or "").suffix.casefold() == ".xlsx" for file in files
                    )
                    if total_xlsx > 3 or any(Path(file.filename or "").suffix.casefold() != ".xlsx" for file in files):
                        raise HTTPException(400, "An MTB review accepts three XLSX source workbooks only: Query 1, Query 2 and Query 4. Other files cannot be mixed in.")
                    if any(document.extension != ".xlsx" for document in session.documents):
                        raise HTTPException(400, "An MTB workbook set cannot be mixed with other document files.")
                directory = store.root / "uploads" / session.id
                directory.mkdir(exist_ok=True)
                new_documents = []
                for file in files:
                    name = (file.filename or "document").replace("\\", "/").split("/")[-1]
                    extension = Path(name).suffix.lower()
                    if extension not in SUPPORTED:
                        raise HTTPException(400, f"{name}: unsupported file type. Use XLSX, PDF, DOCX, TXT, MD, CSV, or JSON.")
                    max_file_mb = int(os.getenv("FOLIO_MAX_XLSX_MB", "50")) if extension == ".xlsx" else int(os.getenv("FOLIO_MAX_FILE_MB", "20"))
                    max_bytes = max_file_mb * 1024 * 1024
                    document = Document(name=name[:240], size=0, extension=extension)
                    path = directory / document.id
                    created.append(path)
                    with path.open("wb") as destination:
                        while chunk := file.file.read(1024 * 1024):
                            document.size += len(chunk)
                            if document.size > max_bytes:
                                raise HTTPException(413, f"{name}: file exceeds the {max_file_mb} MB limit.")
                            destination.write(chunk)
                    if not document.size:
                        raise HTTPException(400, f"{name}: empty files cannot be reviewed.")
                    new_documents.append(document)
                store.forget_documents(session.id)
                session.documents.extend(new_documents)
                store.forget_account_candidates(session.id)
                if session.policy_rules:
                    session.rules = [rule for rule in session.rules if rule not in default_rules()]
                if session.title == "Untitled review":
                    session.title = new_documents[0].name[:120]
                invalidate(session)
                message = Message(role="assistant", content=f"Received {len(new_documents)} file(s). You now have {len(session.documents)} document(s). Use Validate files locally to inspect structure and priority blockers without an Anthropic call. Then choose the next guided action; processing requires your explicit start.")
                store.remember(session.id, message.id, message.content, "conversation", "assistant")
                session.messages.append(message)
                store.save(session)
                return session
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        finally:
            for file in files:
                file.file.close()

    @application.post("/api/sessions/{session_id}/dataset", response_model=Session)
    def load_dataset(request: Request, session_id: str):
        """Import only the three fixed original query workbooks; IDs link them, never row order."""
        store: Store = request.app.state.store
        names = ("MTB_Query_1.xlsx", "MTB_Query_2.xlsx", "MTB_Query_4.xlsx")
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            if session.documents:
                raise HTTPException(409, "Use a new empty session to load all three source workbooks.")
            max_bytes = int(os.getenv("FOLIO_MAX_XLSX_MB", "50")) * 1024 * 1024
            files = []
            try:
                for name in names:
                    path = store.root / name
                    if not path.is_file() or path.is_symlink():
                        raise HTTPException(404, f"Missing workbook in the backend data folder: {name}")
                    with path.open("rb") as source:
                        raw = source.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raise HTTPException(413, f"{name}: exceeds the configured file-size limit.")
                    files.append(UploadFile(filename=name, file=io.BytesIO(raw)))
                return upload_documents(request, session_id, files)
            finally:
                for file in files:
                    file.file.close()

    @application.delete("/api/sessions/{session_id}/documents/{document_id}", response_model=Session)
    def delete_document(request: Request, session_id: str, document_id: str):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            if not any(doc.id == document_id for doc in session.documents):
                raise HTTPException(404, "Document not found.")
            store.forget_documents(session.id)
            store.forget_account_candidates(session.id)
            session.documents = [doc for doc in session.documents if doc.id != document_id]
            invalidate(session)
            store.save(session)
            (store.root / "uploads" / session.id / document_id).unlink(missing_ok=True)
        return session

    @application.get("/api/sessions/{session_id}/documents/{document_id}/download")
    def download_document(request: Request, session_id: str, document_id: str):
        store: Store = request.app.state.store
        session = require_session(store, session_id)
        document = next((item for item in session.documents if item.id == document_id), None)
        if document is None:
            raise HTTPException(404, "Document not found in this session.")
        path = store.root / "uploads" / session.id / document.id
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Uploaded document is no longer available.")
        media_type = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                      if document.extension == ".xlsx" else "application/octet-stream")
        return FileResponse(path, media_type=media_type, filename=document.name,
                            headers={"X-Content-Type-Options": "nosniff"})

    @application.put("/api/sessions/{session_id}/rules", response_model=Session)
    def update_rules(request: Request, session_id: str, body: RulesRequest):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            session.rules = body.rules
            invalidate(session)
            store.save(session)
        return session

    @application.put("/api/sessions/{session_id}/mode", response_model=Session)
    def update_mode(request: Request, session_id: str, body: ModeRequest):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            session.mode = body.mode
            invalidate(session)
            store.save(session)
        return session

    @application.post("/api/sessions/{session_id}/preflight", response_model=Session)
    def validate_locally(request: Request, session_id: str):
        store: Store = request.app.state.store
        with store.lock:
            snapshot = require_session(store, session_id)
            editable(snapshot)
        # Parse detached uploads outside the persistence lock; no provider calls.
        result = preflight(snapshot, store.root)
        with store.lock:
            latest = require_session(store, session_id)
            editable(latest)
            if latest.updated_at != snapshot.updated_at:
                raise HTTPException(409, "Session changed during validation. Validate the current files again.")
            latest.preflight = result
            store.replace_account_candidates(latest.id, result.account_candidates)
            if latest.selected_account_id and not store.has_account_candidate(latest.id, latest.selected_account_id):
                latest.selected_account_id = None
            if latest.selected_loan_id:
                linked_account = store.loan_candidate_account(latest.id, latest.selected_loan_id)
                if linked_account is None or linked_account != latest.selected_account_id:
                    latest.selected_loan_id = None
            latest.messages.append(Message(role="assistant", origin="local_assistant", content=(
                f"Local validation complete: {result.document_count} files, {result.loan_count} explicit loans, "
                f"{result.issue_count} diagnostic issues. No Anthropic request or lending decision was made. "
                + result.suggested_next_step)))
            store.save(latest)
        return latest

    @application.get("/api/sessions/{session_id}/accounts", response_model=AccountCandidatePage)
    def search_accounts(request: Request, session_id: str, q: str = Query("", max_length=128),
                        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        store: Store = request.app.state.store
        session = require_session(store, session_id)
        if session.preflight is None or not session.preflight.recognized_mtb:
            raise HTTPException(409, "Run local validation before searching detected borrower accounts.")
        refreshed_preflight = None
        if session.preflight.account_index_version < ACCOUNT_INDEX_VERSION:
            snapshot_revision = session.updated_at
            refreshed_preflight = preflight(session, store.root)
            refreshed_preflight.comparison = "Account directory refreshed using the current explicit relationship mappings."
            refreshed_preflight.suggested_next_step = (
                "Select the detected borrower account/client to scope processing. "
                "This local index refresh is not credit clearance."
            )
            with store.lock:
                latest = require_session(store, session_id)
                editable(latest)
                if latest.updated_at != snapshot_revision:
                    raise HTTPException(409, "Session changed during account index refresh. Validate the current files again.")
                latest.preflight = refreshed_preflight
                store.replace_account_candidates(session_id, refreshed_preflight.account_candidates)
                if latest.selected_account_id and not store.has_account_candidate(session_id, latest.selected_account_id):
                    latest.selected_account_id = None
                if latest.selected_loan_id:
                    linked_account = store.loan_candidate_account(session_id, latest.selected_loan_id)
                    if linked_account is None or linked_account != latest.selected_account_id:
                        latest.selected_loan_id = None
                    latest.selected_loan_id = None
                latest.messages.append(Message(
                    role="assistant", origin="local_assistant",
                    content=(f"Account directory refreshed: {refreshed_preflight.account_count} borrower accounts "
                             f"for {refreshed_preflight.loan_count} explicit loans. Search by account/client name, "
                             "account ID, or linked loan ID, then select the borrower account. No credit evaluation "
                             "or lending decision was made."),
                ))
                store.save(latest)
                session = latest
        rows, total = store.account_candidates(session_id, q, offset, limit)
        return AccountCandidatePage(
            results=[AccountCandidate(account_id=row[0], name=row[1], loan_count=row[2],
                                      loan_ids=row[3].split(chr(31)) if row[3] else []) for row in rows],
            total=total, offset=offset, limit=limit,
            account_count=session.preflight.account_count if session.preflight else 0,
            refreshed_preflight=refreshed_preflight,
        )

    @application.put("/api/sessions/{session_id}/account", response_model=Session)
    def select_account(request: Request, session_id: str, body: AccountSelectionRequest):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            if session.preflight is None or not session.preflight.recognized_mtb:
                raise HTTPException(409, "Run local validation before selecting a borrower account.")
            if not store.has_account_candidate(session_id, body.account_id):
                raise HTTPException(404, "This account was not found among the validated borrower accounts.")
            session.selected_account_id = body.account_id
            session.selected_loan_id = None
            session.report = None
            session.status = "ready"
            session.steps = [Step(agent=agent) for agent in AGENTS]
            store.save(session)
        return session

    @application.get("/api/sessions/{session_id}/loans", response_model=LoanCandidatePage)
    def search_loans(request: Request, session_id: str, q: str = Query("", max_length=128),
                     offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        store: Store = request.app.state.store
        session = require_session(store, session_id)
        if session.preflight is None or not session.preflight.recognized_mtb:
            raise HTTPException(409, "Run local validation before searching detected loan numbers.")
        rows, total = store.loan_candidates(session_id, q, offset, limit)
        return LoanCandidatePage(
            results=[LoanCandidate(loan_id=row[0], account_id=row[1], account_name=row[2]) for row in rows],
            total=total, offset=offset, limit=limit,
            loan_count=session.preflight.loan_count if session.preflight else 0,
        )

    @application.put("/api/sessions/{session_id}/loan", response_model=Session)
    def select_loan(request: Request, session_id: str, body: LoanSelectionRequest):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            if session.preflight is None or not session.preflight.recognized_mtb:
                raise HTTPException(409, "Run local validation before selecting a loan number.")
            account_id = store.loan_candidate_account(session_id, body.loan_id)
            if account_id is None:
                raise HTTPException(404, "This loan number was not found uniquely among validated explicit loan records.")
            session.selected_loan_id = body.loan_id
            session.selected_account_id = account_id
            session.report = None
            session.status = "ready"
            session.steps = [Step(agent=agent) for agent in AGENTS]
            session.messages.append(Message(
                role="assistant", origin="local_assistant",
                content=(f"Selected explicit loan number {body.loan_id} and its linked borrower account {account_id}. "
                         "Processing will scope to this loan using its validated explicit relationships. "
                         "No credit evaluation or lending decision has been made."),
            ))
            store.save(session)
        return session

    @application.post("/api/sessions/{session_id}/assistant/inspect", response_model=Session)
    def assistant_inspect(request: Request, session_id: str, body: InspectRequest):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            inspection = inspect_loan(session, body)
            session.messages.append(Message(role="assistant", origin="local_assistant", inspection=inspection,
                content=f"Local {body.topic} lookup for loan {body.loan_id}. This is a bounded view of saved deterministic evidence, not a new evaluation. No provider call or lending decision."))
            store.save(session)
        return session

    @application.post("/api/sessions/{session_id}/assistant/loan-review", response_model=Session)
    def assistant_loan_review(request: Request, session_id: str, body: LoanReviewRequest):
        store: Store = request.app.state.store
        with store.lock:
            snapshot = require_session(store, session_id)
            editable(snapshot)
            require_loan(snapshot, body.loan_id)
            assert snapshot.report is not None
            if snapshot.updated_at != body.expected_updated_at or snapshot.report.id != body.report_id:
                raise HTTPException(409, "Report or session changed. Review the current loan and confirm again.")
            if len(snapshot.loan_ai_reviews) >= 20:
                raise HTTPException(409, "This session has reached its 20 selected-loan AI review limit.")
        try:
            review = review_selected_loan(snapshot, body.loan_id, body.preferred_evidence_ref)
        except LLMError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        with store.lock:
            latest = require_session(store, session_id)
            editable(latest)
            if (latest.updated_at != snapshot.updated_at or latest.report is None
                    or latest.report.id != body.report_id):
                raise HTTPException(409, "Session changed during the Claude request. Output was not saved; the provider call may have incurred charges.")
            latest.loan_ai_reviews.append(review)
            store.save(latest)
        return latest

    @application.post("/api/sessions/{session_id}/assistant/notes", response_model=Session)
    def assistant_note(request: Request, session_id: str, body: ReviewNoteRequest):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            require_loan(session, body.loan_id)
            if session.report is None or session.report.id != body.report_id:
                raise HTTPException(409, "Report changed; review the current report before recording a note.")
            if not body.text.strip():
                raise HTTPException(422, "A review note must not be blank.")
            if len(session.review_notes) >= 100:
                raise HTTPException(409, "This session has reached its 100-note limit.")
            session.review_notes.append(ReviewNote(report_id=body.report_id, loan_id=body.loan_id,
                text=body.text.strip(), kind=body.kind))
            session.messages.append(Message(role="assistant", origin="local_assistant",
                content="Review note recorded as an unverified user assertion. No rule status, source evidence, policy conflict or lending authorization was changed."))
            store.save(session)
        return session

    def begin_processing(request: Request, session_id: str, expected_updated_at: str | None = None):
        store: Store = request.app.state.store
        with store.lock:
            session = require_session(store, session_id)
            editable(session)
            if expected_updated_at is not None and session.updated_at != expected_updated_at:
                raise HTTPException(409, "Session changed after confirmation was prepared. Inspect the current inputs and confirm again.")
            if any(document.extension == ".xlsx" for document in session.documents) and session.preflight is None:
                raise HTTPException(409, "Run local validation on XLSX files before starting the review.")
            recognized_mtb = bool(session.preflight and session.preflight.recognized_mtb)
            if session.policy_rules and not recognized_mtb:
                raise HTTPException(409, "Validate the uploaded workbooks before starting the account-scoped review.")
            if recognized_mtb and (len(session.documents) != 3
                                   or not session.preflight.query_file_set_valid
                                   or session.preflight.query_roles != [1, 2, 4]
                                   or session.preflight.missing_query_roles
                                   or session.preflight.duplicate_query_roles):
                raise HTTPException(409, "Upload exactly one workbook each for Query 1, Query 2 and Query 4 before processing.")
            if recognized_mtb and session.preflight and session.preflight.account_count and (
                not session.selected_loan_id and not session.selected_account_id
            ):
                raise HTTPException(409, "Search and select an explicit loan number before starting the review.")
            if recognized_mtb and session.selected_loan_id:
                linked_account = store.loan_candidate_account(session.id, session.selected_loan_id)
                if (linked_account is None or linked_account != session.selected_account_id
                        or not store.has_loan_candidate(session.id, linked_account, session.selected_loan_id)):
                    raise HTTPException(409, "Selected loan is not uniquely linked to the selected borrower account. Validate the workbooks again.")
            try:
                IntakeAgent().check(session)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            # Processing changes results, not the validated files/mode or account directory.
            validated_preflight = session.preflight
            invalidate(session, clear_selection=False)
            session.preflight = validated_preflight
            session.status = "processing"
            store.save(session)
            request.app.state.executor.submit(process_session, store, session.id)
        return session

    @application.post("/api/sessions/{session_id}/process", response_model=Session, status_code=202)
    def start_processing(request: Request, session_id: str):
        return begin_processing(request, session_id)

    @application.post("/api/sessions/{session_id}/assistant/process", response_model=Session, status_code=202)
    def assistant_process(request: Request, session_id: str, body: GuidedProcessRequest):
        return begin_processing(request, session_id, body.expected_updated_at)

    @application.get("/api/sessions/{session_id}/underwriting", response_model=UnderwritingBatch)
    def download_underwriting(request: Request, session_id: str, format: Literal["json", "csv"] = "json"):
        session = require_session(request.app.state.store, session_id)
        if session.status != "completed" or session.report is None or session.report.underwriting is None:
            raise HTTPException(409, "Complete an underwriting review before downloading results.")
        batch = session.report.underwriting
        headers = {"Content-Disposition": f'attachment; filename="agent-underwriting-{session.id}.{format}"',
                   "X-Content-Type-Options": "nosniff"}
        if format == "json":
            # Never convert Decimal money through JSONResponse/float encoding.
            return Response(batch.model_dump_json(), media_type="application/json", headers=headers)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["loan_id", "borrower_account_id", "mode", "policy_version", "currency",
                 "decision_status",
                 "eligibility_route", "credit_assessment", "authorization", "strategy_branch", "implementation_guide_diagnostics",
                 "recommendation_path", "final_action", "human_review_required", "normalized_amounts_usd",
                 "rule_results", "source_evidence", "missing_inputs", "conflicts", "explanation",
                 "data_quality_issues", "mapping_warnings", "unmapped_source_fields", "unavailable_inputs",
                 "source_document", "source_available",
                 "implementation_guide_source_document", "implementation_guide_source_sha256",
                 "implementation_guide_available", "expanded_spec", "expanded_spec_available",
                 "expanded_spec_source_document", "expanded_spec_source_sha256"])

        def safe_cell(value: object) -> str:
            text = str(value)
            # Spreadsheet programs may ignore whitespace before formula prefixes.
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text

        for loan in batch.loan_results:
            data = loan.model_dump(mode="json")
            writer.writerow([safe_cell(value) for value in [
                                loan.loan_id, loan.borrower_account_id or "", batch.mode, batch.policy_version, batch.currency,
                                loan.decision_status,
                                loan.eligibility_route or "", loan.credit_assessment or "", loan.authorization,
                                loan.strategy_branch or "",
                                json.dumps(loan.normalized_data.get("implementation_guide_diagnostics", {}), ensure_ascii=False),
                                loan.recommendation_path, loan.final_action, loan.human_review_required,
                                *[json.dumps(data[key], ensure_ascii=False) for key in
                                    ("normalized_amounts_usd", "rule_results", "source_evidence", "missing_inputs", "conflicts")],
                                loan.explanation, json.dumps(batch.data_quality_issues),
                                json.dumps(batch.mapping_warnings), json.dumps(batch.unmapped_source_fields),
                                json.dumps(batch.unavailable_inputs), batch.source_document, batch.source_available,
                                batch.implementation_guide_source_document, batch.implementation_guide_source_sha256,
                                batch.implementation_guide_available,
                                loan.expanded_spec.model_dump_json() if loan.expanded_spec is not None else "null",
                                batch.expanded_spec_available, batch.expanded_spec_source_document,
                                batch.expanded_spec_source_sha256,
            ]])
        return Response(stream.getvalue(), media_type="text/csv", headers=headers)

    @application.get("/api/sessions/{session_id}/report", response_class=HTMLResponse)
    def download_report(request: Request, session_id: str):
        session = require_session(request.app.state.store, session_id)
        if session.status != "completed" or session.report is None:
            raise HTTPException(409, "Complete a review before downloading a report.")
        return HTMLResponse(render_report(session), headers={
            "Content-Disposition": f'attachment; filename="agent-report-{session.id}.html"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        })

    return application


app = create_app()