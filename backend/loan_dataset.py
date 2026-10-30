"""Shared, indexed store of the latest uploaded MTB Query 1/2/4 workbooks.

Every source row is stored once with the relationship IDs it exposes. A
``(value, key, table)`` index lets a loan's related rows be resolved by
walking explicit IDs instead of re-parsing whole workbooks. Loan and account
directories are indexed for search. A new complete upload clears all stored
data before the replacement set is loaded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from .datasets import mtb_query_ids
from .excel import extract_excel
from .loan_loading import PARENTS, SCOPE_PARENTS, accept, record_mapper
from .models import AccountCandidate, Document, LoanCandidate
from .underwriting_engine import CONFIG_PATH, load_underwriting_config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
REQUIRED_ROLES = (1, 2, 4)
BATCH_SIZE = 2000
_instances: dict[Path, "LoanDataset"] = {}
_instances_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def link_or_copy(source: Path, target: Path) -> None:
    """Hard links share bytes without copying; fall back where unsupported."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


_MAPPING_SOURCES = ("datasets.py", "entity_graph.py", "excel.py", "loan_loading.py", "loan_dataset.py")


@lru_cache(maxsize=4)
def _fingerprint(config_stamp: tuple[int, int]) -> str:
    del config_stamp  # cache key only
    manifest = load_underwriting_config()["mapping_manifest"]
    digest = hashlib.sha256(json.dumps({"schema": SCHEMA_VERSION, "manifest": manifest},
                                       sort_keys=True, default=str).encode())
    for name in _MAPPING_SOURCES:
        digest.update((Path(__file__).with_name(name)).read_bytes())
    return digest.hexdigest()


def fingerprint() -> str:
    """Identifies mapping config and code; a change makes the stored index stale."""
    stat = CONFIG_PATH.stat()
    return _fingerprint((stat.st_mtime_ns, stat.st_size))


class DatasetError(ValueError):
    pass


