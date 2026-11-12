"""
storage.py — saved uploads, jobs and their progress, in SQLite plus files on disk.

Everything lives under one folder (DATA_DIR, or the Railway volume, or data/ locally):

    sheettriage.db              users, uploads, jobs, job_events
    uploads/<upload_id>.csv     the normalized upload
    jobs/<job_id>/output_clean.csv, output_report.csv, review.json

Every upload and job has an owner: a signed-in user's id, or "session:<id>" for a
browser that hasn't signed in. Lookups take the owner, so one person can never load
another person's upload or job; to them it looks like it doesn't exist.
"""
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DEFAULT_DATA_DIR = os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    filename TEXT NOT NULL,
    rows INTEGER NOT NULL,
    columns TEXT NOT NULL,
    demo INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    upload_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    rows INTEGER NOT NULL,
    mode TEXT NOT NULL,
    schema TEXT NOT NULL,
    demo INTEGER NOT NULL DEFAULT 0,
    client TEXT,
    status TEXT NOT NULL,
    error TEXT,
    flagged_rows INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    finished_at REAL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS uploads_by_owner ON uploads (owner, created_at);
CREATE INDEX IF NOT EXISTS jobs_by_owner ON jobs (owner, created_at);
"""

INTERRUPTED_MESSAGE = "This run was cut short when the server restarted. Please upload your file and run it again."

UNFINISHED = ("queued", "running")


class Store:
    def __init__(self, data_dir: str | Path = DEFAULT_DATA_DIR):
        self.dir = Path(data_dir)
        (self.dir / "uploads").mkdir(parents=True, exist_ok=True)
        (self.dir / "jobs").mkdir(parents=True, exist_ok=True)
        # One connection shared by the web server and the run threads, one statement at a time.
        self._db = sqlite3.connect(self.dir / "sheettriage.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._add_missing_columns()
        self._interrupt_unfinished_jobs()

    def _add_missing_columns(self) -> None:
        """Databases made by an earlier version get the columns added since."""
        have = {row[1] for row in self._db.execute("PRAGMA table_info(jobs)")}
        if "client" not in have:
            self._db.execute("ALTER TABLE jobs ADD COLUMN client TEXT")

    def _query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock, self._db:
            return self._db.execute(sql, params).fetchall()

    def _interrupt_unfinished_jobs(self) -> None:
        """A run can't survive a restart, so any left unfinished is marked as cut short."""
        for row in self._query(f"SELECT id FROM jobs WHERE status IN {UNFINISHED}"):
            self.finish_job(row["id"], {"type": "error", "message": INTERRUPTED_MESSAGE},
                            status="interrupted", error=INTERRUPTED_MESSAGE, finished_at=time.time())

    # ── Users ──

    def upsert_user(self, user_id: str, email: str) -> dict:
        self._query(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET email = excluded.email",
            (user_id, email, time.time()),
        )
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict | None:
        rows = self._query("SELECT * FROM users WHERE id = ?", (user_id,))
        return dict(rows[0]) if rows else None

    def delete_owner(self, owner: str) -> None:
        """Everything an owner has: jobs, their files, uploads, and the account itself."""
        for row in self._query("SELECT id FROM jobs WHERE owner = ?", (owner,)):
            self.delete_job(row["id"])
        for row in self._query("SELECT id FROM uploads WHERE owner = ?", (owner,)):
            self.delete_upload(row["id"])
        self._query("DELETE FROM users WHERE id = ?", (owner,))

    def move_owner(self, old: str, new: str) -> None:
        """A browser's runs from before signing in become the account's."""
        self._query("UPDATE uploads SET owner = ? WHERE owner = ?", (new, old))
        self._query("UPDATE jobs SET owner = ? WHERE owner = ?", (new, old))

    # ── Uploads ──

    def upload_path(self, upload_id: str) -> Path:
        return self.dir / "uploads" / f"{upload_id}.csv"

    def add_upload(self, owner: str, filename: str, contents: bytes, rows: int, columns: list[str],
                   keep_seconds: float, demo: bool = False) -> dict:
        upload_id = str(uuid.uuid4())
        self.upload_path(upload_id).write_bytes(contents)
        now = time.time()
        self._query(
            "INSERT INTO uploads (id, owner, filename, rows, columns, demo, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (upload_id, owner, filename, rows, json.dumps(columns), int(demo), now, now + keep_seconds),
        )
        return self.get_upload(upload_id, owner)

    def get_upload(self, upload_id: str, owner: str) -> dict | None:
        rows = self._query("SELECT * FROM uploads WHERE id = ? AND owner = ?", (upload_id, owner))
        if not rows or not self.upload_path(upload_id).exists():
            return None
        upload = dict(rows[0])
        upload["columns"] = json.loads(upload["columns"])
        upload["demo"] = bool(upload["demo"])
        return upload

    def upload_contents(self, upload_id: str) -> bytes:
        return self.upload_path(upload_id).read_bytes()

    def uploads_of(self, owner: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT id, created_at FROM uploads WHERE owner = ? ORDER BY created_at", (owner,))]

    def count_uploads(self, owner_prefix: str = "") -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT id, created_at FROM uploads WHERE owner LIKE ? ORDER BY created_at", (owner_prefix + "%",))]

    def delete_upload(self, upload_id: str) -> None:
        self.upload_path(upload_id).unlink(missing_ok=True)
        self._query("DELETE FROM uploads WHERE id = ?", (upload_id,))

    # ── Jobs ──

    def job_dir(self, job_id: str) -> Path:
        return self.dir / "jobs" / job_id

    def output_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output_clean.csv"

    def add_job(self, owner: str, upload: dict, mode: str, schema: dict, keep_seconds: float,
                client: str | None = None) -> dict:
        """client is a scrambled form of the visitor's address, used only to count their runs."""
        job_id = str(uuid.uuid4())
        self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        now = time.time()
        self._query(
            "INSERT INTO jobs (id, owner, upload_id, filename, rows, mode, schema, demo, client, status, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (job_id, owner, upload["id"], upload["filename"], upload["rows"], mode, json.dumps(schema),
             int(upload["demo"]), client, now, now + keep_seconds),
        )
        return self.get_job(job_id, owner)

    def get_job(self, job_id: str, owner: str | None) -> dict | None:
        """owner=None is for the server's own use (the run thread), never for a request."""
        if owner is None:
            rows = self._query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        else:
            rows = self._query("SELECT * FROM jobs WHERE id = ? AND owner = ?", (job_id, owner))
        return _job(rows[0]) if rows else None

    def update_job(self, job_id: str, **fields) -> None:
        if fields:
            columns = ", ".join(f"{name} = ?" for name in fields)
            self._query(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))

    def jobs_of(self, owner: str) -> list[dict]:
        return [_job(r) for r in self._query(
            "SELECT * FROM jobs WHERE owner = ? ORDER BY created_at DESC", (owner,))]

    def run_times(self, since: float, owner: str | None = None, client: str | None = None) -> list[float]:
        """When each run that counts toward a limit started, oldest first: the demo file doesn't count.

        By owner for an account, or by client for visitors who haven't signed in."""
        if owner is not None:
            rows = self._query(
                "SELECT created_at FROM jobs WHERE owner = ? AND demo = 0 AND created_at >= ? ORDER BY created_at",
                (owner, since))
        else:
            rows = self._query(
                "SELECT created_at FROM jobs WHERE client = ? AND owner LIKE 'session:%' AND demo = 0 "
                "AND created_at >= ? ORDER BY created_at", (client, since))
        return [r["created_at"] for r in rows]

    def delete_job(self, job_id: str) -> None:
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
        self._query("DELETE FROM job_events WHERE job_id = ?", (job_id,))
        self._query("DELETE FROM jobs WHERE id = ?", (job_id,))

    def save_review(self, job_id: str, review: dict) -> None:
        (self.job_dir(job_id) / "review.json").write_text(json.dumps(review), encoding="utf-8")

    def load_review(self, job_id: str) -> dict | None:
        path = self.job_dir(job_id) / "review.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def finish_job(self, job_id: str, last_event: dict, **fields) -> None:
        """Saves the final status and the run's last event together, so nobody sees one without the other."""
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._lock, self._db:
            self._insert_event(job_id, last_event)
            self._db.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))

    # ── Progress events ──

    def _insert_event(self, job_id: str, event: dict) -> None:
        seq = self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE job_id = ?", (job_id,)).fetchone()[0]
        self._db.execute("INSERT INTO job_events (job_id, seq, event) VALUES (?, ?, ?)",
                         (job_id, seq, json.dumps(event)))

    def add_event(self, job_id: str, event: dict) -> None:
        with self._lock, self._db:
            self._insert_event(job_id, event)

    def events(self, job_id: str, after: int = 0) -> list[tuple[int, dict]]:
        rows = self._query(
            "SELECT seq, event FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq", (job_id, after))
        return [(r["seq"], json.loads(r["event"])) for r in rows]

    # ── Expiry ──

    def delete_expired(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for row in self._query(
                f"SELECT id FROM jobs WHERE expires_at < ? AND status NOT IN {UNFINISHED}", (now,)):
            self.delete_job(row["id"])
        for row in self._query("SELECT id FROM uploads WHERE expires_at < ?", (now,)):
            self.delete_upload(row["id"])


def _job(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["schema"] = json.loads(job["schema"])
    job["demo"] = bool(job["demo"])
    return job
