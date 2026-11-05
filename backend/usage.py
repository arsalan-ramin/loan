"""Persistent provider-reported usage, independent of replaceable loan datasets."""

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4


@dataclass(frozen=True)
class UsageContext:
    ledger: "UsageLedger | None" = None
    http_request_id: str | None = None
    session_id: str | None = None
    loan_id: str | None = None
    batch_id: str | None = None
    operation: str = "unspecified"


CURRENT_USAGE: ContextVar[UsageContext] = ContextVar("llm_usage", default=UsageContext())


@contextmanager
def usage_scope(**values):
    token = CURRENT_USAGE.set(replace(CURRENT_USAGE.get(), **values))
    try:
        yield
    finally:
        CURRENT_USAGE.reset(token)


class UsageLedger:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "usage.sqlite3"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    http_request_id TEXT, session_id TEXT, loan_id TEXT, batch_id TEXT,
                    operation TEXT NOT NULL, model TEXT NOT NULL,
                    status TEXT NOT NULL, category TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    duration_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
                    cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER
                );
                CREATE INDEX IF NOT EXISTS llm_usage_time ON llm_usage (started_at DESC, id);
                CREATE INDEX IF NOT EXISTS llm_usage_session ON llm_usage (session_id, started_at DESC, id);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def recover_interrupted(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE llm_usage SET status='interrupted', category='server_restart', "
                         "finished_at=? WHERE status='running'", (datetime.now(timezone.utc).isoformat(),))

    def start(self, request_id: str, model: str, context: UsageContext) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO llm_usage (id, started_at, http_request_id, session_id, loan_id, batch_id, "
                "operation, model, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')",
                (request_id, datetime.now(timezone.utc).isoformat(), context.http_request_id, context.session_id, context.loan_id,
                 context.batch_id, context.operation, model),
            )

    def finish(self, invocation: "Invocation") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE llm_usage SET finished_at=?, status=?, category=?, attempts=?, duration_ms=?, "
                "input_tokens=?, output_tokens=?, cache_creation_input_tokens=?, cache_read_input_tokens=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), invocation.status, invocation.category, invocation.attempts,
                 round((perf_counter() - invocation.started) * 1000, 3),
                 *(invocation.tokens[key] for key in TOKEN_FIELDS), invocation.id),
            )

    def page(self, session_id: str | None = None, offset: int = 0, limit: int = 25) -> dict:
        where, params = ("WHERE session_id=?", [session_id]) if session_id is not None else ("", [])
        sums = ", ".join(f"COALESCE(SUM({key}), 0) AS {key}" for key in TOKEN_FIELDS)
        with self.connect() as conn:
            # One snapshot keeps totals and rows consistent while batch workers write.
            conn.execute("BEGIN")
            totals = dict(conn.execute(
                f"SELECT COUNT(*) AS requests, {sums}, "
                "COALESCE(SUM(input_tokens IS NOT NULL AND output_tokens IS NOT NULL), 0) AS reported_requests, "
                "COALESCE(SUM(status='running'), 0) AS running_requests, "
                "COALESCE(SUM(status IN ('failed','interrupted')), 0) AS failed_requests "
                f"FROM llm_usage {where}", params,
            ).fetchone())
            rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM llm_usage {where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )]
        totals["unreported_requests"] = totals["requests"] - totals["reported_requests"]
        totals["total_tokens"] = sum(totals[key] for key in TOKEN_FIELDS)
        for row in rows:
            row["total_tokens"] = (sum(row[key] or 0 for key in TOKEN_FIELDS)
                                   if row["input_tokens"] is not None and row["output_tokens"] is not None else None)
        return {"totals": totals, "requests": rows, "total": totals["requests"], "offset": offset, "limit": limit}


TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class Invocation:
    def __init__(self, started: float):
        self.id = str(uuid4())
        self.started = started
        self.status = "failed"
        self.category = "internal_error"
        self.attempts = 0
        self.tokens: dict[str, int | None] = dict.fromkeys(TOKEN_FIELDS)
