from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time

import httpx
from filelock import FileLock, Timeout

from .downloads import download_one
from .lifecycle import checked_id, lock_busy, require_available
from .models import Cancelled, Decision, Filters, SourceError
from .resolver import resolve
from .source import CurseForge
from .store import Store
from .platform_support import background_options


def fingerprint(ids):
    return hashlib.sha256(json.dumps(sorted(set(ids))).encode()).hexdigest()


class Engine:
    def __init__(self, store=None):
        self.store = store or Store()

    def launch(self, job_id):
        path = self.store.job_dir(job_id)
        with open(path / "worker.log", "ab") as log:
            p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "mod_curator", "worker", job_id],
                                 env={**os.environ, "MOD_CURATOR_HOME": str(self.store.root)},
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, **background_options())
        return {"job_id": job_id, "worker_pid": p.pid, "progress_tool": "job_status"}

    def start(self, params):
        f = Filters.model_validate(params)
        self.store.destination(f.destination)
        job_id = self.store.create(f.model_dump())
        return self.launch(job_id)

    def cancel(self, job_id):
        self.store.patch(job_id, cancel_requested=True)
        return {"job_id": job_id, "cancel_requested": True}

    def resume(self, job_id, *, max_states=None):
        d = self.store.get(job_id)
        if self.worker_active(job_id, known_exists=True):
            return {"job_id": job_id, "status": d["status"], "worker_active": True}
        if d["stage"] in ("awaiting_analysis", "done"):
            return {"job_id": job_id, "status": d["status"], "next": "analysis_batch or revise_selection"}
        if max_states is not None and (d['stage'] != 'pack_check' or not 1 <= max_states <= 2_000_000):
            raise ValueError("A search budget of 1..2000000 can be set on a resumable preflight")
        def resume_state(state):
            state.update(cancel_requested=False, status="queued", reason=None, recheck=True)
            if max_states is not None:
                state['check_options']['max_states'] = max_states
        self.store.mutate(job_id, resume_state, after=lambda db, _: require_available(db, job_id))
        return self.launch(job_id)

    def check_cancel(self, job_id):
        if self.store.get(job_id)["cancel_requested"]:
            raise Cancelled("User requested cancellation")

    def status(self, job_id, *, data=None, details=True, busy=None):
        d = self.store.get(job_id) if data is None else data
        busy = self.worker_active(job_id, known_exists=True) if busy is None else busy
        missing_count = described = analyzed_with_description = 0
        missing = []
        for key, project in d["projects"].items():
            described += "description" in project
            if project.get("description"):
                analyzed_with_description += key in d["decisions"]
            else:
                missing_count += 1
                if details or len(missing) < 3:
                    missing.append({"project_id": project["id"], "url": project.get("url"),
                                    "message": project.get("description_error", "Description not received")})
        available = len(d["projects"]) - missing_count
        return {k: v for k, v in d.items() if k in ("id", "status", "stage", "activity", "reason", "next_page", "processed", "completeness", "warnings", "errors", "timings", "selection_revision", "requirements_revision", "cancel_requested")} | {
            "collected": len(d["order"]), "described": described,
            "analyzed": len(d["decisions"]), "worker_active": busy,
            "catalogue": d.get("catalogue", {"finished": bool(d.get("collection_reason")), "reason": d.get("collection_reason"), "completeness": "uncertain_catalog_scanned" if not d["early_stop"] else d["completeness"]}),
            "descriptions_available": available, "unresolved_descriptions": missing_count,
            "unresolved_errors": missing, "description_completeness": "partial" if missing_count else "complete",
            "analysis_complete_for_available": analyzed_with_description == available,
            "analysis_complete_for_entire_set": d["projects"].keys() <= d["decisions"].keys(),
            "files_total": len(d.get("lock", {}).get("files", {})),
            "files_finished": sum(r.get("status") in ("downloaded", "already_verified") for r in d.get("downloads", {}).values()),
            "search_scope_results": d.get("search_scope_results", {}),
            "recoverable_interruption": d["status"] in ("running", "queued") and not busy}

    def worker_active(self, job_id, *, known_exists=False):
        if not known_exists:
            self.store.get(job_id)
        return lock_busy(self.store.root / "jobs" / checked_id(job_id) / "worker.lock")

    def idle(self, d):
        if d["status"] in ("running", "queued"):
            raise ValueError("Task is active; cancel and wait for worker to stop before editing")

    async def run(self, job_id, source=None):
        lock = FileLock(self.store.job_dir(job_id) / "worker.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                require_available(db, job_id)
        except ValueError:
            lock.release()
            return
        source = source or CurseForge(self.store.config(), self.store.job_dir(job_id))
        try:
            job = self.store.get(job_id)
            if job.get('extension') and job['stage'] == 'collect':
                from .refinement import ExtensionSource
                source = ExtensionSource(self.store, job, source)
            self.store.patch(job_id, status="running")
            while True:
                self.check_cancel(job_id)
                d = self.store.get(job_id)
                stage = d["stage"]
                if stage not in ("collect", "describe", "install", "export", "pack_check"):
                    return
                begin = time.monotonic()
                try:
                    if stage == "collect":
                        if d.get('search_scopes') and isinstance(source, CurseForge):
                            capture_dir = self.store.job_dir(job_id) / f"search-{d['search_scope_index'] + 1}"
                            capture_dir.mkdir(exist_ok=True)
                            source.evidence_dir = capture_dir
                        await self.collect(job_id, source)
                    elif stage == "describe":
                        if d.get('search_scopes') and isinstance(source, CurseForge):
                            source.evidence_dir = self.store.job_dir(job_id)
                        await self.describe(job_id, source)
                    elif stage == "export":
                        await self.export_prism(job_id, source)
                        return
                    elif stage == "pack_check":
                        from .preflight import run as check_pack
                        await check_pack(self, job_id, source)
                        return
                    else:
                        await self.install(job_id, source)
                        return
                finally:
                    elapsed = time.monotonic() - begin
                    self.store.mutate(job_id, lambda s: s["timings"].__setitem__(stage, s["timings"].get(stage, 0) + elapsed))
        except Cancelled:
            self.store.patch(job_id, status="cancelled", reason="cancelled", completeness="incomplete")
        except Exception as e:
            def fail(s):
                exporting = s["stage"] == "export"
                s.update(status="draft" if exporting else "error", reason="error")
                if not exporting:
                    s["completeness"] = "incomplete"
                s["errors"].append({"stage": s["stage"], "page": s["next_page"], "message": str(e), "at": time.time()})
                if exporting:
                    s["pack_export"] = {"ready": False, "status": "draft", "unresolved": str(e), "no_importable_archive": True}
            self.store.mutate(job_id, fail)
        finally:
            try:
                await source.close()
            finally:
                try:
                    self.export(job_id)
                finally:
                    lock.release()

    async def collect(self, job_id, source):
        d = self.store.get(job_id)
        scopes = d.get('search_scopes') or []
        scope_index = d.get('search_scope_index', 0)
        effective = scopes[scope_index]['filters'] if scopes else {}
        f = Filters.model_validate({**d["filters"], **effective})

        def active_pages(state):
            if not scopes:
                return state["pages"]
            return [page for page in state['pages'] if page.get('search_scope_index') == scope_index]

        pages = active_pages(d)
        replay = None
        if d.get("recheck") and pages:
            last = pages[-1]
            page = await source.page(f, last["number"])
            ids = [c["id"] for c in page.cards]
            values = [c.get("downloads") for c in page.cards]
            if ids != last["ids"] or page.total != last["total"]:
                def restart(s):
                    s.setdefault("catalogue_restarts", []).append({
                        "previous_pages": len(s["pages"]), "previous_cards": s["processed"],
                        "at_page": last["number"], "at": time.time(),
                    })
                    if scopes:
                        s['pages'] = [p for p in s['pages'] if p.get('search_scope_index') != scope_index]
                        s['processed'] = sum(len(p['ids']) for p in s['pages'])
                        s['catalogue_scopes_uncertain'] = True
                    else:
                        s.update(pages=[], processed=0)
                    s.update(next_page=1, early_stop=False, recheck=False, catalogue_uncertain=True)
                    s["warnings"].append("CATALOG_CHANGED_ON_RESUME: replayed from page 1; saved projects kept")
                    return s
                d = self.store.mutate(job_id, restart)
                if last["number"] == 1:
                    replay = page
            elif values != last.get("downloads"):
                def rewind(s):
                    s["pages"].pop()
                    s.update(next_page=last["number"], processed=s["processed"] - len(ids), recheck=False)
                    return s
                d = self.store.mutate(job_id, rewind)
                replay = page
        while True:
            self.check_cancel(job_id)
            pages = active_pages(d)
            number = d["next_page"]
            page = replay if replay is not None else await source.page(f, number)
            replay = None
            values = [c.get("downloads") for c in page.cards]
            if any(type(v) is not int or v < 0 for v in values):
                raise SourceError("UNKNOWN_DOWNLOADS: current page not committed")
            ids = [c["id"] for c in page.cards]
            if page.has_next and not ids:
                raise SourceError("INCOMPLETE_PAGE: empty page before end of catalogue")
            if any(ids == p["ids"] and ids for p in pages):
                raise SourceError("REPEATED_PAGE: pagination did not advance; incomplete")
            history_ids = {i for p in pages for i in p["ids"]}
            warnings = []
            if len(ids) != len(set(ids)) or set(ids) & history_ids:
                warnings.append("DUPLICATES_OR_PAGE_SHIFT: completeness uncertain; early stop disabled")
            if pages and page.total != pages[-1]["total"]:
                warnings.append("CATALOG_TOTAL_CHANGED: completeness uncertain; early stop disabled")
            neighbors = [{"id": pid, "downloads": n, "page": number, "position": pos}
                         for pos, (pid, n) in enumerate(zip(ids, values), 1)]
            if pages and pages[-1]["ids"]:
                prev = pages[-1]
                neighbors.insert(0, {"id": prev["ids"][-1], "downloads": prev["last_downloads"],
                                     "page": prev["number"], "position": len(prev["ids"])})
            violations = [{"before": a, "after": b} for a, b in zip(neighbors, neighbors[1:])
                          if a["downloads"] < b["downloads"]]
            if violations:
                warnings.append("SORT_ORDER_VIOLATION: scanning whole catalogue; completeness uncertain")
            early = (d["early_stop"] and page.order_guaranteed and not warnings
                     and all(p.get("order_guaranteed") for p in pages))
            boundary = next((i for i, n in enumerate(values) if n <= f.minimum_downloads), None)
            if early and boundary is not None:
                accepted = page.cards[:boundary]
            else:
                accepted = [c for c in page.cards if c["downloads"] > f.minimum_downloads]
            accepted_ids = [c["id"] for c in accepted]
            accepted_set = set(accepted_ids)

            def commit(s):
                if s["cancel_requested"]:
                    raise Cancelled("User requested cancellation")
                for warning in warnings:
                    if warning not in s["warnings"]:
                        s["warnings"].append(warning)
                s["catalogue_uncertain"] = (s.get("catalogue_uncertain", not scopes and bool(d["warnings"] and not d["early_stop"]))
                                             or bool(warnings))
                if violations:
                    if scopes:
                        for pair in violations:
                            pair['search_scope_index'] = scope_index
                    s.setdefault("sort_violations", []).extend(violations)
                seen = set(s["order"])
                for card in accepted:
                    key = str(card["id"])
                    if key not in s["projects"]:
                        s["projects"][key] = card
                    if scopes:
                        sightings = s["projects"][key].setdefault('search_matches', {})
                        sightings.setdefault(scopes[scope_index]['label'], {
                            'downloads': card['downloads'], 'page': number, 'position': card['position']})
                    if card["id"] not in seen:
                        s["order"].append(card["id"])
                        seen.add(card["id"])
                s["pages"].append({"number": number, "ids": ids, "total": page.total, "url": page.url,
                                   "last_downloads": values[-1] if values else 0, "downloads": values,
                                   "minimum_downloads": f.minimum_downloads,
                                   "boundary_cards": [{k: c[k] for k in ("id", "name", "slug", "url", "downloads") if k in c}
                                                      for c in page.cards if c["id"] not in accepted_set],
                                   "evidence": page.evidence, "accepted_ids": accepted_ids,
                                   "order_guaranteed": page.order_guaranteed,
                                   "threshold_comparisons": boundary + 1 if early and boundary is not None else len(values)})
                if scopes:
                    s['pages'][-1]['search_scope_index'] = scope_index
                    s['activity'] = {'operation': 'collect', 'search_scope': scopes[scope_index]['label'],
                                     'scope_index': scope_index, 'scope_count': len(scopes), 'page': number}
                s.update(next_page=number + 1, early_stop=early, processed=s["processed"] + len(page.cards),
                         recheck=False)
                if early and boundary is not None or not page.has_next:
                    reason = "threshold" if early and boundary is not None else "catalog_end"
                    if reason == "threshold":
                        coverage = "observed_sorted_prefix"
                    elif s["catalogue_uncertain"] or len(history_ids | set(ids)) != page.total:
                        coverage = "uncertain_catalog_scanned"
                    else:
                        coverage = "observed_catalog_scanned"
                    if scopes:
                        completed = s.setdefault('search_scope_results', {})
                        completed[scopes[scope_index]['label']] = {
                            'reason': reason, 'completeness': coverage, 'cards': sum(len(p['ids']) for p in pages) + len(ids),
                            'pages': number, 'filters': effective}
                        if coverage == 'uncertain_catalog_scanned':
                            s['catalogue_scopes_uncertain'] = True
                        if scope_index + 1 < len(scopes):
                            s.update(search_scope_index=scope_index + 1, next_page=1, early_stop=True,
                                     catalogue_uncertain=False)
                            return s
                        reason = 'catalog_end' if all(row['reason'] == 'catalog_end' for row in completed.values()) else 'scopes_finished'
                        coverage = ('uncertain_catalog_scanned' if s.get('catalogue_scopes_uncertain') else
                                    'observed_scopes_scanned')
                    s.update(reason=reason, collection_reason=reason, completeness=coverage, stage="describe")
                    s["catalogue"] = {"finished": True, "reason": reason, "completeness": coverage,
                                      "early_stop_used": (any(row['reason'] == 'threshold' for row in completed.values())
                                                          if scopes else reason == "threshold"),
                                      "last_page_order_guaranteed": page.order_guaranteed}
                    if s.get("extension"):
                        existing = set(s["extension"]["existing_projects"])
                        s["describe_only_ids"] = [pid for pid in s["order"] if str(pid) not in existing]
                return s

            d = self.store.mutate(job_id, commit)
            if d["stage"] != "collect" or d.get('search_scope_index', 0) != scope_index:
                return

    async def describe(self, job_id, source):
        d = self.store.get(job_id)
        for pid in d.get("pending_additions", []):
            self.check_cancel(job_id)
            if str(pid) not in self.store.get(job_id)["projects"]:
                p = await source.project(pid)
                self.store.mutate(job_id, lambda s: s["projects"].__setitem__(str(pid), p))
        self.store.patch(job_id, pending_additions=[])
        d = self.store.get(job_id)
        if d.get('reuse_descriptions_from'):
            origin = self.store.get(d['reuse_descriptions_from'])
            def reuse(job):
                for key, project in job['projects'].items():
                    old = origin['projects'].get(key, {})
                    if old.get('description') and not project.get('description'):
                        for field in ('description', 'description_sha256', 'description_state', 'metadata'):
                            if field in old:
                                project[field] = old[field]
            self.store.mutate(job_id, reuse)
            d = self.store.get(job_id)
        only = d.get('describe_only_ids')
        pending = [p for p in d['projects'].values() if 'description' not in p and (only is None or p['id'] in only)]
        sem = asyncio.Semaphore(3)
        errors = []

        async def one(p):
            async with sem:
                self.check_cancel(job_id)
                try:
                    details = await source.describe(p)
                    if not details.get("description", "").strip():
                        raise SourceError("EMPTY_DESCRIPTION")
                    details.update(description_state="available", description_error=None)
                    self.store.mutate(job_id, lambda s: s["projects"][str(p["id"])].update(details))
                except Cancelled:
                    raise
                except Exception as e:
                    errors.append({"project_id": p["id"], "message": str(e), "stage": "describe", "at": time.time()})
                    self.store.mutate(job_id, lambda s: s["projects"][str(p["id"])].update(
                        description_state="insufficient_data", description_error=str(e), description_checked_at=time.time()))
        # Bounded tasks as well as bounded requests; no task per entire catalogue.
        for offset in range(0, len(pending), 3):
            await asyncio.gather(*(one(p) for p in pending[offset:offset + 3]))
        if errors:
            self.store.mutate(job_id, lambda s: s["errors"].extend(errors))

        self.store.patch(job_id, stage="awaiting_analysis", status="awaiting_analysis", describe_only_ids=None)

    def analysis_batch(self, job_id, offset=0, limit=5):
        if offset < 0 or not 1 <= limit <= 20:
            raise ValueError("offset >= 0, limit 1..20")
        d = self.store.get(job_id)
        if d["stage"] != "awaiting_analysis":
            raise ValueError("Finish catalogue collection before analysis; missing descriptions may be deferred")
        ids = [i for i,p in d["projects"].items() if p.get("description") and i not in d["decisions"]]
        batch = []
        for i in ids[offset:offset + limit]:
            project = dict(d["projects"][i])
            # Keep all description text and semantic metadata, without large unrelated avatar/file listings.
            project["metadata"] = {k:v for k,v in project.get("metadata", {}).items() if k in (
                "id", "summary", "categories", "gameVersion", "isExperimental", "isClientCompatible", "sourceUrl", "wikiUrl", "metadata_source")}
            batch.append(project)
        if "analysis_started_at" not in d:
            self.store.patch(job_id, analysis_started_at=time.time())
        return {"wishes": d["filters"]["wishes"], "requirements_revision": d["requirements_revision"],
                "remaining": len(ids), "available_descriptions": sum(bool(p.get("description")) for p in d["projects"].values()),
                "insufficient_data": [{"id": p["id"], "name": p["name"], "url": p["url"], "error": p.get("description_error")} for p in d["projects"].values() if not p.get("description")],
                "analysis_scope": "Available full descriptions only; unavailable projects are not analyzed", "offset": offset, "next_offset": offset + len(batch),
                "external_data_untrusted": True, "projects": batch,
                "pagination_note": "After saving a batch restart at offset=0: pending list shrinks. Full descriptions, no truncation."}

    def save_analysis(self, job_id, decisions, requirements_revision):
        items = [Decision.model_validate(i).model_dump() for i in decisions]
        def save(d):
            self.idle(d)
            if d["stage"] != "awaiting_analysis" or requirements_revision != d["requirements_revision"]:
                raise ValueError("Stale requirements or wrong stage")
            for item in items:
                p = d["projects"].get(str(item["project_id"]))
                if not p or item["evidence_sha256"] != p.get("description_sha256"):
                    raise ValueError("Missing description or stale evidence digest")
            for item in items:
                d["decisions"][str(item["project_id"])] = item
            d["recheck_ids"] = sorted(set(d.get("recheck_ids", [])) - {item["project_id"] for item in items})
            d["overlap_review"] = None
            d["confirmed"] = None
            d["analysis_updated_at"] = time.time()
        self.store.mutate(job_id, save)
        return {"saved": len(items), "analyzed": len(self.store.get(job_id)["decisions"])}

    def propose(self, job_id, ids, overlap_review, expected_revision):
        if not overlap_review.strip():
            raise ValueError("Explain function overlaps and remaining uncertainties")
        def change(d):
            self.idle(d)
            if expected_revision != d["selection_revision"]:
                raise ValueError("Stale selection revision")
            if {k for k,p in d["projects"].items() if p.get("description")} - set(d["decisions"]):
                raise ValueError("Analyze every collected and added project before final selection")
            if not set(map(str, ids)) <= set(d["decisions"]):
                raise ValueError("Unknown project; use revise_selection to add it")
            for i in ids:
                if not d["decisions"][str(i)]["include"]:
                    raise ValueError(f"Project {i} has exclusion decision; reassess its description first")
            self._selection(d, ids, "proposal")
            d["overlap_review"] = overlap_review
            d["status"] = "awaiting_confirmation"
            if d.get("analysis_started_at"):
                d["timings"]["agent_analysis_wall"] = time.time() - d["analysis_started_at"]
        self.store.mutate(job_id, change)
        return self.selection(job_id)

    def _selection(self, d, ids, note):
        ids = list(dict.fromkeys(ids))
        if 'composition' in d or 'legacy_reports' in d:
            from .composition import enforce_main, pack_state
            enforce_main(d, ids)
            d['composition'] = pack_state(d)
            d['composition']['main'] = ids
        old, new = set(d["selection"]), set(ids)
        diff = {"added": sorted(new - old), "removed": sorted(old - new), "unchanged": sorted(old & new)}
        d["history"].append({"at": time.time(), "revision": d["selection_revision"] + 1,
                             "ids": ids, "diff": diff, "note": note})
        d["selection"], d["selection_revision"] = ids, d["selection_revision"] + 1
        d["confirmed"] = None
        d["installation_complete"] = False
        d.pop("lock", None)
        d.pop("downloads", None)
        d.pop("jar_dependency_validation", None)
        d.pop("pack_export", None)
        return diff

    async def revise(self, job_id, ids, expected_revision, wishes=None, note="User edit", source=None):
        d = self.store.get(job_id)
        self.idle(d)
        if expected_revision != d["selection_revision"]:
            raise ValueError("Stale selection revision")
        ids = list(dict.fromkeys(ids))
        if any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError("Positive project IDs required")
        if set(ids) == set(d["selection"]) and (wishes is None or wishes == d["filters"]["wishes"]):
            return {"diff": {"added": [], "removed": [], "unchanged": sorted(ids)},
                    "selection": self.selection(job_id), "next": "No changes; confirm current revision without reanalysis"}
        added_unknown = set(map(str, ids)) - d["projects"].keys()
        # Metadata additions are queued to detached worker as well.
        def update(s):
            self.idle(s)
            if expected_revision != s["selection_revision"]:
                raise ValueError("Concurrent selection edit")
            diff = self._selection(s, ids, note)
            if wishes is not None and wishes != s["filters"]["wishes"]:
                if "brief" in s:
                    raise ValueError("Use pack_brief update for preferences on a managed job")
                if not wishes.strip():
                    raise ValueError("Wishes cannot be empty")
                s["filters"]["wishes"] = wishes
                s["requirements_revision"] += 1
                s.setdefault("decision_history", []).append({"requirements_revision": s["requirements_revision"]-1, "decisions": s["decisions"]})
                s["decisions"] = {}
                s["decision_basis"] = {}
                s.pop("analysis_started_at", None)
            s["pending_additions"] = sorted(map(int, added_unknown))
            s["overlap_review"] = None
            s["stage"] = "describe" if added_unknown else "awaiting_analysis"
            s["status"] = "queued" if added_unknown else "awaiting_analysis"
            return diff
        diff = self.store.mutate(job_id, update)
        if added_unknown:
            self.launch(job_id)
        return {"diff": diff, "selection": self.selection(job_id), "next": "analyze additions/changed wishes; review impact on selected set; propose"}

    def selection(self, job_id):
        d = self.store.get(job_id)
        return {"revision": d["selection_revision"], "fingerprint": fingerprint(d["selection"]),
                "overlap_review": d["overlap_review"], "confirmed": d["confirmed"],
                "uncertain_decision_count": sum(v.get('status')=='uncertain' for v in d.get('decision_basis',{}).values()),
                "uncertain_decisions_note": "Deferred, not confidently excluded; paginate analysis_overview for all reasons",
                "scope": "Available descriptions only; unavailable projects deferred" if any(not p.get("description") for p in d["projects"].values()) else "All collected descriptions",
                "insufficient_data": [{"id":p["id"],"name":p["name"],"url":p["url"],"error":p.get("description_error")} for p in d["projects"].values() if not p.get("description")],
                "items": [{"id": i, **{k: v for k, v in d["projects"].get(str(i), {}).items() if k in ("name", "url")},
                           "analysis": d["decisions"].get(str(i))} for i in d["selection"]]}

    def confirm(self, job_id, revision, selection_fingerprint, *, target="jars", pack_name="Curated Create Trial", loader_version=None):
        if target == "jars" and os.name == "nt":
            raise ValueError("Direct JAR download requires POSIX. Export a CurseForge ZIP and import it in Prism Launcher.")

        def confirm(d):
            self.idle(d)
            if revision != d["selection_revision"] or selection_fingerprint != fingerprint(d["selection"]):
                raise ValueError("Stale selection; show current list to user")
            if not d["selection"] or not d["overlap_review"] or d["status"] != "awaiting_confirmation":
                raise ValueError("Propose a nonempty fully analyzed list before confirmation")
            if {k for k,p in d["projects"].items() if p.get("description")} - set(d["decisions"]):
                raise ValueError("Unanalyzed projects remain")
            if target == "prism":
                from .preflight import latest, plan_input, plan_key
                plan = latest(self.store, job_id, revision)
                if not plan and (d.get('external_projects') or d.get('install_evidence')):
                    raise ValueError("Run the revision preflight before exporting external files or dependency evidence.")
                if plan and (not plan['ready'] or plan['input_key'] != plan_key(plan_input(d, revision))):
                    raise ValueError("The saved installation plan is blocked or stale. Run preflight again.")
                if plan and loader_version and loader_version != plan['loader']['version']:
                    raise ValueError("Loader differs from the saved plan. Run preflight again.")
                d["export_plan"] = plan
            d["confirmed"] = {"revision": revision, "fingerprint": selection_fingerprint, "at": time.time()}
            d["export_options"] = {"name": pack_name, "loader_version": loader_version}
            d.update(stage="export" if target == "prism" else "install", status="queued", cancel_requested=False)
        self.store.mutate(job_id, confirm, after=lambda db, _: require_available(db, job_id))
        return self.launch(job_id)

    async def export_prism(self, job_id, source):
        from .prism_export import build_manifest, loader_version, write_pack
        d = self.store.get(job_id)
        if not d["confirmed"] or d["confirmed"]["fingerprint"] != fingerprint(d["selection"]):
            raise ValueError("User confirmation missing or selection changed")
        f = Filters.model_validate(d["filters"])
        if f.release_types is None and d.get('release_policy', {}).get('allowed_release_types'):
            f.release_types = d['release_policy']['allowed_release_types']
        self.store.patch(job_id, activity={"operation": "resolving_export_dependencies"})
        if hasattr(source, "seed_projects"):
            source.seed_projects(d["projects"])
        start = time.monotonic()
        from .composition import manual_exclusions
        plan = d.get("export_plan")
        if plan:
            from .preflight import revalidate
            lock = await revalidate(plan, source, f, lambda: self.check_cancel(job_id))
        else:
            lock = await resolve(source, d["selection"], f, lambda: self.check_cancel(job_id),
                                 pins=d.get("file_pins"), excluded=manual_exclusions(d))
        self.store.patch(job_id, lock=lock)
        options = d.get("export_options", {})
        loader = await loader_version(f, plan['loader']['version'] if plan else options.get("loader_version"))
        if plan and any(loader.get(k) != plan['loader'].get(k) for k in ('id', 'version_metadata_sha256')):
            raise SourceError("INSTALLATION_PLAN_STALE: loader metadata changed. Run preflight again.")
        external = plan.get('external_items', []) if plan else []
        manifest = build_manifest(lock, f, loader, options.get("name", "Curated mods"),
                                  has_verified_external=bool(external))
        self.check_cancel(job_id)
        destination = self.store.destination(f.destination)
        basename = f"curated-{job_id}-r{d['selection_revision']}.zip"
        overrides = {}
        if external:
            import tempfile
            import httpx
            from pathlib import Path
            from .modrinth import Modrinth
            with tempfile.TemporaryDirectory(dir=self.store.job_dir(job_id), prefix='prism-overrides-') as folder:
                async with httpx.AsyncClient(timeout=45, headers={'User-Agent': 'Modpack Builder'}) as client:
                    provider = Modrinth(client)
                    for item in external:
                        self.check_cancel(job_id)
                        await provider.verify_item(item, f)
                        if item['acquisition'] != 'manual':
                            path = Path(folder) / item['file_name']
                            await provider.download(item, path)
                            overrides[item['file_name']] = path
                self.check_cancel(job_id)
                archive = write_pack(manifest, destination, basename, overrides)
        else:
            archive = write_pack(manifest, destination, basename)
        report = {"ready": True, "status": "ready_for_prism_import", **archive, "manifest": manifest,
                  "loader": loader, "files_count": len(manifest["files"]) + len(external), "dependencies_complete": True,
                  "dependency_coverage": "Exact source relations and recorded supplemental evidence. Runtime untested.",
                  "jar_evidence_count": len((plan or {}).get('jar_evidence', {}).get('verified_files', [])),
                  "installation_plan_task": plan['task_id'] if plan else None,
                  "prism_import_tested": False, "manual_downloads": [item for item in (plan or {}).get('items', []) if item['acquisition'] == 'manual'],
                  "override_confirmation": list(overrides),
                  "verification_stages": {"plan": "metadata_ready" if plan else "resolved_at_export",
                                          "zip": "written", "import": "untested", "main_menu": "untested",
                                          "world": "untested", "mechanics": "untested"}}
        from .verification import receipt, write_receipt
        report['receipt_path'] = write_receipt(self.store, job_id, d, report)
        evidence = receipt(job_id, d, report)
        self.store.mutate(job_id, lambda s: s["timings"].__setitem__("resolve_and_export", time.monotonic() - start))
        self.store.patch(job_id, stage="done", status="exported", pack_export=report, installation_complete=False,
                         activity={"operation": "ready_for_prism_import"}, installation_receipt=evidence)

    async def install(self, job_id, source):
        d = self.store.get(job_id)
        if not d["confirmed"] or d["confirmed"]["fingerprint"] != fingerprint(d["selection"]):
            raise ValueError("User confirmation missing or selection changed")
        f = Filters.model_validate(d["filters"])
        if not d.get("lock"):
            if hasattr(source, "seed_projects"):
                source.seed_projects(d["projects"])
            self.store.patch(job_id, activity={"operation": "resolving_dependencies"})
            start = time.monotonic()
            from .composition import manual_exclusions
            if f.release_types is None and d.get('release_policy', {}).get('allowed_release_types'):
                f.release_types = d['release_policy']['allowed_release_types']
            lock = await resolve(source, d["selection"], f, lambda: self.check_cancel(job_id),
                                 excluded=manual_exclusions(d))
            for pid, file in lock["files"].items():
                # Authoritative URLs for manual downloads, including transitive dependencies.
                self.check_cancel(job_id)
                p = d["projects"].get(pid) or await source.project(int(pid))
                file["_slug"] = p["slug"]
                if p.get("allow_mod_distribution", p.get("metadata", {}).get("allowModDistribution")) is False:
                    file["downloadUrl"] = None
                    file["distribution_note"] = "Author disallowed automatic third-party distribution"
            self.store.patch(job_id, lock=lock)
            self.store.mutate(job_id, lambda s: s["timings"].__setitem__("resolve", time.monotonic() - start))
        d = self.store.get(job_id)
        destination = self.store.destination(f.destination)
        results = dict(d.get("downloads", {}))
        errors = []
        begin = time.monotonic()
        async with httpx.AsyncClient(timeout=90) as client:
            for pid, file in d["lock"]["files"].items():
                self.check_cancel(job_id)
                self.store.patch(job_id, activity={"operation": "downloading", "project_id": int(pid), "file_id": file["id"]})
                try:
                    # Recheck every existing file on resume, never trust a previous success record.
                    results[pid] = await download_one(client, file, destination, lambda: self.check_cancel(job_id))
                except Cancelled:
                    raise
                except Exception as e:
                    results[pid] = {"status": "error", "message": str(e)}
                    errors.append({"project_id": int(pid), "message": str(e)})
                self.store.patch(job_id, downloads=results)
        self.store.mutate(job_id, lambda s: s["timings"].__setitem__("download", s["timings"].get("download", 0) + time.monotonic() - begin))
        complete = all(r["status"] in ("downloaded", "already_verified") for r in results.values())
        if complete:
            from .jar_validation import validate_pack
            self.store.patch(job_id, activity={"operation": "validating_jar_dependencies"})
            check = validate_pack([destination / f["fileName"] for f in d["lock"]["files"].values()], f)
            self.store.patch(job_id, jar_dependency_validation=check)
            if check["errors"] or check["unresolved"]:
                complete = False
                errors.extend({"message": msg} for msg in check["errors"] + check["unresolved"])
        if errors:
            self.store.mutate(job_id, lambda s: s["errors"].extend(errors))
        self.store.patch(job_id, status="completed" if complete else "partial", stage="done" if complete else "install",
                         installation_complete=complete, activity={"operation": "finished" if complete else "requires_attention"})

    def export(self, job_id):
        d = self.store.get(job_id)
        path = self.store.job_dir(job_id) / "report.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return str(path)
