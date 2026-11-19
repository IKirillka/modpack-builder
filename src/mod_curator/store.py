"""SQLite transactions are the boundary between a chat and detached workers."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .lifecycle import checked_id, record_open, references, require_available


def state_home() -> Path:
    return Path(os.environ.get("MOD_CURATOR_HOME", Path.home() / ".local/share/mod-curator")).expanduser().resolve()


class Store:
    def __init__(self, root: Path | None = None):
        self.root = (root or state_home()).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / "state.sqlite3"
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            from .facts import migrate
            migrate(c)
            from .economy import migrate as migrate_economy
            migrate_economy(c)
            c.execute("CREATE TABLE IF NOT EXISTS intake_drafts (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS installation_plans (
                      job_id TEXT NOT NULL, revision INTEGER NOT NULL, curseforge_only INTEGER NOT NULL,
                      ready INTEGER NOT NULL, data TEXT NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS plans_by_revision ON installation_plans (job_id,revision,curseforge_only)")
            from .lifecycle import migrate as migrate_lifecycle
            migrate_lifecycle(c)

    def connect(self):
        c = sqlite3.connect(self.db, timeout=30)
        return c

    def get(self, job_id, *, opened=False):
        with self.connect() as c:
            if opened:
                c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                in_trash = c.execute("SELECT 1 FROM job_trash WHERE id=?", (job_id,)).fetchone()
                raise ValueError("Job is in trash. Restore it first." if in_trash else "Unknown job")
            if opened:
                record_open(c, job_id, time.time())
        return json.loads(row[0])

    def mutate(self, job_id, fn, *, after=None):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Unknown job")
            data = json.loads(row[0])
            previous_refs = set(references(data))
            result = fn(data)
            for target, _ in set(references(data)) - previous_refs:
                require_available(c, target)
            data["updated_at"] = time.time()
            c.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), job_id))
            if after:
                after(c, result)
        return result

    def patch(self, job_id, **changes):
        self.mutate(job_id, lambda d: d.update(changes))

    def create(self, filters, *, connection=None, **initial):
        job_id = uuid.uuid4().hex
        data = dict(id=job_id, filters=filters, status="queued", stage="collect", reason=None,
                    next_page=1, pages=[], projects={}, order=[], processed=0,
                    errors=[], warnings=[], early_stop=True, completeness="incomplete",
                    decisions={}, requirements_revision=1, selection=[], selection_revision=0,
                    history=[], confirmed=None, overlap_review=None, cancel_requested=False,
                    timings={}, created_at=time.time())
        data.update(initial, updated_at=time.time())
        def insert(db):
            for target, _ in references(data):
                require_available(db, target)
            db.execute("INSERT INTO jobs VALUES (?,?)", (job_id, json.dumps(data, ensure_ascii=False)))
        if connection is not None:
            insert(connection)
        else:
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                insert(c)
        return job_id

    def jobs(self):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT data FROM jobs ORDER BY rowid DESC")]

    def job_list(self):
        with self.connect() as c:
            rows = c.execute("""SELECT id, json_extract(data,'$.status'),json_extract(data,'$.stage'),
                                json_extract(data,'$.created_at') FROM jobs ORDER BY rowid DESC""")
            return [dict(id=job_id, status=status, stage=stage, created_at=created)
                    for job_id, status, stage, created in rows]

    def job_dir(self, job_id, *, known_exists=False):
        checked_id(job_id)
        if not known_exists:
            with self.connect() as c:
                if c.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
                    raise ValueError("Unknown job")
        p = self.root / "jobs" / job_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def config(self):
        p = self.root / "config.json"
        return json.loads(p.read_text()) if p.exists() else {"destinations": {}, "transport": "http"}

    def destination(self, alias):
        value = self.config()["destinations"].get(alias)
        if not value:
            raise ValueError("Destination must be registered locally with `mod-curator init`")
        p = Path(value)
        if not p.is_dir() or p.is_symlink() or str(p.resolve()) != value:
            raise ValueError("Destination moved, vanished, or became a symlink; register it again")
        return p
