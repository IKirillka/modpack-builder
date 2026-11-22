"""Revision-scoped installation plans and exact-file revalidation."""
from __future__ import annotations

import hashlib
import json
import time

import httpx

from .brief import legacy_brief
from .composition import at_revision, external_summary
from .checkpoint import PreflightSource
from .modrinth import ExternalVersion, Modrinth, safe_jar_name
from .plan_evidence import grouped
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
    inputs = {'roots': pack['main'], 'excluded': [int(pid) for pid, choice in pack['manual'].items()
                                               if choice['list'] == 'excluded'],
            'pins': state.get('file_pins', {}), 'external': state['external_projects'],
            'curseforge_only': curseforge_only, 'minecraft': f.minecraft,
            'loader': f.loader, 'release_types': sorted(f.release_types or ([1, 2, 3] if f.allow_prerelease else [1]))}


    # Leave historical input fingerprints stable for ordinary old revisions.
    for key in ('install_evidence', 'replacements'):
        if state.get(key):
            inputs[key] = state[key]
    return inputs


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
        'files_count': len(plan.get('items', plan.get('lock', {}).get('files', {}))),
        'progress': plan.get('progress'), 'manual_count': sum(x.get('acquisition') == 'manual' for x in plan.get('items', [])),
        'blockers_count': len(plan['blockers']), 'blockers': plan['blockers'][:5],
        'warnings_count': len(plan['warnings']), 'warnings': plan['warnings'][:5],
        'unchecked': plan['unchecked'], 'report_path': report_path,
    }


def schedule(engine, job_id, revision=None, requested_loader=None, curseforge_only=False, *,
             rollback=None, update_scope=None, update_apply=None, user_request=None, max_states=100_000,
             jar_alias=None):
    if not 1 <= max_states <= 2_000_000:
        raise ValueError('Search budget must be 1..2000000 states')
    if user_request and len(user_request) > 8000:
        raise ValueError('User request may contain up to 8000 characters')
    if jar_alias:
        engine.store.destination(jar_alias)
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
                                                 'update_scope': update_scope, 'update_apply': update_apply,
                                                 'user_request': user_request, 'max_states': max_states, 'jar_alias': jar_alias})
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
            'checked_at': time.time(), 'blockers': [], 'warnings': [], 'unchecked': unchecked_metadata(),
            'request': options.get('user_request'), 'items': [], 'status_model': 1}
    external = inputs['external'] if not inputs['curseforge_only'] else {}
    if not inputs['roots'] and not external:
        plan['blockers'].append({'code': 'empty_selection', 'message': 'Choose a nonempty main list'})
    external_specs = []
    for item in external.values():
        if item.get('selected') is False:
            continue
        if item.get('source') != 'modrinth' or not item.get('version_id'):
            plan['blockers'].append({'code': 'external_exact_file_required', 'projects': external_summary(external),
                                     'message': 'Record the exact Modrinth project and version IDs first'})
            continue
        external_specs.append(ExternalVersion.model_validate({
            key: item[key] for key in ExternalVersion.model_fields if key in item}).model_dump())
    if not inputs['roots'] and not external_specs and not plan['blockers']:
        plan['blockers'].append({'code': 'empty_selection', 'message': 'Choose a nonempty main list'})
    job = engine.store.get(options['job_id'])
    revision = at_revision(job, options['revision'])
    if track_curation:
        add_curation_notes(engine.store, plan, job, revision, projects)
    if plan['blockers']:
        return plan
    if hasattr(source, 'seed_projects'):
        source.seed_projects(projects)
    tracker = PreflightSource(engine, task_id, source, (options['job_id'], f"{options['revision']}:{task_id}:{plan['input_key']}"))
    try:
        rules = grouped(inputs.get('install_evidence', []))
        lock = await resolve(tracker, inputs['roots'], f, tracker.cancel,
                             pins={**options.get('temporary_pins', {}), **inputs['pins']}, excluded=inputs['excluded'],
                             max_states=options.get('max_states', 100_000), progress=tracker.progress, evidence=rules)
        plan['lock'] = lock
        tracker.progress(phase='loader_metadata', force=True)
        loader_start = time.monotonic()
        try:
            plan['loader'] = await loader_version(f, options['loader_version'])
        finally:
            elapsed = time.monotonic() - loader_start
            tracker.source_time += elapsed
            tracker.activity['loader_metadata_seconds'] = elapsed
        if external_specs:
            tracker.progress(phase='modrinth', force=True)
            async with httpx.AsyncClient(timeout=30, headers={'User-Agent': 'Modpack Builder'}) as client:
                provider = Modrinth(client)
                plan['external_items'] = await tracker.fetch('modrinth', lambda: provider.resolve(external_specs, f, tracker.cancel), meter=provider)
            for item in plan['external_items']:
                missing = [owner for owner in item['required_by']
                           if owner.startswith('curseforge:') and owner.split(':', 1)[1] not in lock['files']]
                if missing:
                    raise SourceError(f'EXTERNAL_OWNER_ABSENT: {item["project_id"]}: {missing}')
    except ResolutionError as exc:
        plan['blockers'].extend(exc.issues)
        return plan
    except (SourceError, ValueError, httpx.HTTPError) as exc:
        plan['blockers'].append({'code': 'metadata_unverified', 'message': str(exc)})
        return plan
    finally:
        tracker.progress(phase='candidate_search_complete', force=True)
        plan['progress'] = tracker.activity.copy()
    for pid, file in lock['files'].items():
        project = {**projects.get(pid, {}), **getattr(source, 'known_projects', {}).get(int(pid), {})}
        if project:
            plan.setdefault('projects', {})[pid] = {k: project[k] for k in ('id', 'name', 'slug', 'url') if k in project}
        if warning := distribution_warning(file, project):
            plan['warnings'].append(warning)
    attach_inventory(plan, revision, job)
    if options.get('jar_alias'):
        verify_local_evidence(engine.store.destination(options['jar_alias']), plan, f)
    plan['ready'] = not plan['blockers']
    return plan


