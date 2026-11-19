"""Revision-scoped installation plans and exact-file revalidation."""
from __future__ import annotations

import hashlib
import json
import time

import httpx

from .brief import legacy_brief
from .composition import at_revision, external_summary
from .models import Filters, SourceError
from .prism_export import loader_version
from .resolver import ResolutionError, compatible, resolve


def filters_for(job, revision):
    state = at_revision(job, revision)
    data = legacy_brief({'filters': state['filters'], 'release_policy': job.get('release_policy', {})})['filters']
    data.update(wishes=state['filters']['wishes'])
    if releases := state['brief'].get('filters', {}).get('release_types'):
        data['release_types'] = releases
    return Filters.model_validate(data)


def plan_input(job, revision, curseforge_only=False):
    state = at_revision(job, revision)
    pack = state['pack']
    f = filters_for(job, revision)
    return {'roots': pack['main'], 'excluded': [int(pid) for pid, choice in pack['manual'].items()
                                               if choice['list'] == 'excluded'],
            'pins': state.get('file_pins', {}), 'external': state['external_projects'],
            'curseforge_only': curseforge_only, 'minecraft': f.minecraft,
            'loader': f.loader, 'release_types': sorted(f.release_types or ([1, 2, 3] if f.allow_prerelease else [1]))}


def plan_key(inputs):
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def latest(store, job_id, revision, curseforge_only=False, *, ready_only=False):
    sql = "SELECT data FROM installation_plans WHERE job_id=? AND revision=? AND curseforge_only=?"
    if ready_only:
        sql += " AND ready=1"
    with store.connect() as db:
        row = db.execute(sql + " ORDER BY rowid DESC LIMIT 1", (job_id, revision, int(curseforge_only))).fetchone()
    return json.loads(row[0]) if row else None


def save(store, plan):
    with store.connect() as db:
        insert(db, plan)


def insert(db, plan):
    db.execute("INSERT INTO installation_plans (job_id,revision,curseforge_only,ready,data) VALUES (?,?,?,?,?)",
               (plan['job_id'], plan['revision'], int(plan['inputs']['curseforge_only']),
                int(plan['ready']), json.dumps(plan, ensure_ascii=False)))


def save_with_revision(plan):
    def save_revision(db, result):
        plan.update(revision=result['revision'], applied=result)
        insert(db, plan)
    return save_revision


def summary(plan, report_path=None):
    return {k: plan[k] for k in ('job_id', 'revision', 'task_id', 'ready', 'checked_at', 'minecraft',
                                  'loader', 'release_types', 'external_projects_omitted')} | {
        'files_count': len(plan.get('lock', {}).get('files', {})),
        'blockers_count': len(plan['blockers']), 'blockers': plan['blockers'][:5],
        'warnings_count': len(plan['warnings']), 'warnings': plan['warnings'][:5],
        'unchecked': plan['unchecked'], 'report_path': report_path,
    }


def schedule(engine, job_id, revision=None, requested_loader=None, curseforge_only=False, *,
             rollback=None, update_scope=None, update_apply=None):
    job = engine.store.get(job_id)
    revision = job['selection_revision'] if revision is None else revision
    inputs = plan_input(job, revision, curseforge_only)
    related = {str(pid) for pid in inputs['roots']}
    previous = latest(engine.store, job_id, revision, curseforge_only, ready_only=True)
    if previous:
        related.update(previous['lock']['files'])
    if update_apply:
        related.update(update_apply['projects'])
    task_id = engine.store.create(filters_for(job, revision).model_dump(), stage='pack_check',
                                  projects={pid: p for pid, p in job['projects'].items() if pid in related},
                                  check_options={'job_id': job_id, 'revision': revision, 'inputs': inputs,
                                                 'loader_version': requested_loader, 'rollback': rollback,
                                                 'update_scope': update_scope, 'update_apply': update_apply})
    return {**engine.launch(task_id), 'source_job_id': job_id, 'revision': revision,
            'operation': ('updates' if update_scope is not None else 'apply_updates' if update_apply else
                          'rollback' if rollback else 'preflight')}