class LoanDataset:
    def __init__(self, root: Path):
        self.root = root
        self.directory = root / "loan_dataset"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "loan_dataset.sqlite3"
        # Serializes file replacement against session attachment.
        self.files_lock = threading.RLock()
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dataset_meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS dataset_files (
                    role INTEGER PRIMARY KEY, file_id TEXT NOT NULL, name TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dataset_records (
                    id INTEGER PRIMARY KEY, role INTEGER NOT NULL, sheet TEXT NOT NULL,
                    row INTEGER NOT NULL, payload TEXT NOT NULL, text TEXT NOT NULL,
                    formulas INTEGER NOT NULL, mapped TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dataset_keys (
                    record_id INTEGER NOT NULL, tbl TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS dataset_keys_lookup ON dataset_keys (value, key, tbl, record_id);
                CREATE TABLE IF NOT EXISTS dataset_loans (
                    loan_id TEXT PRIMARY KEY, account_id TEXT, account_name TEXT
                );
                CREATE INDEX IF NOT EXISTS dataset_loans_search ON dataset_loans (loan_id COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS dataset_loans_account ON dataset_loans (account_id);
                CREATE INDEX IF NOT EXISTS dataset_loans_account_search
                    ON dataset_loans (account_id COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS dataset_loans_name_search
                    ON dataset_loans (account_name COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS dataset_accounts (
                    account_id TEXT PRIMARY KEY, name TEXT, loan_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS dataset_accounts_name ON dataset_accounts (name COLLATE NOCASE, account_id);
                CREATE INDEX IF NOT EXISTS dataset_accounts_id_search
                    ON dataset_accounts (account_id COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS dataset_account_loans (
                    account_id TEXT NOT NULL, loan_id TEXT NOT NULL, PRIMARY KEY (account_id, loan_id)
                );
                CREATE INDEX IF NOT EXISTS dataset_account_loans_loan ON dataset_account_loans (loan_id, account_id);
                CREATE INDEX IF NOT EXISTS dataset_account_loans_search
                    ON dataset_account_loans (loan_id COLLATE NOCASE, account_id);
                CREATE TABLE IF NOT EXISTS batch_run (
                    id INTEGER PRIMARY KEY CHECK (id = 1), status TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, total INTEGER NOT NULL, dataset_updated_at TEXT, model TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_results (
                    loan_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, account_id TEXT, status TEXT,
                    branch TEXT, rationale TEXT, engine_rationale TEXT, error TEXT, model TEXT,
                    processed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS batch_results_seq ON batch_results (seq);
                CREATE INDEX IF NOT EXISTS batch_results_status ON batch_results (status, seq);
            """)

    @classmethod
    def for_root(cls, root: Path) -> "LoanDataset":
        key = root.resolve()
        with _instances_lock:
            if key not in _instances:
                _instances[key] = cls(root)
            return _instances[key]

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # Autocommit mode; transactions are explicit. Always close (Windows file locks).
        conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        try:
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-65536")
            yield conn
        finally:
            conn.close()

    # ---- status -----------------------------------------------------------------

    def _meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        return dict(conn.execute("SELECT key, value FROM dataset_meta").fetchall())

    def _set_meta(self, conn: sqlite3.Connection, **values) -> None:
        conn.executemany("INSERT OR REPLACE INTO dataset_meta (key, value) VALUES (?, ?)",
                         [(key, None if value is None else str(value)) for key, value in values.items()])

    def status(self) -> dict:
        with self.connect() as conn:
            meta = self._meta(conn)
            files = conn.execute("SELECT role, name, size, sha256 FROM dataset_files ORDER BY role").fetchall()
        state = meta.get("status", "empty")
        if state == "ready" and meta.get("fingerprint") != fingerprint():
            state = "stale"
        return {
            "status": state,
            "updated_at": meta.get("updated_at"),
            "error": meta.get("error"),
            "record_count": int(meta.get("record_count") or 0),
            "loan_count": int(meta.get("loan_count") or 0),
            "account_count": int(meta.get("account_count") or 0),
            "files": [{"query_role": role, "name": name, "size": size, "sha256": sha}
                      for role, name, size, sha in files],
        }

    def recover_interrupted(self) -> None:
        with self.connect() as conn:
            if self._meta(conn).get("status") == "indexing":
                self._set_meta(conn, status="failed", updated_at=_now(),
                               error="Server restarted while indexing. Upload Query 1, 2 and 4 again.")

    def stale_files(self) -> list[tuple[str, Path]] | None:
        """Stored workbooks to re-index when mappings changed since they were indexed."""
        with self.connect() as conn:
            meta = self._meta(conn)
            files = conn.execute("SELECT name, file_id FROM dataset_files ORDER BY role").fetchall()
        if meta.get("status") != "ready" or meta.get("fingerprint") == fingerprint():
            return None
        paths = [(name, self.directory / f"{file_id}.xlsx") for name, file_id in files]
        return paths if paths and all(path.is_file() for _, path in paths) else None

    # ---- ingestion ----------------------------------------------------------------

    def identify_roles(self, files: list[tuple[str, Path]]) -> dict[int, tuple[str, Path]] | None:
        """Return files keyed by query role only for exactly one Query 1, 2 and 4 workbook."""
        if len(files) != len(REQUIRED_ROLES):
            return None
        by_role: dict[int, tuple[str, Path]] = {}
        for name, path in files:
            if Path(name).suffix.casefold() != ".xlsx":
                return None
            try:
                _, metadata = extract_excel(path, include_text=False, headers_only=True)
            except (ValueError, OSError):
                return None
            roles = mtb_query_ids(name, metadata)
            if len(roles) != 1:
                return None
            role = next(iter(roles))
            if role in by_role:
                return None
            by_role[role] = (name, path)
        return by_role if set(by_role) == set(REQUIRED_ROLES) else None

    def is_current(self, files: list[tuple[str, str]]) -> bool:
        """True when this exact (name, sha256) set is already indexed with current mappings."""
        with self.connect() as conn:
            meta = self._meta(conn)
            stored = conn.execute("SELECT name, sha256 FROM dataset_files").fetchall()
        return (meta.get("status") == "ready" and meta.get("fingerprint") == fingerprint()
                and sorted(stored) == sorted(files))

    def replace(self, files: list[tuple[str, Path]], source_session_id: str | None = None) -> bool:
        """Clear all stored data, then load a complete Query 1/2/4 set. Returns False if not a valid set."""
        staging = self.directory / f"staging-{uuid4().hex}"
        staging.mkdir()
        try:
            staged = []
            for name, path in files:
                target = staging / f"{uuid4().hex}.xlsx"
                link_or_copy(path, target)
                staged.append((name, target))
            by_role = self.identify_roles(staged)
            if by_role is None:
                return False
            hashes = {role: _sha256(path) for role, (_, path) in by_role.items()}
            if self.is_current([(name, hashes[role]) for role, (name, _) in by_role.items()]):
                return True
            with self.files_lock:
                self._clear(status="indexing")
                placed = {}
                for role, (name, path) in by_role.items():
                    file_id = uuid4().hex
                    target = self.directory / f"{file_id}.xlsx"
                    path.replace(target)
                    placed[role] = (file_id, name, target)
            try:
                self._index(placed, hashes, source_session_id)
            except Exception as exc:
                logger.exception("Loan dataset indexing failed")
                self._clear(status="failed", error=f"Indexing failed: {type(exc).__name__}. Upload the workbooks again.")
                raise
            return True
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _clear(self, *, status: str, error: str | None = None) -> None:
        with self.files_lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for table in ("dataset_files", "dataset_records", "dataset_keys", "dataset_loans",
                                  "dataset_accounts", "dataset_account_loans", "dataset_meta",
                                  "batch_results", "batch_run"):
                        conn.execute(f"DELETE FROM {table}")
                    self._set_meta(conn, status=status, error=error, updated_at=_now())
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            for path in self.directory.glob("*.xlsx"):
                try:
                    path.unlink()
                except OSError:
                    # Unique names avoid collisions; a later clear retries removal.
                    logger.warning("Could not remove a previous stored workbook; will retry later.")

    def _index(self, placed: dict[int, tuple[str, str, Path]], hashes: dict[int, str],
               source_session_id: str | None) -> None:
        config = load_underwriting_config()
        manifest = config["mapping_manifest"]
        next_id = 1
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for role in REQUIRED_ROLES:
                    file_id, name, path = placed[role]
                    mapper = record_mapper(name, manifest)
                    records: list[tuple] = []
                    keys: list[tuple] = []

                    def flush() -> None:
                        conn.executemany("INSERT INTO dataset_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)", records)
                        conn.executemany("INSERT INTO dataset_keys VALUES (?, ?, ?, ?)", keys)
                        records.clear()
                        keys.clear()

                    def observe(record: dict, headers: list[str], text: str, formulas: int) -> None:
                        nonlocal next_id
                        mapped = {table: {key: sorted(ids) for key, ids in data.items() if ids}
                                  for table, data in mapper(record, headers).items()}
                        mapped = {table: data for table, data in mapped.items() if data}
                        record_id = next_id
                        next_id += 1
                        records.append((record_id, role, record["sheet"], record["row"],
                                        json.dumps({"values": record["values"], "sources": record["sources"]},
                                                   default=str),
                                        text, formulas, json.dumps(mapped)))
                        keys.extend((record_id, table, key, value)
                                    for table, data in mapped.items()
                                    for key, ids in data.items() for value in ids)
                        if len(records) >= BATCH_SIZE:
                            flush()

                    _, metadata = extract_excel(path, include_text=False,
                                                record_filter=lambda record, headers: False,
                                                record_observer=observe)
                    flush()
                    for key in ("records", "data_row_count", "formula_count"):
                        metadata.pop(key, None)
                    conn.execute("INSERT INTO dataset_files VALUES (?, ?, ?, ?, ?, ?)",
                                 (role, file_id, name, hashes[role], path.stat().st_size, json.dumps(metadata)))
                loans, accounts = self._directory(conn, config)
                self._set_meta(conn, status="ready", error=None, updated_at=_now(), fingerprint=fingerprint(),
                               source_session_id=source_session_id, record_count=next_id - 1,
                               loan_count=len(loans), account_count=len(accounts))
                # Planner statistics keep lookups on the key indexes as the data grows.
                conn.execute("ANALYZE")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _directory(self, conn: sqlite3.Connection, config: dict):
        from .assistant_workflow import _discover_accounts

        files = conn.execute("SELECT role, name, metadata FROM dataset_files ORDER BY role").fetchall()

        def documents() -> Iterator[Document]:
            for role, name, metadata in files:
                sheets = json.loads(metadata).get("sheets", [])
                last = 0
                while True:
                    rows = conn.execute(
                        "SELECT id, sheet, row, payload FROM dataset_records WHERE role=? AND id>? ORDER BY id LIMIT ?",
                        (role, last, 5000)).fetchall()
                    if not rows:
                        break
                    last = rows[-1][0]
                    records = [{"sheet": sheet, "row": row, **json.loads(payload)} for _, sheet, row, payload in rows]
                    yield Document(name=name, size=0, extension=".xlsx",
                                   metadata={"sheets": sheets, "records": records})

        loans: list[LoanCandidate] = []
        _, accounts = _discover_accounts(documents(), config, loans)
        conn.executemany("INSERT INTO dataset_accounts VALUES (?, ?, ?)",
                         [(item.account_id, item.name, item.loan_count) for item in accounts])
        conn.executemany("INSERT INTO dataset_account_loans VALUES (?, ?)",
                         [(item.account_id, loan_id) for item in accounts for loan_id in item.loan_ids])
        conn.executemany("INSERT INTO dataset_loans VALUES (?, ?, ?)",
                         [(item.loan_id, item.account_id, item.account_name) for item in loans])
        return loans, accounts

    # ---- session attachment -----------------------------------------------------

    def attach(self, target_directory: Path) -> list[Document]:
        """Materialize the stored workbooks as new session documents (hard-linked when possible)."""
        with self.files_lock:
            state = self.status()
            if state["status"] != "ready":
                raise DatasetError("No stored Query 1, 2 and 4 data is ready. Upload the three workbooks first.")
            with self.connect() as conn:
                files = conn.execute("SELECT file_id, name, size FROM dataset_files ORDER BY role").fetchall()
            target_directory.mkdir(parents=True, exist_ok=True)
            documents = []
            try:
                for file_id, name, size in files:
                    document = Document(name=name, size=size, extension=".xlsx")
                    link_or_copy(self.directory / f"{file_id}.xlsx", target_directory / document.id)
                    documents.append(document)
            except OSError:
                for document in documents:
                    (target_directory / document.id).unlink(missing_ok=True)
                raise
            return documents

    # ---- indexed loan retrieval -----------------------------------------------------

    def load_scoped(self, documents: list[Document], loan_id: str) -> bool:
        """Populate documents with the loan's related rows from the index.

        Returns False (caller streams the files) unless every document is
        byte-identical, by name and SHA-256, to a stored workbook indexed with
        the current mappings.
        """
        if not documents or any(doc.extension != ".xlsx" for doc in documents):
            return False
        with self.connect() as conn:
            conn.execute("BEGIN")  # one consistent snapshot even if a re-index commits meanwhile
            try:
                return self._load_scoped(conn, documents, loan_id)
            finally:
                conn.execute("ROLLBACK")

    def _load_scoped(self, conn: sqlite3.Connection, documents: list[Document], loan_id: str) -> bool:
        meta = self._meta(conn)
        if meta.get("status") != "ready" or meta.get("fingerprint") != fingerprint():
            return False
        stored = {(name, sha): (role, json.loads(metadata)) for role, name, sha, metadata in
                  conn.execute("SELECT role, name, sha256, metadata FROM dataset_files").fetchall()}
        roles: dict[int, tuple[Document, dict]] = {}
        for document in documents:
            match = stored.get((document.name, document.metadata.get("sha256")))
            if match is None or match[0] in roles:
                return False
            roles[match[0]] = (document, match[1])

        manifest = load_underwriting_config()["mapping_manifest"]
        related: dict[str, set[str]] = {table: set() for table in manifest["tables"]}
        related["Loan"] = {loan_id}
        seen: dict[str, set[str]] = {table: set(ids) for table, ids in related.items()}
        children = {}
        for child, keys in SCOPE_PARENTS.items():
            for key in keys:
                children.setdefault(PARENTS[key], []).append((child, key))
        frontier: list[tuple[str | None, str, str]] = [(None, "loan_id", loan_id), ("Loan", "id", loan_id)]
        evaluated: set[int] = set()
        kept: list[int] = []
        role_list = ",".join(str(role) for role in roles)
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS dataset_frontier (tbl TEXT, key TEXT, value TEXT)")
        while frontier:
            conn.execute("DELETE FROM dataset_frontier")
            conn.executemany("INSERT INTO dataset_frontier VALUES (?, ?, ?)", dict.fromkeys(frontier))
            # CROSS JOIN fixes the order: frontier -> key index seek -> record primary key.
            rows = conn.execute(
                "SELECT DISTINCT r.id, r.mapped FROM dataset_frontier f "
                "CROSS JOIN dataset_keys k ON k.value = f.value AND k.key = f.key "
                "AND (f.tbl IS NULL OR k.tbl = f.tbl) "
                f"CROSS JOIN dataset_records r ON r.id = k.record_id WHERE r.role IN ({role_list}) ORDER BY r.id"
            ).fetchall()
            for record_id, mapped_json in rows:
                if record_id in evaluated:
                    continue
                evaluated.add(record_id)
                mapped = {table: {"id": set(), **{key: set(ids) for key, ids in data.items()}}
                          for table, data in json.loads(mapped_json).items()}
                # Every candidate is connected by construction; only an explicit
                # different loan excludes it, which is permanent.
                if accept(mapped, related, loan_id):
                    kept.append(record_id)
            frontier = []
            for table, ids in related.items():
                new = ids - seen.setdefault(table, set())
                if not new:
                    continue
                seen[table].update(new)
                for value in new:
                    frontier.append((table, "id", value))
                    frontier.extend((child, key, value) for child, key in children.get(table, ()))
        conn.execute("DROP TABLE IF EXISTS dataset_frontier")

        conn.execute("CREATE TEMP TABLE IF NOT EXISTS dataset_kept (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO dataset_kept VALUES (?)", [(item,) for item in kept])
        by_role: dict[int, list[tuple]] = {role: [] for role in roles}
        for role, sheet, row, payload, text, formulas in conn.execute(
            "SELECT r.role, r.sheet, r.row, r.payload, r.text, r.formulas FROM dataset_kept k "
            "CROSS JOIN dataset_records r ON r.id = k.id ORDER BY k.id"
        ):
            by_role[role].append((sheet, row, payload, text, formulas))
        conn.execute("DROP TABLE IF EXISTS dataset_kept")

        for role, (document, file_metadata) in roles.items():
            rows = by_role[role]
            lines_by_sheet: dict[str, list[str]] = {}
            records = []
            for sheet, row, payload, text, _ in rows:
                lines_by_sheet.setdefault(sheet, []).append(text)
                records.append({"sheet": sheet, "row": row, **json.loads(payload)})
            parts = []
            for sheet in file_metadata.get("sheets", []):
                parts.append(f"Sheet: {sheet['name']}")
                parts.extend(lines_by_sheet.get(sheet["name"], []))
            content = "\n".join(parts)
            metadata = {**file_metadata, "records": records, "data_row_count": len(records),
                        "formula_count": sum(item[4] for item in rows)}
            document.content = content
            document.metadata = {**document.metadata, **metadata, "scoped_loan_id": loan_id,
                                 "word_count": len(content.split()), "character_count": len(content)}
        return True

    # ---- directory search ---------------------------------------------------------

    @staticmethod
    def _prefix(column: str) -> str:
        # Case-insensitive prefix match as a range so it seeks the NOCASE index.
        return f"({column} COLLATE NOCASE >= :lo AND {column} COLLATE NOCASE < :hi)"

    def search_loans(self, query: str = "", offset: int = 0, limit: int = 50):
        term = query.strip()
        params = {"lo": term, "hi": term + "\U0010ffff", "limit": limit, "offset": offset}
        source = "dataset_loans"
        if term:
            source = ("(SELECT * FROM dataset_loans WHERE rowid IN ("
                      f"SELECT rowid FROM dataset_loans WHERE {self._prefix('loan_id')} "
                      f"UNION SELECT rowid FROM dataset_loans WHERE {self._prefix('account_id')} "
                      f"UNION SELECT rowid FROM dataset_loans WHERE {self._prefix('account_name')}))")
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM {source}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT loan_id, account_id, account_name FROM {source} "
                "ORDER BY loan_id COLLATE NOCASE LIMIT :limit OFFSET :offset", params).fetchall()
        return [LoanCandidate(loan_id=row[0], account_id=row[1], account_name=row[2]) for row in rows], total

    def search_accounts(self, query: str = "", offset: int = 0, limit: int = 50):
        term = query.strip()
        params = {"lo": term, "hi": term + "\U0010ffff", "limit": limit, "offset": offset}
        source = "dataset_accounts"
        if term:
            source = ("(SELECT * FROM dataset_accounts WHERE account_id IN ("
                      f"SELECT account_id FROM dataset_accounts WHERE {self._prefix('account_id')} "
                      f"UNION SELECT account_id FROM dataset_accounts WHERE {self._prefix('name')} "
                      f"UNION SELECT account_id FROM dataset_account_loans WHERE {self._prefix('loan_id')}))")
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM {source}", params).fetchone()[0]
            rows = conn.execute(
                "SELECT a.account_id, a.name, a.loan_count, (SELECT group_concat(loan_id, char(31)) "
                "FROM dataset_account_loans l WHERE l.account_id = a.account_id) "
                f"FROM {source} a ORDER BY a.name COLLATE NOCASE, a.account_id LIMIT :limit OFFSET :offset",
                params).fetchall()
        return [AccountCandidate(account_id=row[0], name=row[1], loan_count=row[2],
                                 loan_ids=sorted(row[3].split(chr(31))) if row[3] else []) for row in rows], total
