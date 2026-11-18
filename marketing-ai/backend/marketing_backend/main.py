import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from .assistant import ModelError, answer_question, ollama_answer
from .extraction import MAX_BYTES, extract
from .store import Store

COOKIE = "marketing_workspace"
MAX_FILES = 5
MAX_UPLOAD = MAX_FILES * MAX_BYTES + 128 * 1024
UI_ORIGINS = {"http://127.0.0.1:5174", "http://localhost:5174"}
logger = logging.getLogger(__name__)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    document_ids: list[str] = Field(default_factory=list, max_length=50)


def workspace(request: Request) -> str:
    value = request.cookies.get(COOKIE, "")
    if re.fullmatch(r"[a-zA-Z0-9_-]{43}", value):
        return value
    raise HTTPException(409, "Open the workspace before making changes.")


def create_app(db_path: Path | None = None) -> FastAPI:
    model = os.getenv("MARKETING_OLLAMA_MODEL", "").strip()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = Store(db_path or Path(__file__).resolve().parents[2] / "data" / "workspace.sqlite3")
        yield

    app = FastAPI(title="Marketing AI API", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin not in UI_ORIGINS and origin != f"http://{request.headers.get('host')}":
                return JSONResponse({"detail": "Open the app on its local address before making changes."}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/workspace")
    def get_workspace(request: Request, response: Response):
        try:
            key = workspace(request)
        except HTTPException:
            key = secrets.token_urlsafe(32)
            response.set_cookie(COOKIE, key, httponly=True, samesite="strict", max_age=31536000)
        store: Store = request.app.state.store
        return {"documents": store.documents(key), "messages": store.history(key),
                "answer_mode": "local-model" if model else "excerpts"}

    @app.post("/api/documents")
    async def upload(request: Request):
        key = workspace(request)
        content_length = request.headers.get("content-length", "")
        if not content_length.isascii() or not content_length.isdecimal() or len(content_length) > 10:
            raise HTTPException(411, "A valid upload size is required.")
        if int(content_length) > MAX_UPLOAD:
            raise HTTPException(413, "Upload at most five files of up to 12 MB each.")
        if request.headers.get("content-type", "").split(";")[0] != "multipart/form-data":
            raise HTTPException(415, "Send files as multipart form data.")
        store: Store = request.app.state.store
        errors: list[dict] = []
        added = 0
        async with request.form(max_files=MAX_FILES, max_fields=0) as form:
            files = form.getlist("files")
            if not files or any(not isinstance(file, UploadFile) for file in files):
                raise HTTPException(400, "Choose at least one file to upload.")
            for file in files:
                name = PurePosixPath((file.filename or "Unnamed").replace("\\", "/")).name[:240]
                try:
                    metadata, passages = await run_in_threadpool(extract, name, await file.read(MAX_BYTES + 1))
                except ValueError as exc:
                    errors.append({"name": name, "detail": str(exc)})
                    continue
                if await run_in_threadpool(store.save_document, key, name, metadata, passages):
                    added += 1
                else:
                    errors.append({"name": name, "detail": "This file is already indexed in this workspace."})
        return {"added": added, "errors": errors, "documents": await run_in_threadpool(store.documents, key)}

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def remove_document(request: Request, doc_id: str):
        if not request.app.state.store.remove_document(workspace(request), doc_id):
            raise HTTPException(404, "Document not found in this workspace.")

    @app.post("/api/chat")
    def chat(request: Request, payload: Question):
        key = workspace(request)
        question = payload.question.strip()
        if not question:
            raise HTTPException(422, "Enter a question.")
        store: Store = request.app.state.store
        ids = {doc["id"] for doc in store.documents(key)}
        if not ids:
            raise HTTPException(409, "Upload a readable document before asking a question.")
        if not set(payload.document_ids) <= ids:
            raise HTTPException(404, "A selected document is not in this workspace.")
        try:
            answer, sources = answer_question(store, key, question, store.history(key, 8), payload.document_ids,
                                              partial(ollama_answer, model) if model else None)
        except ModelError as exc:
            logger.warning("Local answer generation failed: %s", exc)
            raise HTTPException(502, str(exc)) from exc
        messages = store.save_exchange(key, question, answer, sources, ids)
        if messages is None:
            raise HTTPException(409, "The document list changed during this answer. Ask again.")
        return {"messages": messages}

    @app.delete("/api/chat", status_code=204)
    def clear_chat(request: Request):
        request.app.state.store.clear_history(workspace(request))

    return app


app = create_app()
