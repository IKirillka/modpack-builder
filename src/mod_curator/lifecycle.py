"""Job trash, guarded deletion and daily retention for the local state store."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, field_validator

DAY = 86400
PREVIEW_TTL = 1800
CleanupStatus = Literal["completed", "exported", "cancelled", "error", "failed", "blocked"]
DEFAULT_STATUSES = ("completed", "exported", "cancelled", "error", "failed", "blocked")
REFERENCES = (
    ("reuse_descriptions_from",),
    ("extension", "source_job_id"),
    ("exported_from", "job_id"),
    ("check_options", "job_id"),
    ("check_options", "update_apply", "proposal_task"),
    ("candidate_review", "job_id"),
    ("candidate_review", "proposal_id"),
    ("update_proposals", "source_job_id"),
    ("update_proposals", "baseline_plan_task"),
    ("copy_from_job",),
)
REPORT_REFERENCES = (("export_plan", "task_id"), ("pack_export", "installation_plan_task"))


class CleanupPolicy(BaseModel):
    enabled: bool = False
    days: int = Field(default=30, ge=1, le=3650)
    statuses: list[CleanupStatus] = Field(default_factory=lambda: list(DEFAULT_STATUSES), min_length=1)
    purge_after_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("statuses")
    @classmethod
    def unique(cls, value):
        return sorted(set(value))


class CleanupPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    days: int | None = Field(default=None, ge=1, le=3650)
    statuses: list[CleanupStatus] | None = Field(default=None, min_length=1)
    purge_after_days: int | None = Field(default=None, ge=1, le=3650)


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS job_usage (
        job_id TEXT PRIMARY KEY, opened_at REAL, pinned INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS job_trash (
        id TEXT PRIMARY KEY, data TEXT NOT NULL, trashed_at REAL NOT NULL,
        reason TEXT NOT NULL, purging INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS cleanup_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL, last_auto_run REAL
    );
    CREATE TABLE IF NOT EXISTS lifecycle_previews (
        scope TEXT PRIMARY KEY, token_hash TEXT NOT NULL, expires_at REAL NOT NULL,
        fingerprint TEXT NOT NULL, stopping INTEGER NOT NULL DEFAULT 0
    );
    INSERT OR IGNORE INTO curator_migrations VALUES(4,strftime('%s','now'));
    """)


