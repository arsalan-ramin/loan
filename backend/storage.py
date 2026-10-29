"""SQLite is authoritative; Chroma provides persistent, session-scoped retrieval."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import threading
from pathlib import Path

import chromadb
from chromadb.api.types import Where

from .models import Session, now


def embed(text: str) -> list[float]:
    """Offline feature-hashing embeddings; no model downloads or API keys.

    Lexical rather than neural semantic retrieval. Stable hashes survive restarts.
    """
    vector = [0.0] * 384
    for token in re.findall(r"\w+", text.casefold()):
        digest = hashlib.sha256(token.encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % len(vector)] += 1.0
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        (root / "uploads").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db_path = root / "sessions.sqlite3"
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, updated_at TEXT, payload TEXT)")
            conn.execute("""CREATE TABLE IF NOT EXISTS account_candidates (
                session_id TEXT NOT NULL, account_id TEXT NOT NULL, name TEXT, loan_count INTEGER NOT NULL,
                PRIMARY KEY (session_id, account_id)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS account_candidates_name ON account_candidates (session_id, name COLLATE NOCASE, account_id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS account_candidate_loans (
                session_id TEXT NOT NULL, account_id TEXT NOT NULL, loan_id TEXT NOT NULL,
                PRIMARY KEY (session_id, account_id, loan_id)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS account_candidate_loans_lookup ON account_candidate_loans (session_id, loan_id, account_id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS loan_directory (
                session_id TEXT NOT NULL, loan_id TEXT NOT NULL, account_id TEXT,
                PRIMARY KEY (session_id, loan_id)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS loan_directory_lookup ON loan_directory (session_id, loan_id COLLATE NOCASE)")
        self.chroma = chromadb.PersistentClient(path=str(root / "chroma"))
        self.vectors = self.chroma.get_or_create_collection(
            name="folio_memory", embedding_function=None, metadata={"hnsw:space": "cosine"}
        )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def save(self, session: Session) -> None:
        session.updated_at = now()
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)",
                         (session.id, session.updated_at, session.model_dump_json()))

    def get(self, session_id: str) -> Session | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return Session.model_validate_json(row[0]) if row else None

    def list(self) -> list[Session]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM sessions ORDER BY updated_at DESC").fetchall()
        return [Session.model_validate_json(row[0]) for row in rows]

    def replace_account_candidates(self, session_id: str, candidates, loan_candidates=None) -> None:
        """Replace both directories in one transaction; old callers may supply accounts only."""
        candidates = list(candidates)
        with self.connect() as conn:
            conn.execute("DELETE FROM account_candidates WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM account_candidate_loans WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM loan_directory WHERE session_id = ?", (session_id,))
            conn.executemany(
                "INSERT INTO account_candidates (session_id, account_id, name, loan_count) VALUES (?, ?, ?, ?)",
                [(session_id, item.account_id, item.name, item.loan_count) for item in candidates],
            )
            conn.executemany(
                "INSERT INTO account_candidate_loans (session_id, account_id, loan_id) VALUES (?, ?, ?)",
                [(session_id, item.account_id, loan_id) for item in candidates for loan_id in item.loan_ids],
            )
            if loan_candidates is None:
                conn.execute(
                    "INSERT INTO loan_directory (session_id, loan_id, account_id) "
                    "SELECT session_id, loan_id, CASE WHEN COUNT(DISTINCT account_id)=1 THEN MIN(account_id) END "
                    "FROM account_candidate_loans WHERE session_id=? GROUP BY session_id, loan_id",
                    (session_id,),
                )
            else:
                conn.executemany(
                    "INSERT INTO loan_directory (session_id, loan_id, account_id) VALUES (?, ?, ?)",
                    [(session_id, item.loan_id, item.account_id) for item in loan_candidates],
                )

    def account_candidates(self, session_id: str, query: str = "", offset: int = 0, limit: int = 50):
        term = query.strip().casefold()
        with self.connect() as conn:
            if term:
                pattern = term.replace("%", "\\%").replace("_", "\\_") + "%"
                where = ("session_id = ? AND (account_id LIKE ? ESCAPE '\\' COLLATE NOCASE "
                         "OR name LIKE ? ESCAPE '\\' COLLATE NOCASE OR EXISTS ("
                         "SELECT 1 FROM account_candidate_loans l WHERE l.session_id = account_candidates.session_id "
                         "AND l.account_id = account_candidates.account_id AND l.loan_id LIKE ? ESCAPE '\\' COLLATE NOCASE))")
                params = (session_id, pattern, pattern, pattern)
            else:
                where = "session_id = ?"
                params = (session_id,)
            total = conn.execute(f"SELECT COUNT(*) FROM account_candidates WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                "SELECT account_id, name, loan_count, "
                "(SELECT group_concat(loan_id, char(31)) FROM account_candidate_loans l "
                "WHERE l.session_id = account_candidates.session_id AND l.account_id = account_candidates.account_id) "
                "FROM account_candidates "
                f"WHERE {where} ORDER BY name COLLATE NOCASE, account_id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return rows, total

    def loan_candidates(self, session_id: str, query: str = "", offset: int = 0, limit: int = 50):
        term = query.strip().casefold()
        with self.connect() as conn:
            if term:
                pattern = term.replace("%", "\\%").replace("_", "\\_") + "%"
                where = ("l.session_id = ? AND (l.loan_id LIKE ? ESCAPE '\\' COLLATE NOCASE "
                         "OR l.account_id LIKE ? ESCAPE '\\' COLLATE NOCASE "
                         "OR a.name LIKE ? ESCAPE '\\' COLLATE NOCASE)")
                params = (session_id, pattern, pattern, pattern)
            else:
                where = "l.session_id = ?"
                params = (session_id,)
            total = conn.execute(
                "SELECT COUNT(*) FROM loan_directory l "
                "LEFT JOIN account_candidates a ON a.session_id=l.session_id AND a.account_id=l.account_id "
                f"WHERE {where}", params,
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT l.loan_id, l.account_id, a.name FROM loan_directory l "
                "LEFT JOIN account_candidates a ON a.session_id=l.session_id AND a.account_id=l.account_id "
                f"WHERE {where} ORDER BY l.loan_id COLLATE NOCASE, l.account_id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return rows, total

    def loan_candidate_account(self, session_id: str, loan_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT account_id FROM loan_directory WHERE session_id=? AND loan_id=?",
                (session_id, loan_id),
            ).fetchone()
        return row[0] if row else None

    def has_loan_candidate(self, session_id: str, account_id: str | None, loan_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM loan_directory WHERE session_id=? AND account_id IS ? AND loan_id=?",
                (session_id, account_id, loan_id),
            ).fetchone()
        return row is not None

    def loan_candidate_exists(self, session_id: str, loan_id: str) -> bool:
        """Existence is independent of whether the borrower is known."""
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM loan_directory WHERE session_id=? AND loan_id=?",
                                (session_id, loan_id)).fetchone() is not None

    def has_account_candidate(self, session_id: str, account_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM account_candidates WHERE session_id = ? AND account_id = ?",
                (session_id, account_id),
            ).fetchone()
        return row is not None

    def forget_account_candidates(self, session_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM account_candidates WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM account_candidate_loans WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM loan_directory WHERE session_id = ?", (session_id,))

    def remember(self, session_id: str, item_id: str, text: str, kind: str, source: str = "") -> None:
        if not text.strip():
            return
        self.vectors.upsert(ids=[f"{session_id}:{item_id}"], documents=[text], embeddings=[embed(text)],
                            metadatas=[{"session_id": session_id, "kind": kind, "source": source}])

    def forget_documents(self, session_id: str) -> None:
        self.vectors.delete(where={"$and": [{"session_id": session_id}, {"kind": "document"}]})

    def recall(self, session_id: str, query: str, kind: str | None = None) -> list[tuple[str, str]]:
        where: Where = ({"$and": [{"session_id": session_id}, {"kind": kind}]} if kind else {"session_id": session_id})
        result = self.vectors.query(query_embeddings=[embed(query)], n_results=4, where=where,
                                    include=["documents", "metadatas", "distances"])
        texts = (result["documents"] or [[]])[0]
        metas = (result["metadatas"] or [[]])[0]
        distances = (result["distances"] or [[]])[0]
        return [(str(meta.get("source", "")), text) for text, meta, distance in zip(texts, metas, distances)
                if distance < 0.95]