"""User presets, no-call previews, and persistent budgets for Codex extraction."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time
import uuid

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .analysis_roles import (
    DEFAULT_MODEL, Effort, ModelId, ROLE_KEYS, model_options, role_options, separate_retry,
)
from .facts import Facts, cache_key, normalize
from .lifecycle import require_available
from .store import Store


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preset: Literal['economy', 'balanced', 'faster'] | None = None
    workers: int | None = Field(default=None, ge=1, le=2)
    batches: int | None = Field(default=None, ge=1, le=100)
    model: ModelId | None = None
    effort: Effort | None = None
    retry_model: ModelId | Literal['inherit'] | None = None
    retry_effort: Effort | Literal['inherit'] | None = None
    projects_per_batch: int | None = Field(default=None, ge=1, le=30)
    chars_per_batch: int | None = Field(default=None, ge=1000, le=200000)


PRESETS = {
    'economy': dict(workers=1, batches=1, effort='medium', projects_per_batch=25, chars_per_batch=50000),
    'balanced': dict(workers=1, batches=3, effort='medium', projects_per_batch=25, chars_per_batch=50000),
    'faster': dict(workers=2, batches=6, effort='medium', projects_per_batch=25, chars_per_batch=50000),
}
LABELS = {'economy': 'Экономный', 'balanced': 'Сбалансированный', 'faster': 'Быстрее'}


def migrate(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS economy_settings(scope TEXT PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS economy_runs(
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL, settings TEXT NOT NULL,
        state TEXT NOT NULL, created_at REAL NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0,
        error TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS economy_active_job ON economy_runs(job_id) WHERE state='active';
    CREATE TABLE IF NOT EXISTS economy_projects(
        run_id TEXT NOT NULL, project_id INTEGER NOT NULL, cache_key TEXT NOT NULL,
        PRIMARY KEY(run_id, project_id)
    );
    CREATE TABLE IF NOT EXISTS economy_claims(
        run_id TEXT NOT NULL, batch_id TEXT NOT NULL UNIQUE, projects INTEGER NOT NULL,
        chars INTEGER NOT NULL, retries INTEGER NOT NULL, PRIMARY KEY(run_id, batch_id)
    );
    INSERT OR IGNORE INTO curator_migrations VALUES(2,strftime('%s','now'));
    """)


