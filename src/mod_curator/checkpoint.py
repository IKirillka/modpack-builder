"""Revision-scoped, expiring preflight responses and compact progress."""
from __future__ import annotations

import json
import time


MAX_AGE = 24 * 3600


class PreflightSource:
    def __init__(self, engine, task_id, source, scope):
        self.engine, self.task_id, self.source, self.scope = engine, task_id, source, scope
        self.started = time.monotonic()
        self.last_poll = 0.0
        self.last_write = 0.0
        self.activity = {'operation': 'preflight', 'project_id': None, 'chain': [], 'states': 0,
                         'file_lists': 0, 'files_checked': 0, 'cache_hits': 0,
                         'network_seconds': 0.0, 'parse_seconds': 0.0, 'search_seconds': 0.0}
        self._checked = set()
        self.source_time = 0.0

    def __getattr__(self, name):
        result = getattr(self.source, name)
        return self.pinned_file if name == 'exact_file' else result

    def cancel(self, *, force=False):
        now = time.monotonic()
        if force or now - self.last_poll >= .25:
            self.engine.check_cancel(self.task_id)
            self.last_poll = now

    def progress(self, pid=None, chain=(), states=0, *, force=False, **extra):
        self.activity.update(extra)
        if pid is not None:
            self.activity.update(project_id=pid, chain=chain, states=states)
        now = time.monotonic()
        self.activity['search_seconds'] = round(max(0, now - self.started - self.source_time), 4)
        if force or now - self.last_write >= 1:
            self.engine.store.patch(self.task_id, activity=self.activity.copy())
            self.last_write = now

    async def fetch(self, key, operation, *, reuse=True, persist=True, meter=None):
        start = time.monotonic()
        meter = meter or self.source
        start_network = getattr(meter, 'network_seconds', 0.0)
        parse_started = None
        parse_elapsed = 0.0
        self.cancel(force=True)
        try:
            if reuse:
                with self.engine.store.connect() as db:
                    row = db.execute('SELECT data FROM preflight_cache WHERE job_id=? AND scope=? AND item=? AND captured>?',
                                     (*self.scope, key, time.time() - MAX_AGE)).fetchone()
                if row:
                    self.activity['cache_hits'] += 1
                    return json.loads(row[0])
            parse_started = time.monotonic()
            try:
                result = await operation()
            finally:
                parse_elapsed = time.monotonic() - parse_started
            if persist and result:
                with self.engine.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO preflight_cache VALUES(?,?,?,?,?)',
                               (*self.scope, key, time.time(), json.dumps(result, ensure_ascii=False)))
            return result
        finally:
            elapsed = time.monotonic() - start
            network = getattr(meter, 'network_seconds', 0.0) - start_network
            self.source_time += elapsed
            self.activity['network_seconds'] += network
            self.activity['parse_seconds'] += max(0, parse_elapsed - network)
            self.activity['checkpoint_seconds'] = self.activity.get('checkpoint_seconds', 0.0) + max(0, elapsed - parse_elapsed)
            self.progress()

    def note_file(self, pid, fid):
        self._checked.add((pid, fid))
        self.activity['files_checked'] = len(self._checked)

    async def files(self, pid, f):
        self.progress(project_id=pid, phase='file_list', force=True)
        result = await self.fetch(f'list:{pid}', lambda: self.source.files(pid, f))
        self.activity['file_lists'] += 1
        return result

    async def pinned_file(self, pid, fid, filters):
        self.progress(project_id=pid, file_id=fid, phase='pinned_file', force=True)
        return await self.fetch(f'pinned:{pid}:{fid}', lambda: self.source.exact_file(pid, fid, filters))

    async def complete_file(self, item):
        pid, fid = item['modId'], item['id']
        self.progress(project_id=pid, file_id=fid, phase='file_relations', force=True)
        return await self.fetch(f'file:{pid}:{fid}', lambda: self.source.complete_file(item))