def checked_id(job_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise ValueError("Invalid job ID")
    return job_id


def field(data, *path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def references(data, *, include_reports=False):
    for path in REFERENCES + (REPORT_REFERENCES if include_reports else ()):
        if isinstance(target := field(data, *path), str) and target:
            yield target, ".".join(path)


def stopping(db, job_id, now=None):
    return bool(db.execute(
        "SELECT 1 FROM lifecycle_previews WHERE scope=? AND stopping=1 AND expires_at>?",
        (f"trash:{job_id}", time.time() if now is None else now),
    ).fetchone())


def require_available(db, job_id):
    if stopping(db, job_id):
        raise ValueError("Job is stopping for confirmed deletion")
    if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
        raise ValueError("Job is unavailable. Restore it from trash if present there.")


def record_open(db, job_id, now, *, pinned=None):
    if pinned is None:
        db.execute("""INSERT INTO job_usage(job_id,opened_at) VALUES(?,?)
                      ON CONFLICT(job_id) DO UPDATE SET opened_at=excluded.opened_at""", (job_id, now))
    else:
        db.execute("""INSERT INTO job_usage(job_id,opened_at,pinned) VALUES(?,?,?)
                      ON CONFLICT(job_id) DO UPDATE SET pinned=excluded.pinned,opened_at=excluded.opened_at""",
                   (job_id, now, int(pinned)))


def title(job):
    text = (job.get("title") or job.get("name") or field(job, "export_options", "name")
            or field(job, "brief", "concept") or field(job, "filters", "wishes"))
    return text.strip().splitlines()[0][:150] if isinstance(text, str) and text.strip() else "Untitled job"


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def lock_busy(path):
    if path.is_symlink() or path.parent.is_symlink():
        return True
    if not path.is_file():
        return False
    try:
        with FileLock(path).acquire(timeout=0):
            return False
    except Timeout:
        return True


class JobLifecycle:
    def __init__(self, store):
        self.store = store
        self.lock = FileLock(store.root / "job-lifecycle.lock")

    @staticmethod
    def _policy(db):
        row = db.execute("SELECT policy,last_auto_run FROM cleanup_settings WHERE id=1").fetchone()
        return (CleanupPolicy.model_validate_json(row[0]), row[1]) if row else (CleanupPolicy(), None)

    def settings(self):
        with self.store.connect() as db:
            policy, last = self._policy(db)
        return policy.model_dump() | {"last_auto_run": last, "recommended_purge_days": 7,
                                     "allowed_statuses": list(DEFAULT_STATUSES)}

    @staticmethod
    def _patched(policy, patch):
        changes = patch.model_dump(exclude_unset=True)
        if any(value is None and key != "purge_after_days" for key, value in changes.items()):
            raise ValueError("Only purge_after_days can be cleared")
        return CleanupPolicy.model_validate(policy.model_dump() | changes)

    @staticmethod
    def _ticket(db, scope, fingerprint, now):
        token = secrets.token_urlsafe(24)
        db.execute("""INSERT INTO lifecycle_previews(scope,token_hash,expires_at,fingerprint)
                      VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE SET
                      token_hash=excluded.token_hash,expires_at=excluded.expires_at,
                      fingerprint=excluded.fingerprint,stopping=0""",
                   (scope, hash_value(token), now + PREVIEW_TTL, fingerprint))
        return token

    @staticmethod
    def _confirm(db, scope, token, fingerprint, now):
        row = db.execute("SELECT token_hash,fingerprint FROM lifecycle_previews WHERE scope=? AND expires_at>?",
                         (scope, now)).fetchone()
        if not row or not hmac.compare_digest(row[0], hash_value(token)) or row[1] != fingerprint:
            raise ValueError("Preview expired, changed, or belongs to a different ID. Preview again.")

    @staticmethod
    def _job(db, job_id, *, trashed=False):
        if trashed:
            row = db.execute("SELECT data FROM job_trash WHERE id=?", (job_id,)).fetchone()
        else:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Unknown job in the requested location")
        return row[0], json.loads(row[0])

    def _inbound(self, db):
        incoming = {}
        for owner, raw, location in db.execute("""SELECT id,data,'active' FROM jobs UNION ALL
                                                 SELECT id,data,'trash' FROM job_trash"""):
            for target, relation in references(json.loads(raw), include_reports=True):
                if owner != target:
                    incoming.setdefault(target, []).append({"id": owner, "location": location, "relation": relation})
        for draft_id, raw in db.execute("SELECT id,data FROM intake_drafts"):
            draft = json.loads(raw)
            if not draft.get("job_id") and (target := draft.get("copy_from_job")):
                incoming.setdefault(target, []).append({"id": draft_id, "location": "draft", "relation": "copy_from_job"})
        return incoming

    def _busy(self, db, job_id, now):
        checked_id(job_id)
        worker = lock_busy(self.store.root / "jobs" / job_id / "worker.lock")
        extraction = lock_busy(self.store.root / "jobs" / job_id / "extraction.lock")
        workers = [rid for rid, in db.execute("SELECT id FROM economy_runs WHERE job_id=? AND state='active'", (job_id,))]
        running = [rid for rid in workers if lock_busy(self.store.root / f"economy-{checked_id(rid)}.lock")]
        leases = db.execute("SELECT COUNT(*) FROM fact_batches WHERE job_id=? AND state='leased' AND lease_until>?",
                            (job_id, now)).fetchone()[0]
        return {"worker": worker, "extraction_worker": extraction, "analysis_runs": running, "leased_batches": leases}, workers

    def _impact(self, db, job_id, job):
        plan_revisions = dict(db.execute("SELECT revision,COUNT(*) FROM installation_plans WHERE job_id=? GROUP BY revision", (job_id,)))
        revisions = {job.get("selection_revision", 0)}
        revisions.update(row["revision"] for row in job.get("history", []) if "revision" in row)
        for row in job.get("pack_revisions", []):
            revisions.update(rev for key in ("before", "after")
                             if (rev := field(row, key, "revision")) is not None)
        directory = self.store.root / "jobs" / job_id
        reports = sorted(p.name for p in directory.iterdir() if p.suffix in (".json", ".csv", ".log")) if (
            directory.is_dir() and not directory.is_symlink() and not directory.parent.is_symlink()
        ) else []
        return {"revisions": sorted(revisions), "installation_plan_counts_by_revision": plan_revisions,
                "reports_directory": str(directory), "reports": reports[:30], "report_count": len(reports),
                "job_data": ["catalogue pages", "project descriptions and selections in this job", "revisions",
                             "installation plans", "reports and logs", "job-specific analysis queue and runs"],
                "preserved": ["shared fact and description caches", "settings and other jobs", "exported ZIPs and Prism instances"],
                "archive": field(job, "pack_export", "path")}

    def preview_delete(self, job_id, *, permanent=False):
        checked_id(job_id)
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, job = self._job(db, job_id, trashed=permanent)
            deps = self._inbound(db).get(job_id, [])
            blockers = [item for item in deps if permanent or item["location"] != "trash"]
            work, _ = self._busy(db, job_id, now)
            mode = "purge" if permanent else "trash"
            result = {"job_id": job_id, "title": title(job), "status": job["status"], "stage": job["stage"],
                      "action": mode, "blockers": blockers, "references": deps, "active_work": work,
                      "impact": self._impact(db, job_id, job)}
            if permanent:
                try:
                    self._purge_path(job_id, job)
                except ValueError as exc:
                    blockers.append({"id": job_id, "location": "filesystem", "reason": str(exc)})
            if not blockers:
                result["preview_token"] = self._ticket(db, f"{mode}:{job_id}", self._delete_key(job, permanent), now)
        return result

    @staticmethod
    def _delete_key(job, permanent):
        # Status and timings can change while a cooperative stop finishes.
        keys = ("id", "selection_revision", "requirements_revision", "selection", "filters", "brief")
        return hash_value([permanent, {key: job.get(key) for key in keys}])

    def _move(self, db, job_id, raw, now, reason):
        db.execute("INSERT INTO job_trash(id,data,trashed_at,reason) VALUES(?,?,?,?)", (job_id, raw, now, reason))
        db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        db.execute("DELETE FROM lifecycle_previews WHERE scope=?", (f"trash:{job_id}",))

    async def delete(self, job_id, confirm_id, token, *, permanent=False, wait_seconds=8):
        checked_id(job_id)
        if confirm_id != job_id:
            raise ValueError("Explicit confirmation must equal the exact job ID")
        scope = f"{'purge' if permanent else 'trash'}:{job_id}"
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                handle = self.lock.acquire(timeout=0)
            except Timeout:
                return {"job_id": job_id, "status": "lifecycle_busy",
                        "poll_after_seconds": 5, "repeat_same_confirmation": True}
            with handle:
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    now = time.time()
                    raw, job = self._job(db, job_id, trashed=permanent)
                    self._confirm(db, scope, token, self._delete_key(job, permanent), now)
                    refs = [item for item in self._inbound(db).get(job_id, [])
                            if permanent or item["location"] != "trash"]
                    if refs:
                        return {"job_id": job_id, "status": "blocked_by_references", "blockers": refs}
                    if permanent:
                        self._begin_purge(db, job_id, job)
                    else:
                        work, runs = self._busy(db, job_id, now)
                        if any(work.values()):
                            self._stop_for_delete(db, job_id, job, scope, runs)
                        else:
                            if job["status"] in ("queued", "running"):
                                job.update(status="cancelled", cancel_requested=True, reason="cancelled")
                                raw = json.dumps(job, ensure_ascii=False)
                            db.execute("UPDATE economy_runs SET state='paused',stop_requested=1 WHERE job_id=? AND state='active'",
                                       (job_id,))
                            self._move(db, job_id, raw, now, "manual")
                            return {"job_id": job_id, "status": "in_trash"}
                if permanent:
                    self._finish_purge(job_id)
                    return {"job_id": job_id, "status": "permanently_deleted"}
            if time.monotonic() >= deadline:
                return {"job_id": job_id, "status": "waiting_for_stop", "active_work": work,
                        "poll_after_seconds": 5, "repeat_same_confirmation": True}
            await asyncio.sleep(0.25)

    @staticmethod
    def _stop_for_delete(db, job_id, job, scope, runs):
        if job["status"] in ("queued", "running") and not job.get("cancel_requested"):
            job.update(cancel_requested=True, updated_at=time.time())
            db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))
        db.executemany("UPDATE economy_runs SET stop_requested=1 WHERE id=?", ((rid,) for rid in runs))
        db.execute("UPDATE lifecycle_previews SET stopping=1 WHERE scope=?", (scope,))

    def pin(self, job_id, pinned=True):
        checked_id(job_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._job(db, job_id)
            record_open(db, job_id, time.time(), pinned=pinned)
        return {"job_id": job_id, "pinned": pinned}

    def trash(self, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid pagination")
        with self.store.connect() as db:
            rows = db.execute("""SELECT id,data,trashed_at,reason,purging FROM job_trash
                                ORDER BY trashed_at DESC,id LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
            total = db.execute("SELECT COUNT(*) FROM job_trash").fetchone()[0]
        return {"total": total, "offset": offset, "items": [
            {"job_id": job_id, "title": title(job := json.loads(raw)), "status": job["status"],
             "trashed_at": at, "trashed_at_utc": datetime.fromtimestamp(at, timezone.utc).isoformat(),
             "reason": reason, "purging": bool(purging)}
            for job_id, raw, at, reason, purging in rows]}

    def restore(self, job_id):
        checked_id(job_id)
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            raw, job = self._job(db, job_id, trashed=True)
            flag = db.execute("SELECT purging FROM job_trash WHERE id=?", (job_id,)).fetchone()[0]
            if flag:
                raise ValueError("Permanent deletion has already started")
            missing = [{"id": target, "relation": relation} for target, relation in references(job)
                       if target != job_id and db.execute("SELECT 1 FROM job_trash WHERE id=?", (target,)).fetchone()]
            if missing:
                return {"job_id": job_id, "status": "restore_dependencies_first", "blockers": missing}
            db.execute("INSERT INTO jobs VALUES(?,?)", (job_id, raw))
            db.execute("DELETE FROM job_trash WHERE id=?", (job_id,))
            db.execute("DELETE FROM lifecycle_previews WHERE scope=?", (f"purge:{job_id}",))
            record_open(db, job_id, time.time())
        return {"job_id": job_id, "status": "restored", "job_status": job["status"]}

    def _purge_path(self, job_id, job=None):
        directory = self.store.root / "jobs" / checked_id(job_id)
        if directory.parent.is_symlink() or directory.is_symlink():
            raise ValueError("Refusing to remove a linked job directory")
        if directory.exists() and (not directory.is_dir() or not shutil.rmtree.avoids_symlink_attacks):
            raise ValueError("Safe recursive removal of this job directory is unavailable")
        current = job or {}
        protected = [field(current, "pack_export", "path")]
        protected.extend(self.store.config().get("destinations", {}).values())
        if any(Path(value).resolve().is_relative_to(directory) for value in protected if isinstance(value, str)):
            raise ValueError("Job directory contains an export or a registered destination")
        if directory.exists() and any(p.suffix.lower() == ".zip" or p.name in ("instance.cfg", "mmc-pack.json")
                                      for p in directory.rglob("*")):
            raise ValueError("Job directory contains a ZIP or Prism instance. Permanent deletion blocked.")
        return directory

    def _begin_purge(self, db, job_id, job):
        self._purge_path(job_id, job)
        db.execute("UPDATE job_trash SET purging=1 WHERE id=?", (job_id,))

    def _finish_purge(self, job_id):
        directory = self._purge_path(job_id)
        if directory.exists():
            shutil.rmtree(directory)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("economy_claims", "economy_projects"):
                db.execute(f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM economy_runs WHERE job_id=?)", (job_id,))
            for table in ("economy_runs", "fact_queue", "fact_batches", "fact_selections", "installation_plans", "job_usage"):
                db.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM intake_drafts WHERE json_extract(data,'$.job_id')=?", (job_id,))
            db.execute("DELETE FROM job_trash WHERE id=?", (job_id,))
            db.execute("DELETE FROM lifecycle_previews WHERE scope=?", (f"purge:{job_id}",))

    def _eligible(self, db, job_id, job, opened, pinned, policy, now, incoming):
        last = max(job.get("updated_at") or 0, job.get("created_at") or 0, opened or 0)
        if job["status"] not in policy.statuses or not last or last > now - policy.days * DAY:
            return None, None
        refs = incoming.get(job_id, [])
        if pinned or refs:
            return None, {"job_id": job_id, "reason": "pinned" if pinned else "referenced", "references": refs}
        if stopping(db, job_id, now):
            return None, {"job_id": job_id, "reason": "manual_deletion_pending"}
        work, runs = self._busy(db, job_id, now)
        paused = db.execute("SELECT 1 FROM economy_runs WHERE job_id=? AND state='paused'", (job_id,)).fetchone()
        if any(work.values()) or runs or paused:
            return None, {"job_id": job_id, "reason": "analysis_or_worker_active_or_paused"}
        return {"job_id": job_id, "title": title(job), "status": job["status"], "last_used_at": last}, None

    @staticmethod
    def _jobs(db):
        return db.execute("""SELECT j.id,j.data,u.opened_at,u.pinned FROM jobs j
                           LEFT JOIN job_usage u ON j.id=u.job_id ORDER BY j.rowid""").fetchall()

    def preview_cleanup(self, patch=None):
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE" if patch is not None else "BEGIN")
            old, _ = self._policy(db)
            policy = self._patched(old, patch) if patch is not None else old
            incoming = self._inbound(db)
            candidates, skipped = [], []
            for jid, raw, opened, pinned in self._jobs(db):
                candidate, reason = self._eligible(db, jid, json.loads(raw), opened, pinned, policy, now, incoming)
                if candidate:
                    candidates.append(candidate)
                if reason:
                    skipped.append(reason)
            result = {"policy": policy.model_dump(), "candidates": candidates, "skipped": skipped,
                      "purge_candidates": [jid for jid in self._purge_candidates(db, policy, now) if not incoming.get(jid)]}
            if patch is not None:
                result["preview_token"] = self._ticket(db, "policy", hash_value([old.model_dump(), policy.model_dump()]), now)
        return result

    def configure(self, patch, token):
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old, last = self._policy(db)
            new = self._patched(old, patch)
            self._confirm(db, "policy", token, hash_value([old.model_dump(), new.model_dump()]), time.time())
            db.execute("INSERT OR REPLACE INTO cleanup_settings VALUES(1,?,?)", (new.model_dump_json(), last))
            db.execute("DELETE FROM lifecycle_previews WHERE scope='policy'")
        applied = self.sweep(automatic=True) if new.enabled and not old.enabled else None
        return self.settings() | {"initial_run": applied}

    @staticmethod
    def _purge_candidates(db, policy, now):
        if policy.purge_after_days is None:
            return []
        return [jid for jid, in db.execute(
            "SELECT id FROM job_trash WHERE trashed_at<=? ORDER BY trashed_at,id",
            (now - policy.purge_after_days * DAY,))]

    def sweep(self, *, automatic=False):
        try:
            with self.lock.acquire(timeout=0):
                return self._sweep(automatic)
        except Timeout:
            return {"status": "already_running", "moved": [], "purged": []}

    def _sweep(self, automatic):
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            policy, last = self._policy(db)
            if automatic and (not policy.enabled or last is not None and now - last < DAY):
                return {"status": "disabled" if not policy.enabled else "not_due", "moved": [], "purged": []}
            if automatic:
                db.execute("UPDATE cleanup_settings SET last_auto_run=? WHERE id=1", (now,))
            incoming = self._inbound(db)
            moved, skipped = [], []
            for jid, raw, opened, pinned in self._jobs(db):
                candidate, reason = self._eligible(db, jid, json.loads(raw), opened, pinned, policy, now, incoming)
                if candidate:
                    checked_id(jid)
                    self._move(db, jid, raw, now, f"inactive_for_{policy.days}_days")
                    moved.append(candidate)
                if reason:
                    skipped.append(reason)
            purge_ids = self._purge_candidates(db, policy, now)
        purged = []
        for jid in purge_ids:
            try:
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    refs = self._inbound(db).get(jid, [])
                    if refs:
                        skipped.append({"job_id": jid, "reason": "referenced", "references": refs})
                        continue
                    _, job = self._job(db, jid, trashed=True)
                    self._begin_purge(db, jid, job)
                self._finish_purge(jid)
                purged.append(jid)
            except (OSError, ValueError) as exc:
                skipped.append({"job_id": jid, "reason": str(exc)})
        return {"status": "completed", "moved": moved, "purged": purged, "skipped": skipped}


def run_on_startup(store):
    try:
        JobLifecycle(store).sweep(automatic=True)
    except (OSError, ValueError, sqlite3.Error) as exc:
        logging.getLogger(__name__).warning("Job cleanup could not finish: %s", exc)
