import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL,
                    digest TEXT NOT NULL, metadata TEXT NOT NULL, UNIQUE(workspace, digest)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
                    document_id UNINDEXED, location UNINDEXED, name, text, tokenize='porter unicode61'
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL, role TEXT NOT NULL,
                    text TEXT NOT NULL, sources TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workspace_messages ON messages(workspace, id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def documents(self, workspace: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id, name, metadata FROM documents WHERE workspace=? ORDER BY rowid DESC", (workspace,))
            return [dict(id=row["id"], name=row["name"], metadata=json.loads(row["metadata"])) for row in rows]

    def save_document(self, workspace: str, name: str, metadata: dict, passages: list[tuple[str, str]]) -> bool:
        doc_id = uuid4().hex
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?) ON CONFLICT(workspace, digest) DO NOTHING",
                (doc_id, workspace, name, metadata["sha256"], json.dumps(metadata)),
            )
            if not cursor.rowcount:
                return False
            db.executemany("INSERT INTO passages VALUES (?, ?, ?, ?)",
                           ((doc_id, location, name, text) for location, text in passages))
        return True

    def remove_document(self, workspace: str, doc_id: str) -> bool:
        with self.connect() as db:
            if not db.execute("DELETE FROM documents WHERE id=? AND workspace=?", (doc_id, workspace)).rowcount:
                return False
            db.execute("DELETE FROM passages WHERE document_id=?", (doc_id,))
            # Prior answers may contain excerpts from the removed document.
            db.execute("DELETE FROM messages WHERE workspace=?", (workspace,))
        return True

    def history(self, workspace: str, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id, role, text, sources FROM messages WHERE workspace=? ORDER BY id DESC LIMIT ?",
                              (workspace, limit)).fetchall()
            return [dict(id=row["id"], role=row["role"], text=row["text"], sources=json.loads(row["sources"]))
                    for row in reversed(rows)]

    def save_exchange(self, workspace: str, question: str, answer: str, sources: list[dict], expected_ids: set[str]):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = {row["id"] for row in db.execute("SELECT id FROM documents WHERE workspace=?", (workspace,))}
            if ids != expected_ids:
                return None
            user = db.execute("INSERT INTO messages(workspace,role,text,sources) VALUES (?, 'user', ?, '[]')",
                              (workspace, question))
            agent = db.execute("INSERT INTO messages(workspace,role,text,sources) VALUES (?, 'assistant', ?, ?)",
                               (workspace, answer, json.dumps(sources)))
            return [
                dict(id=user.lastrowid, role="user", text=question, sources=[]),
                dict(id=agent.lastrowid, role="assistant", text=answer, sources=sources),
            ]

    def clear_history(self, workspace: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM messages WHERE workspace=?", (workspace,))

    def search(self, workspace: str, expression: str, doc_ids: list[str], limit: int = 8) -> list[dict]:
        if not expression:
            return []
        restriction = " AND d.id IN (" + ",".join("?" for _ in doc_ids) + ")" if doc_ids else ""
        with self.connect() as db:
            rows = db.execute("""
                SELECT d.id AS document_id, d.name, p.location, p.text
                FROM passages p JOIN documents d ON p.document_id = d.id
                WHERE passages MATCH ? AND d.workspace=?""" + restriction +
                " ORDER BY bm25(passages, 0, 0, 0.25, 1) LIMIT ?",
                (expression, workspace, *doc_ids, limit))
            return [dict(row) for row in rows]
