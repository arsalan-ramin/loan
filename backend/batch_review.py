"""Whole stored-dataset batch review.

Each loan's Approved / Decline / Refer status comes from the deterministic TEST
decision flow; its rationale comes from the same bounded Claude review used for a
single selected loan. Only the latest run is kept, in the stored-dataset database,
so replacing the stored workbooks also clears the batch results.
"""

import hashlib
import logging
import os
import re
import shutil
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font

from .llm import AnthropicLLM, LLMError
from .loan_dataset import LoanDataset
from .loan_review import review_selected_loan
from .models import Document, Report, Session
from .underwriting_engine import evaluate_underwriting

logger = logging.getLogger(__name__)

STATUS_LABELS = {"Approve": "Approved", "Decline": "Decline", "Refer": "Refer"}
EXPORT_COLUMNS = ("Loan ID", "Account ID", "Status", "Rationale / result")


class BatchError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class _Stop(Exception):
    """Abort the whole run (provider unusable or stored data replaced)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def readable_rationale(text: str) -> str:
    """Put each labelled section of the Claude explanation on its own line."""
    text = re.sub(r"\s*\b(Why|What is unresolved|Next steps):", r"\n\1:", text.strip())
    return re.sub(r"\n{2,}", "\n", text).strip()


def safe_cell(value: str | None) -> str:
    text = value or ""
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


class BatchReviewer:
    def __init__(self, dataset: LoanDataset, concurrency: int | None = None):
        self.dataset = dataset
        self.workroot = dataset.db_path.parent / "batch_run"
        if concurrency is None:
            try:
                concurrency = int(os.getenv("BATCH_REVIEW_CONCURRENCY", "4"))
            except ValueError:
                concurrency = 4
        self.concurrency = max(1, min(16, concurrency))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    # ---- state ------------------------------------------------------------------------

    def recover_interrupted(self) -> None:
        with self.dataset.connect() as conn:
            conn.execute("UPDATE batch_run SET status='interrupted', finished_at=?, error=? WHERE status='running'",
                         (_now(), "The server restarted during the batch review. Resume to process the remaining loans."))
        shutil.rmtree(self.workroot, ignore_errors=True)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        dataset = self.dataset.status()
        with self.dataset.connect() as conn:
            run = conn.execute("SELECT status, started_at, finished_at, total, dataset_updated_at, model, error "
                               "FROM batch_run WHERE id = 1").fetchone()
            counts = dict(conn.execute("SELECT COALESCE(status, 'Error'), COUNT(*) FROM batch_results "
                                       "GROUP BY COALESCE(status, 'Error')").fetchall())
            errors = conn.execute("SELECT COUNT(*) FROM batch_results WHERE error IS NOT NULL").fetchone()[0]
        processed = sum(counts.values())
        state = run[0] if run else "none"
        if state == "running" and not self.running():
            state = "interrupted"
        return {
            "status": state,
            "started_at": run[1] if run else None,
            "finished_at": run[2] if run else None,
            "total": run[3] if run else 0,
            "processed": processed,
            "approved": counts.get("Approved", 0),
            "declined": counts.get("Decline", 0),
            "referred": counts.get("Refer", 0),
            "errors": errors,
            "model": run[5] if run else None,
            "error": run[6] if run else None,
            "dataset_status": dataset["status"],
            "dataset_loan_count": dataset["loan_count"],
            "dataset_current": bool(run) and run[4] == dataset["updated_at"] and dataset["status"] == "ready",
            "concurrency": self.concurrency,
        }

    def results(self, offset: int = 0, limit: int = 50, status: str | None = None) -> dict:
        where, params = "", []
        if status == "Error":
            where = "WHERE error IS NOT NULL"
        elif status:
            where, params = "WHERE status = ?", [status]
        with self.dataset.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM batch_results {where}", params).fetchone()[0]
            rows = conn.execute(
                "SELECT loan_id, account_id, status, branch, rationale, engine_rationale, error, processed_at "
                f"FROM batch_results {where} ORDER BY seq LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        keys = ("loan_id", "account_id", "status", "branch", "rationale", "engine_rationale", "error", "processed_at")
        return {"results": [dict(zip(keys, row)) for row in rows], "total": total, "offset": offset, "limit": limit}

    # ---- control ----------------------------------------------------------------------

    def start(self, *, resume: bool = False) -> dict:
        with self._lock:
            if self.running():
                raise BatchError("A batch review is already running.")
            dataset = self.dataset.status()
            if dataset["status"] != "ready" or not dataset["loan_count"]:
                raise BatchError("No stored Query 1, 2 and 4 data is ready. Upload the three workbooks first.")
            if not os.getenv("ANTHROPIC_API_KEY", "").strip():
                raise BatchError("Anthropic is not configured. Set ANTHROPIC_API_KEY in the backend environment "
                                 "and restart the API.", 503)
            with self.dataset.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    run = conn.execute("SELECT dataset_updated_at FROM batch_run WHERE id = 1").fetchone()
                    if resume and (run is None or run[0] != dataset["updated_at"]):
                        raise BatchError("There is no batch review of the current stored data to resume. Start a new run.")
                    if not resume:
                        conn.execute("DELETE FROM batch_results")
                    conn.execute("INSERT OR REPLACE INTO batch_run VALUES (1, 'running', ?, NULL, ?, ?, ?, NULL)",
                                 (_now(), dataset["loan_count"], dataset["updated_at"], AnthropicLLM().model))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, args=(dataset["updated_at"],),
                                            name="batch-review", daemon=True)
            self._thread.start()
        return self.status()

    def cancel(self) -> dict:
        if not self.running():
            raise BatchError("No batch review is running.")
        self._cancel.set()
        return self.status()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- worker -----------------------------------------------------------------------

    def _finish(self, snapshot: str, status: str, error: str | None) -> None:
        with self.dataset.connect() as conn:
            conn.execute("UPDATE batch_run SET status=?, finished_at=?, error=? WHERE id=1 AND dataset_updated_at=?",
                         (status, _now(), error, snapshot))

    def _save(self, row: tuple, snapshot: str) -> bool:
        """Persist one result only while the run still belongs to the current stored data."""
        with self.dataset.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute("SELECT 1 FROM batch_run WHERE id=1 AND dataset_updated_at=? "
                                       "AND dataset_updated_at=(SELECT value FROM dataset_meta WHERE key='updated_at')",
                                       (snapshot,)).fetchone()
                if current:
                    conn.execute("INSERT OR REPLACE INTO batch_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return bool(current)

    def _run(self, snapshot: str) -> None:
        workdir = self.workroot / uuid4().hex
        status, error = "completed", None
        try:
            templates = self.dataset.attach(workdir)
            for document in templates:
                path = workdir / document.id
                document.metadata = {"filename": document.name, "extension": document.extension,
                                     "size_bytes": path.stat().st_size,
                                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            with self.dataset.connect() as conn:
                loans = conn.execute("SELECT loan_id, account_id FROM dataset_loans "
                                     "ORDER BY loan_id COLLATE NOCASE").fetchall()
                done = {row[0] for row in conn.execute("SELECT loan_id FROM batch_results WHERE error IS NULL")}
            pending = iter([(seq, loan_id, account_id) for seq, (loan_id, account_id) in enumerate(loans)
                            if loan_id not in done])
            with ThreadPoolExecutor(self.concurrency, thread_name_prefix="batch-loan") as pool:
                futures: set[Future] = set()

                def submit() -> None:
                    item = next(pending, None) if not self._cancel.is_set() else None
                    if item is not None:
                        futures.add(pool.submit(self._review, templates, *item))

                for _ in range(self.concurrency):
                    submit()
                while futures:
                    finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        futures.discard(future)
                        try:
                            row = future.result()
                        except _Stop as stop:
                            if error is None:
                                status, error = "failed", str(stop)
                            self._cancel.set()
                            continue
                        if not self._save(row, snapshot):
                            status, error = "cancelled", "The stored data was replaced during the batch review."
                            self._cancel.set()
                            continue
                        submit()
            if status == "completed" and self._cancel.is_set():
                status = "cancelled"
        except Exception:
            logger.exception("Batch review failed")
            status, error = "failed", "The batch review failed. Check the server logs, then resume."
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            self._finish(snapshot, status, error)

    def _review(self, templates: list[Document], seq: int, loan_id: str, account_id: str | None) -> tuple:
        documents = [document.model_copy(deep=True) for document in templates]
        if not self.dataset.load_scoped(documents, loan_id):
            raise _Stop("The stored data changed during the batch review. Start a new run.")
        status = branch = rationale = engine_rationale = error = model = None
        try:
            batch = evaluate_underwriting(documents, "TEST", selected_loan_id=loan_id)
            loan = next((item for item in batch.loan_results if item.loan_id == loan_id), None)
            demo = loan.normalized_data.get("demo_decision") if loan is not None else None
            if not isinstance(demo, dict) or demo.get("label") not in STATUS_LABELS:
                error = "The decision flow could not evaluate this loan."
            else:
                status = STATUS_LABELS[demo["label"]]
                branch, engine_rationale = demo.get("branch"), demo.get("rationale")
                session = Session(status="completed", mode="TEST", documents=documents,
                                  report=Report(decision="needs_review", summary="Batch review", findings=[],
                                                document_count=len(documents), underwriting=batch))
                review = review_selected_loan(session, loan_id)
                rationale, model = readable_rationale(review.analysis.explanation), review.model
        except LLMError as exc:
            if exc.status_code == 503:
                # Missing key, failed authentication or unavailable model affects every loan.
                raise _Stop(str(exc)) from None
            error = f"Claude rationale unavailable: {exc}"
        except HTTPException as exc:
            error = str(exc.detail)
        except Exception:
            logger.exception("Batch review of a loan failed")
            error = "This loan could not be reviewed. Check the server logs."
        return (loan_id, seq, account_id, status, branch, rationale, engine_rationale, error, model, _now())

    # ---- export -----------------------------------------------------------------------

    def export_xlsx(self) -> bytes:
        with self.dataset.connect() as conn:
            rows = conn.execute("SELECT loan_id, account_id, status, rationale, error FROM batch_results "
                                "ORDER BY seq").fetchall()
        if not rows:
            raise BatchError("There are no batch results to export yet.")
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Batch results")
        for column, width in zip("ABCD", (20, 20, 12, 110)):
            sheet.column_dimensions[column].width = width
        sheet.freeze_panes = "A2"
        bold, wrap = Font(bold=True), Alignment(wrap_text=True, vertical="top")
        header = []
        for title in EXPORT_COLUMNS:
            cell = WriteOnlyCell(sheet, value=title)
            cell.font = bold
            header.append(cell)
        sheet.append(header)
        for loan_id, account_id, status, rationale, error in rows:
            text = rationale or error or ""
            cells = [WriteOnlyCell(sheet, value=safe_cell(value))
                     for value in (loan_id, account_id, status or "Error", text)]
            for cell in cells:
                cell.alignment = wrap
            sheet.append(cells)
        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()