def distribution_warning(file, project):
    if file.get('allowModDistribution', project.get('allow_mod_distribution', project.get('metadata', {}).get('allowModDistribution'))) is False:
        pid, fid = file['modId'], file['id']
        slug = file.get('_slug') or project.get('slug')
        return {'code': 'manual_download', 'project_id': pid, 'file_id': fid,
                'url': (f'https://www.curseforge.com/minecraft/mc-mods/{slug}/files/{fid}' if slug
                        else f'https://www.curseforge.com/minecraft/mc-mods/projects/{pid}'),
                'message': 'The source explicitly disallows automatic third-party distribution'}


def unchecked_metadata():
    return ['JAR-internal dependency declarations and version ranges unavailable in website/API metadata',
            'Prism import and manual downloads without explicit distribution metadata',
            'Minecraft startup and runtime compatibility']


async def build_plan(engine, task_id, source, options, f, projects, *, track_curation=True):
    inputs = options['inputs']
    plan = {'job_id': options['job_id'], 'revision': options['revision'], 'task_id': task_id,
            'inputs': inputs, 'input_key': plan_key(inputs), 'minecraft': inputs['minecraft'],
            'loader': None, 'release_types': inputs['release_types'], 'ready': False,
            'external_projects_omitted': external_summary(inputs['external']) if inputs['curseforge_only'] else {},
            'checked_at': time.time(), 'blockers': [], 'warnings': [], 'unchecked': unchecked_metadata()}
    if not inputs['roots']:
        plan['blockers'].append({'code': 'empty_selection', 'message': 'Choose a nonempty main list'})
    if inputs['external'] and not inputs['curseforge_only']:
        plan['blockers'].append({'code': 'external_projects', 'projects': external_summary(inputs['external']),
                                 'message': 'CurseForge plans cannot resolve these external projects'})
    if track_curation:
        job = engine.store.get(options['job_id'])
        add_curation_notes(engine.store, plan, job, at_revision(job, options['revision']), projects)
    if plan['blockers']:
        return plan
    if hasattr(source, 'seed_projects'):
        source.seed_projects(projects)
    try:
        lock = await resolve(source, inputs['roots'], f, lambda: engine.check_cancel(task_id),
                             pins={**options.get('temporary_pins', {}), **inputs['pins']}, excluded=inputs['excluded'])
        plan['lock'] = lock
        plan['loader'] = await loader_version(f, options['loader_version'])
    except ResolutionError as exc:
        plan['blockers'].extend(exc.issues)
        return plan
    except (SourceError, ValueError, httpx.HTTPError) as exc:
        plan['blockers'].append({'code': 'metadata_unverified', 'message': str(exc)})
        return plan
    for pid, file in lock['files'].items():
        project = {**projects.get(pid, {}), **getattr(source, 'known_projects', {}).get(int(pid), {})}
        if project:
            plan.setdefault('projects', {})[pid] = {k: project[k] for k in ('id', 'name', 'slug', 'url') if k in project}
        if warning := distribution_warning(file, project):
            plan['warnings'].append(warning)
    plan['ready'] = True
    return plan


def add_curation_notes(store, plan, job, revision, projects):
    roots = set(plan['inputs']['roots'])
    for pair in revision.get('relationships', []):
        if not {pair['first'], pair['second']} <= roots:
            continue
        if pair['kind'] == 'incompatibility':
            code = 'reported_incompatibility'
        elif pair['kind'] in ('substitute', 'partial_overlap'):
            code = 'functional_overlap'
        elif pair['kind'] == 'insufficient_data':
            code = 'insufficient_data'
        else:
            continue
        plan['warnings'].append({'code': code, **pair})
    known_pairs = {frozenset((p['first'], p['second'])) for p in revision.get('relationships', [])}
    if len(roots) > 1 and revision['revision'] == job['selection_revision']:
        from .refinement import review_pairs
        leads = review_pairs(store, job['id'], group='main', limit=8)
        for pair in leads['pairs']:
            if frozenset((pair['first'], pair['second'])) not in known_pairs:
                plan['warnings'].append({'code': 'possible_functional_overlap', **pair,
                                         'verification': 'lexical_lead_only'})
        plan['unchecked'].append(f"Full semantic comparison of alternatives (up to 8 of {leads['suggested_total']} local leads shown)")
    for pid in roots:
        project = projects.get(str(pid), {})
        decision = job['decisions'].get(str(pid), {}) if revision['requirements_revision'] == job['requirements_revision'] else {}
        if not project.get('description') or not decision or decision.get('uncertainties'):
            plan['warnings'].append({'code': 'insufficient_data', 'project_id': pid,
                                     'uncertainties': decision.get('uncertainties', []),
                                     'description_unavailable': not bool(project.get('description')),
                                     'decision_unavailable_for_revision': not bool(decision)})