class Economy:
    def __init__(self, store):
        self.store = store

    def settings(self, job_id=None, patch=None, reset=False):
        if job_id:
            self.store.get(job_id)
        scope = job_id or 'default'
        with self.store.connect() as connection:
            if patch is not None or reset:
                connection.execute('BEGIN IMMEDIATE')
            defaults, local = self._setting_rows(connection, job_id)
            if patch is not None or reset:
                current = dict(local if job_id else defaults)
                if reset:
                    current = {}
                values = patch.model_dump(exclude_none=True) if patch else {}
                if 'preset' in values:
                    current = {key: value for key, value in current.items() if key in ROLE_KEYS}
                current.update(values)
                connection.execute('INSERT OR REPLACE INTO economy_settings VALUES(?,?)',
                                   (scope, json.dumps(current)))
                defaults, local = self._setting_rows(connection, job_id)
            latest = (connection.execute('SELECT id,state FROM economy_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1', (job_id,)).fetchone()
                      if job_id else None)
        effective = self._effective(defaults, local)
        return {'job_id': job_id, 'scope': 'job' if job_id else 'default',
                'mode': LABELS[effective['preset']], 'settings': effective,
                'speed': 'Standard', 'model': effective['model'], 'overrides': local if job_id else defaults,
                'agent_roles': role_options(effective), 'model_options': model_options(),
                'main_agent_task': 'Агент текущего чата подбирает моды по ТЗ, решает спорные сравнения и применяет правки пользователя.',
                'latest_run': {'id': latest[0], 'state': latest[1]} if latest else None,
                'presets': [{'id': key, 'label': LABELS[key], 'workers': val['workers'], 'batches': val['batches']}
                            for key, val in PRESETS.items()]}

    @staticmethod
    def _setting_rows(connection, job_id):
        rows = dict(connection.execute('SELECT scope,data FROM economy_settings WHERE scope IN (?,?)',
                                       ('default', job_id or 'default')))
        return json.loads(rows.get('default', '{}')), json.loads(rows.get(job_id, '{}'))

    @staticmethod
    def _effective(defaults, local):
        inherited = {key: value for key, value in defaults.items() if key in ROLE_KEYS} if 'preset' in local else defaults
        values = {**inherited, **local}
        name = values.get('preset', 'economy')
        return {'model': DEFAULT_MODEL, 'retry_model': 'inherit', 'retry_effort': 'inherit',
                **PRESETS[name], **values, 'preset': name}

    def _plan(self, connection, job_id, include_reviewed, batch_override=None):
        row = connection.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown job')
        job = json.loads(row[0])
        settings = self._effective(*self._setting_rows(connection, job_id))
        if batch_override is not None:
            settings['batches'] = SettingsPatch(batches=batch_override).batches
        cached = {r[0] for r in connection.execute('SELECT cache_key FROM fact_cache')}
        work = dict((pid, (key, state, attempts, until)) for pid, key, state, attempts, until in
                    connection.execute('SELECT project_id,cache_key,state,attempts,lease_until FROM fact_queue WHERE job_id=?', (job_id,)))
        counts = dict(total=len(job['projects']), saved_decisions=0, cache_hits=0, no_description=0,
                      retry_limit=0, already_leased=0, new=0, retries=0, long_texts=0)
        targets, sizes, retry_sizes = {}, [], []
        split_roles = separate_retry(settings)
        recheck = set(job.get('recheck_ids', []))
        now = time.time()
        for key, project in job['projects'].items():
            pid = int(key)
            desc = project.get('description')
            if not desc:
                counts['no_description'] += 1
                continue
            if not include_reviewed and project.get('description_sha256') and pid not in recheck and job['decisions'].get(key, {}).get('evidence_sha256') == project.get('description_sha256'):
                counts['saved_decisions'] += 1
                continue
            content_key = cache_key(pid, desc)[0]
            if content_key in cached:
                counts['cache_hits'] += 1
                targets[pid] = content_key
                continue
            old = work.get(pid)
            attempts = 0
            if old and old[0] == content_key:
                _, state, attempts, until = old
                if state == 'leased' and until > now:
                    counts['already_leased'] += 1
                    continue
                if state == 'failed' or attempts >= 3:
                    counts['retry_limit'] += 1
                    continue
            targets[pid] = content_key
            size = len(normalize(desc))
            (retry_sizes if attempts and split_roles else sizes).append(size)
            counts['long_texts'] += size > settings['chars_per_batch']
            counts['retries' if attempts else 'new'] += 1
        limits = settings['projects_per_batch'], settings['chars_per_batch']
        estimate = batch_count(sizes, *limits) + batch_count(retry_sizes, *limits)
        return job, settings, counts, targets, estimate

    def preview(self, job_id, include_reviewed=False, batches=None):
        with self.store.connect() as connection:
            job, settings, counts, _, estimate = self._plan(connection, job_id, include_reviewed, batches)
        return {'job_id': job_id, 'settings': settings, 'counts': counts, 'estimated_initial_batches': estimate,
                'agent_roles': role_options(settings), 'includes_previously_reviewed': include_reviewed, 'pending_reassessment': len(job.get('recheck_ids', [])),
                'measurement': 'Initial queue estimate; retries consume the same batch budget. No subscription quota is inferred.'}

    def start(self, job_id, include_reviewed=False, batches=None, *, detached=True):
        run_id = uuid.uuid4().hex
        with self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            require_available(connection, job_id)
            job, settings, counts, targets, estimate = self._plan(connection, job_id, include_reviewed, batches)
            if job['status'] in ('queued', 'running'):
                raise ValueError('Wait for the catalogue/description worker to finish')
            if connection.execute("SELECT 1 FROM economy_runs WHERE job_id=? AND state='active'", (job_id,)).fetchone():
                raise ValueError('An analysis run already exists. Check, stop, or resume that run.')
            if connection.execute("SELECT 1 FROM fact_batches WHERE job_id=? AND state='leased' AND lease_until>?",
                                  (job_id,time.time())).fetchone():
                raise ValueError('An extraction batch is already in progress for this job')
            Facts(self.store).prepare(job_id, list(targets), connection=connection, job=job)
            state = 'active' if estimate else 'complete'
            connection.execute('INSERT INTO economy_runs(id,job_id,settings,state,created_at) VALUES(?,?,?,?,?)',
                               (run_id, job_id, json.dumps(settings), state, time.time()))
            connection.executemany('INSERT INTO economy_projects VALUES(?,?,?)',
                                   ((run_id, pid, key) for pid, key in targets.items()))
        if detached and state == 'active':
            self._launch_or_pause(run_id)
        return self.status(run_id) | {'initial': counts, 'estimated_initial_batches': estimate}

    def status(self, run_id):
        with self.store.connect() as connection:
            job_id, settings, state, stop, error = self._read(connection, run_id)
            used, claims, chars, retries = connection.execute(
                'SELECT COUNT(*),COALESCE(SUM(projects),0),COALESCE(SUM(chars),0),COALESCE(SUM(retries),0) FROM economy_claims WHERE run_id=?',
                (run_id,)).fetchone()
            rows = connection.execute(
                """SELECT CASE WHEN q.cache_key!=p.cache_key OR q.state IS NULL THEN 'stale'
                               WHEN q.state='leased' AND q.lease_until<=? THEN 'expired'
                               ELSE q.state END AS status, COUNT(*)
                   FROM economy_projects p LEFT JOIN fact_queue q
                    ON q.job_id=? AND q.project_id=p.project_id WHERE p.run_id=? GROUP BY status""",
                (time.time(),job_id,run_id))
            counts = dict(rows)
            batches = [bid for (bid,) in connection.execute('SELECT batch_id FROM economy_claims WHERE run_id=?', (run_id,))]
        usage = {}
        reported = 0
        observed_workers = {}
        folder = self.store.job_dir(job_id) / 'extraction-runs'
        for bid in batches:
            try:
                metrics = json.loads((folder / bid / 'metrics.json').read_text())
            except (OSError, ValueError):
                continue
            observed = metrics.get('observed') or {}
            if observed.get('model') and observed.get('reasoning_effort'):
                key = (metrics.get('role', 'extract'), metrics.get('requested_model'),
                       observed['model'], observed['reasoning_effort'])
                observed_workers[key] = observed_workers.get(key, 0) + 1
            if metrics.get('usage'):
                reported += 1
                for key, value in metrics['usage'].items():
                    if type(value) is int:
                        usage[key] = usage.get(key, 0) + value
        return {'run_id': run_id, 'job_id': job_id, 'state': state, 'stop_requested': bool(stop),
                'budget_batches': settings['batches'], 'used_batches': used, 'remaining_budget': settings['batches'] - used,
                'assigned_projects_including_retries': claims, 'retry_assignments': retries, 'source_chars_assigned': chars,
                'target_counts': counts, 'token_usage': usage, 'batches_with_token_usage': reported,
                'agent_roles': role_options(settings),
                'observed_workers': [{'role': role, 'requested_model': wanted, 'model': model,
                                      'effort': effort,'batches': count}
                                     for (role,wanted,model,effort),count in observed_workers.items()],
                'error': error, 'poll_after_seconds': 60}

    def stop(self, run_id):
        with self.store.connect() as connection:
            self._read(connection, run_id)
            connection.execute("UPDATE economy_runs SET stop_requested=1 WHERE id=? AND state='active'", (run_id,))
        return self.status(run_id)

    def resume(self, run_id):
        lock = FileLock(self.store.root / f'economy-{self._validated_id(run_id)}.lock')
        try:
            with lock.acquire(timeout=0):
                status = self.status(run_id)
                if status['state'] in ('complete', 'budget_exhausted', 'needs_attention'):
                    return status
                if status['remaining_budget'] <= 0:
                    self._finish(run_id, 'budget_exhausted')
                    return self.status(run_id)
                with self.store.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    require_available(connection, status['job_id'])
                    connection.execute("UPDATE economy_runs SET stop_requested=0,state='active',error=NULL WHERE id=?", (run_id,))
        except Timeout:
            return self.status(run_id)
        self._launch_or_pause(run_id)
        return self.status(run_id)

    @staticmethod
    def _validated_id(run_id):
        if str(uuid.UUID(run_id)).replace('-', '') != run_id:
            raise ValueError('Invalid analysis run ID')
        return run_id

    @staticmethod
    def _read(connection, run_id):
        row = connection.execute('SELECT job_id,settings,state,stop_requested,error FROM economy_runs WHERE id=?',
                                 (run_id,)).fetchone()
        if not row:
            raise ValueError('Unknown analysis run')
        return row[0], json.loads(row[1]), *row[2:]

    def _finish(self, run_id, state, error=None):
        with self.store.connect() as connection:
            connection.execute('UPDATE economy_runs SET state=?,error=? WHERE id=?', (state,error,run_id))

    def _launch_or_pause(self, run_id):
        try:
            self.launch(run_id)
        except OSError as exc:
            self._finish(run_id, 'paused', str(exc))

    def launch(self, run_id):
        with (self.store.root / 'economy-worker.log').open('ab') as log:
            subprocess.Popen([sys.executable, '-m', 'mod_curator', 'economy-worker', run_id],
                             env={**os.environ, 'MOD_CURATOR_HOME': str(self.store.root)},
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)

    def work(self, run_id, runner=None):
        if runner is None:
            from .extraction_worker import run_batch
            runner = run_batch
        lock = FileLock(self.store.root / f'economy-{self._validated_id(run_id)}.lock')
        try:
            with lock.acquire(timeout=0):
                with self.store.connect() as connection:
                    job_id, settings, state, _, _ = self._read(connection, run_id)
                if state != 'active':
                    return self.status(run_id)
                facts = Facts(self.store)
                def worker():
                    while True:
                        batch = facts.claim(job_id, f'codex-cli-{os.getpid()}', run_id=run_id)
                        if not batch.get('batch_id'):
                            return None
                        try:
                            runner(self.store, batch, batch['agent']['effort'])
                        except Exception as exc:
                            message = str(exc)
                            facts.fail(batch['batch_id'], message)
                            self.stop(run_id)
                            return message
                with ThreadPoolExecutor(max_workers=settings['workers']) as executor:
                    futures = [executor.submit(worker) for _ in range(settings['workers'])]
                    errors = [message for future in futures if (message := future.result())]
                status = self.status(run_id)
                counts = status['target_counts']
                pending = sum(counts.get(k, 0) for k in ('queued','leased','expired'))
                if errors:
                    self._finish(run_id, 'paused', errors[0])
                elif not pending:
                    self._finish(run_id, 'needs_attention' if counts.get('failed') or counts.get('stale') else 'complete')
                elif status['remaining_budget'] == 0:
                    self._finish(run_id, 'budget_exhausted')
                else:
                    self._finish(run_id, 'paused')
        except Timeout:
            pass
        return self.status(run_id)