def attach_inventory(plan, revision, job):
    requirements = {}
    for owner, file in plan['lock']['files'].items():
        for dep in file['dependencies']:
            if dep['relationType'] == 3:
                requirements.setdefault(dep['modId'], []).append(f'curseforge:{owner}')
    roots = set(plan['inputs']['roots'])
    for pid, file in plan['lock']['files'].items():
        project = plan.get('projects', {}).get(pid, {})
        manual = next((w for w in plan['warnings'] if w['code'] == 'manual_download' and w['project_id'] == int(pid)), {})
        reason = revision['pack']['manual'].get(pid, {}).get('reason')
        if not reason and revision['requirements_revision'] == job['requirements_revision']:
            reason = job['decisions'].get(pid, {}).get('explanation')
        url = manual.get('url') or (project['url'] + f'/files/{file["id"]}' if project.get('url') else
                                    f'https://www.curseforge.com/minecraft/mc-mods/projects/{pid}')
        hashes = { {1: 'sha1', 2: 'md5'}[x['algo']]: x['value'] for x in file.get('hashes', ())
                   if x.get('algo') in (1, 2) and x.get('value')}
        item = {'source': 'curseforge', 'project_id': int(pid), 'file_id': file['id'], 'file_name': file['fileName'],
                'version': file.get('versionNumber') or file.get('version'),
                'source_label': file.get('displayName'),
                'url': url, 'size': file.get('fileLength'), 'hashes': hashes, 'required_by': requirements.get(int(pid), []),
                'role': 'selected' if int(pid) in roots else 'dependency',
                'reason': ((reason or 'Selected in the saved revision; specific reason unavailable') if int(pid) in roots
                           else 'Required by exact selected file metadata'),
                'acquisition': 'manual' if manual else 'prism_curseforge',
                'evidence': {'source': file.get('_dependencies_source') or f'https://api.curseforge.com/v1/mods/{pid}/files/{file["id"]}',
                             'version': file['id'], 'modified': file.get('dateModified') or file.get('fileDate'),
                             'resolved_from': file.get('_metadata_source', 'CurseForge'),
                             'additional': file.get('_dependency_evidence', [])}}
        if item['evidence']['additional']:
            plan['warnings'].append({'code': 'supplemental_metadata', 'project_id': int(pid),
                                     'file_id': file['id'], 'evidence': item['evidence']['additional']})
        plan['items'].append(item)
    plan['items'].extend(plan.get('external_items', ()))
    for item in plan.get('external_items', ()):
        plan['warnings'].append({'code': item['acquisition'], 'source': 'modrinth',
                                 'project_id': item['project_id'], 'file_id': item['file_id'], 'url': item['url']})
    names = set()
    for item in plan['items']:
        try:
            name = safe_jar_name(item['file_name']).casefold()
        except ValueError as exc:
            plan['blockers'].append({'code': 'unsafe_file_name', 'item': item, 'message': str(exc)})
            continue
        if name in names:
            plan['blockers'].append({'code': 'file_name_collision', 'file': item['file_name']})
        names.add(name)
    plan['replacements'] = plan['inputs'].get('replacements', [])


def verify_local_evidence(folder, plan, f):
    from .verification import checked_artifact, file_digests, regular
    from .jar_validation import validate_pack
    paths = []
    for item in plan['items']:
        if any(b.get('code') == 'unsafe_file_name' for b in plan['blockers']):
            break
        path = folder / item['file_name']
        if not item['hashes'] or not regular(path):
            continue
        actual = file_digests(path, item['hashes'])
        if not checked_artifact(item, actual):
            plan['blockers'].append({'code': 'local_evidence_mismatch', 'file': item['file_name']})
            continue
        paths.append(path)
    report = validate_pack(paths, f, {f.loader.lower(): plan['loader']['version']})
    plan['jar_evidence'] = {'verified_files': [path.name for path in paths], **report}
    if len(paths) == len(plan['items']):
        plan['blockers'].extend({'code': 'jar_metadata_conflict', 'message': message}
                                for message in report['errors'] + report['unresolved'])
    else:
        plan['warnings'].append({'code': 'partial_jar_evidence', 'verified': len(paths),
                                 'total': len(plan['items']), 'unconfirmed': report['errors'] + report['unresolved']})


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
        'dependencies': sorted(file.get('_platform_dependencies', file.get('dependencies', [])), key=lambda x: json.dumps(x, sort_keys=True)),
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
        checked[key] = saved
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
    transient = any(b['code'] in ('source_unavailable', 'search_budget_exhausted') or
                    (b['code'] == 'metadata_unverified' and 'UNAVAILABLE' in b.get('message', '')) for b in plan['blockers'])
    engine.store.patch(task_id, check_summary=summary(plan, str(path)), check_report_path=str(path),
                       check_plan=plan, stage='pack_check' if transient else 'done',
                       status='completed' if plan['ready'] else 'blocked',
                       activity={**(plan.get('progress') or {}),
                                 'operation': 'metadata_ready' if plan['ready'] else 'requires_attention'})