def file_signature(file):
    return {k: file.get(k) for k in ('id', 'modId', 'releaseType', 'isAvailable', 'fileName',
                                    'fileLength', 'version', 'versionNumber', 'versionConstraints')} | {
        'hashes': sorted(file.get('hashes', []), key=lambda h: (h.get('algo', 0), h.get('value', ''))),
        'dependencies': sorted(file.get('dependencies', []), key=lambda x: json.dumps(x, sort_keys=True)),
    }


async def revalidate(plan, source, f, check_cancel):
    """Require the same files AND relevant file metadata; never substitute an export file."""
    lock = plan['lock']
    exact_file = getattr(source, 'exact_file', None)
    checked = {}
    for key, saved in lock['files'].items():
        check_cancel()
        pid, fid = int(key), saved['id']
        try:
            if exact_file:
                current = await exact_file(pid, fid, f)
            else:
                items = await source.files(pid, f)
                current = next((item for item in items if item.get('id') == fid and compatible(item, pid, f)), None)
                if current and not current.get('_details_complete', True):
                    current = await source.complete_file(current)
        except SourceError as exc:
            raise SourceError(f'INSTALLATION_PLAN_STALE_OR_UNVERIFIABLE: project {pid}, file {fid}: {exc}. Run preflight again.') from exc
        if (not current or current.get('id') != fid or not compatible(current, pid, f)
                or not isinstance(current.get('dependencies'), list) or not current.get('_details_complete', True)):
            raise SourceError(f'INSTALLATION_PLAN_STALE: fixed file {fid} of project {pid} is unavailable or incompatible. Run preflight again.')
        if file_signature(current) != file_signature(saved):
            raise SourceError(f'INSTALLATION_PLAN_STALE: dependency metadata or identity changed for project {pid}, file {fid}. Run preflight again.')
        checked[key] = current
    return {**lock, 'files': checked}


async def run(engine, task_id, source):
    task = engine.store.get(task_id)
    options = task['check_options']
    f = Filters.model_validate(task['filters'])
    if options.get('update_scope') is not None or options.get('update_apply'):
        from .updates import run as update
        await update(engine, task_id, source, task, f, options)
        return
    rollback = options.get('rollback')
    if rollback:
        options['inputs']['pins'] = {**options['inputs']['pins'], str(rollback['project_id']): rollback['file_id']}
        options['temporary_pins'] = rollback['fixed_roots']
    plan = await build_plan(engine, task_id, source, options, f, task['projects'])
    if rollback and plan['ready']:
        from .composition import Composition
        def apply(job, pack):
            job['file_pins'] = options['inputs']['pins']
            return {'file_change': {k: v for k, v in rollback.items() if k != 'fixed_roots'}}
        if str(rollback['project_id']) not in plan['lock']['files']:
            plan['ready'] = False
            plan['blockers'].append({'code': 'project_no_longer_required', 'project_id': rollback['project_id'],
                                     'message': 'This file would no longer be part of the required dependency closure'})
        else:
            Composition(engine)._commit(options['job_id'], options['revision'], 'file_rollback', apply,
                                        after_commit=save_with_revision(plan))
    if not rollback:
        save(engine.store, plan)
    path = engine.store.job_dir(task_id) / 'installation-plan.json'
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    engine.store.patch(task_id, check_summary=summary(plan, str(path)), check_report_path=str(path),
                       check_plan=plan, stage='done', status='completed' if plan['ready'] else 'blocked',
                       activity={'operation': 'metadata_ready' if plan['ready'] else 'requires_attention'})