def batch_count(sizes, limit, max_chars):
    count = items = chars = 0
    for size in sizes:
        if items and (items == limit or chars + size > max_chars):
            count += 1
            chars = items = 0
        chars += size
        items += 1
    return count + bool(items)


def interactive(store=None):
    """Small local menu mirroring the chat settings, with optional job override."""
    service = Economy(store or Store())
    job_id = input('ID сборки (Enter для настроек по умолчанию): ').strip() or None
    current = service.settings(job_id)
    print(f"Текущий режим: {current['mode']}. Порций: {current['settings']['batches']}. "
          f"Воркеров: {current['settings']['workers']}. Скорость: Standard.")
    for role in current['agent_roles']:
        print(f"{role['label']}: {role['model_label']}, {role['effort']}. {role['task']}")
    choices = list(PRESETS)
    for index, name in enumerate(choices, 1):
        print(f"{index}. {LABELS[name]}. Порций: {PRESETS[name]['batches']}. Воркеров: {PRESETS[name]['workers']}.")
    print('4. Изменить настройки текущего режима')
    print('5. Выбрать модели и глубину рассуждений')
    answer = input('Номер режима, 0 для сброса или Enter для выхода: ').strip()
    if not answer:
        return current
    if answer == '0':
        return service.settings(job_id, reset=True)
    if answer == '5':
        return interactive_models(service, job_id, current)
    if answer == '4':
        def number(prompt):
            value = input(f'{prompt} (Enter оставить): ').strip()
            return int(value) if value else None
        batches = number('Предел порций с повторами')
        workers = number('Воркеров 1 или 2')
        count = number('Проектов в порции, 1–30')
        chars = number('Целевой предел символов в порции, 1000–200000')
        effort = input('Глубина анализа medium/high (Enter оставить): ').strip() or None
        return service.settings(job_id, SettingsPatch(workers=workers,batches=batches,effort=effort,
                                                       projects_per_batch=count,chars_per_batch=chars))
    if answer not in ('1','2','3'):
        raise ValueError('Выберите номер 1, 2, 3 или 4')
    batches = input('Другой предел порций, включая повторы (Enter для значения режима): ').strip()
    return service.settings(job_id, SettingsPatch(preset=choices[int(answer)-1], batches=int(batches) if batches else None))


def interactive_models(service, job_id, current):
    options = current['model_options']
    for index, option in enumerate(options, 1):
        print(f"{index}. {option['label']}. {option['use']}")
    values = {}
    for role in current['agent_roles']:
        retry = role['role'] == 'retry'
        print(f"{role['label']}. {role['task']}")
        tip = ", 0 как в первом проходе" if retry else ""
        selected = input(f"Модель, номер{tip} (Enter оставить): ").strip()
        if selected:
            if not selected.isascii() or not selected.isdigit():
                raise ValueError('Введите номер модели из списка')
            number = int(selected)
            if number == 0 and retry:
                values['retry_model'] = 'inherit'
            elif 1 <= number <= len(options):
                values['retry_model' if retry else 'model'] = options[number-1]['id']
            else:
                raise ValueError('Введите номер модели из списка')
        effort = input(f"Глубина low/medium/high{'/inherit' if retry else ''} (Enter оставить): ").strip().lower()
        if effort:
            values['retry_effort' if retry else 'effort'] = effort
    return service.settings(job_id, SettingsPatch(**values)) if values else current
