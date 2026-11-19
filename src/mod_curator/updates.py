"""Bounded update scans, proposal-only results and explicit validated application."""
from __future__ import annotations

import json

from .composition import Composition, at_revision, place, placement, sync_brief_constraints
from .models import SourceError
from .preflight import build_plan, latest, plan_key, save_with_revision, schedule, summary
from .resolver import compatible, declared_version


def mandatory(file):
    return [d for d in file.get('dependencies', []) if d['relationType'] == 3]


def file_description(file):
    return {'id': file['id'], 'version': declared_version(file),
            'label': file.get('displayName') or file.get('fileName'), 'release_type': file.get('releaseType'),
            'date': file.get('fileDate'), 'required_dependencies': mandatory(file),
            'source': file.get('_dependencies_source') or file.get('_metadata_source')}


async def scan_catalogue(source, f, pages, known, check_cancel):
    candidates, seen, errors = {}, set(), []
    total, visited, last, count, ended, disorder, shift = None, 0, None, 0, False, False, False
    # Download-sorted pages cannot establish the absence of new low-ranked projects.
    for number in range(1, pages + 1):
        check_cancel()
        try:
            page = await source.page(f, number)
        except SourceError as exc:
            errors.append({'page': number, 'message': str(exc)})
            break
        if any(type(c.get('downloads')) is not int for c in page.cards):
            errors.append({'page': number, 'message': 'Unknown exact download count'})
            break
        visited += 1
        count += len(page.cards)
        if total is not None and total != page.total:
            shift = True
        total = page.total
        for card in page.cards:
            pid, downloads = str(card['id']), card['downloads']
            if pid in seen:
                shift = True
            seen.add(pid)
            disorder = disorder or (last is not None and last < downloads)
            last = downloads
            if pid not in known and downloads > f.minimum_downloads:
                candidates[pid] = card
        if not page.has_next:
            ended = True
            break
    complete = ended and not shift and not errors and total == len(seen)
    return candidates, {'page_limit': pages, 'pages_checked': visited, 'cards_checked': count,
                        'unique_cards': len(seen), 'total_reported_by_site': total, 'catalogue_end': ended,
                        'complete_for_filters': complete, 'sort_violation': disorder,
                        'pagination_shift_or_duplicates': shift, 'errors': errors,
                        'strict_download_cutoff': f.minimum_downloads, 'sort': 'total downloads descending',
                        'scope': 'New to the saved catalogue within observed pages; publication date is not inferred.'}


async def check_files(engine, task_id, source, f, previous, inputs, selected_ids=None):
    checked, proposals, errors, unavailable = 0, {}, [], []
    # Include transitive mandatory dependencies, even if they are absent from the main list.
    files = previous['lock']['files'] if previous else {}
    requested = set(selected_ids) if selected_ids is not None else set(map(int, files))
    for pid, before in files.items():
        if int(pid) not in requested:
            continue
        engine.check_cancel(task_id)
        engine.store.patch(task_id, activity={'operation': 'checking_file_updates', 'project_id': int(pid), 'checked': checked})
        try:
            options = sorted((v for v in await source.files(int(pid), f) if compatible(v, int(pid), f)),
                             key=lambda v: (v.get('fileDate', ''), v['id']), reverse=True)
            old = next((v for v in options if v['id'] == before['id']), None)
            if old is None:
                unavailable.append({'project_id': int(pid), 'file_id': before['id'],
                                    'message': 'Exact file absent from the verified compatible file listing'})
            if options:
                new = options[0]
                if new['id'] != before['id'] and (not old or new.get('fileDate', '') > old.get('fileDate', '')):
                    new = await source.complete_file(new) if not new.get('_details_complete', True) else new
                    if not compatible(new, int(pid), f) or 'dependencies' not in new:
                        raise SourceError('New file metadata incomplete or changed')
                    first, second = file_description(before), file_description(new)
                    before_deps, after_deps = mandatory(before), mandatory(new)
                    proposals[pid] = {'project_id': int(pid), 'current': first, 'candidate': second,
                                      'reason': 'Available under the saved game, loader and release filters',
                                      'current_file_available': old is not None,
                                      'file_pin': inputs['pins'].get(pid),
                                      'dependencies_added_or_changed': [d for d in after_deps if d not in before_deps],
                                      'dependencies_removed_or_changed': [d for d in before_deps if d not in after_deps],
                                      'whole_pack_verified': False}
            checked += 1
        except SourceError as exc:
            errors.append({'project_id': int(pid), 'message': str(exc)})
    return proposals, unavailable, {'ready_plan_revision': previous['revision'] if previous else None,
                                    'projects_requested': len(requested), 'projects_checked': checked, 'errors': errors,
                                    'status': 'checked' if previous else 'requires_ready_plan'}


async def preview(engine, task_id, source, task, f, options):
    store = engine.store
    job = store.get(options['job_id'])
    scope, inputs = options['update_scope'], options['inputs']
    previous = latest(store, options['job_id'], options['revision'], inputs['curseforge_only'], ready_only=True)
    if hasattr(source, 'seed_projects'):
        source.seed_projects(task['projects'])
    known = set(job['projects'])
    known.update(str(pid) for pid in (*inputs['roots'], *inputs['excluded']))
    if previous:
        known.update(previous['lock']['files'])
    engine.store.patch(task_id, activity={'operation': 'checking_catalogue_updates'})
    cards, catalogue = await scan_catalogue(source, f, scope['catalogue_pages'], known,
                                             lambda: engine.check_cancel(task_id))
    files, unavailable, coverage = await check_files(engine, task_id, source, f, previous, inputs, scope['project_ids']) \
        if scope['include_files'] else ({}, [], {'status': 'not_requested'})
    report = {'source_job_id': options['job_id'], 'revision': options['revision'], 'task_id': task_id,
              'input_key': plan_key(inputs), 'baseline_plan_task': previous['task_id'] if previous else None,
              'filters': f.model_dump(exclude={'wishes', 'destination'}),
              'catalogue_coverage': catalogue, 'file_coverage': coverage,
              'new_projects': cards, 'file_updates': files, 'old_files_unavailable': unavailable}
    path = store.job_dir(task_id) / 'update-proposals.json'
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    store.patch(task_id, update_proposals=report, check_report_path=str(path), stage='done', status='completed',
                activity={'operation': 'proposals_ready'})


def propose(engine, job_id, revision, catalogue_pages, include_files, project_ids, curseforge_only):
    if not 0 <= catalogue_pages <= 30 or not (catalogue_pages or include_files):
        raise ValueError('Specify 0–30 catalogue pages and/or file checks')
    if project_ids and not include_files:
        raise ValueError('Project scope applies to file checks')
    if project_ids:
        job = engine.store.get(job_id)
        rev = revision if revision is not None else job['selection_revision']
        plan = latest(engine.store, job_id, rev, curseforge_only, ready_only=True)
        if not plan or not set(map(str, project_ids)) <= plan['lock']['files'].keys():
            raise ValueError('Specify known project IDs from the ready installation plan')
    return schedule(engine, job_id, revision, curseforge_only=curseforge_only,
                    update_scope={'catalogue_pages': catalogue_pages, 'include_files': include_files,
                                  'project_ids': project_ids})


def report(store, task_id, offset, limit):
    from .composition import validate_page
    validate_page(offset, limit)
    task = store.get(task_id)
    result = task.get('update_proposals')
    if not task.get('check_options'):
        raise ValueError('Expected a pack check task ID')
    if not result:
        return {'task_id': task_id, 'status': task['status'], 'activity': task.get('activity'),
                'latest_errors': task['errors'][-3:]}
    projects = [{k: p[k] for k in ('id', 'name', 'url', 'downloads') if k in p} |
                {'reason': 'Observed in the requested filters and strictly above the cutoff',
                 'concept_fit': 'not_reviewed', 'files_and_dependencies': 'not_checked'} for p in result['new_projects'].values()]
    changes = ([{'kind': 'file_update', **f} for f in result['file_updates'].values()] +
               [{'kind': 'file_unavailable', **f} for f in result['old_files_unavailable']] +
               [{'kind': 'new_project', **p} for p in projects])
    headers = {k: v for k, v in result.items() if k not in ('new_projects', 'file_updates', 'old_files_unavailable')}
    for key in ('catalogue_coverage', 'file_coverage'):
        coverage = headers[key]
        if 'errors' in coverage:
            headers[key] = {**coverage, 'error_count': len(coverage['errors']), 'errors': coverage['errors'][:3]}
    return {**headers, 'status': task['status'], 'report_path': task['check_report_path'],
            'total': len(changes), 'offset': offset, 'items': changes[offset:offset + limit]}


def inspect_candidates(engine, job_id, revision, task_id, project_ids):
    from .economy import Economy
    store = engine.store
    evidence = store.get(task_id).get('update_proposals')
    if not evidence or (evidence['source_job_id'], evidence['revision']) != (job_id, revision):
        raise ValueError('Proposal belongs to a different job or revision')
    job = store.get(job_id)
    if job['selection_revision'] != revision:
        raise ValueError('Stale update proposal')
    settings = Economy(store).settings(job_id)['settings']
    ids = list(dict.fromkeys(project_ids))
    if not 1 <= len(ids) <= settings['projects_per_batch']:
        raise ValueError('Choose a nonempty candidate scope within the saved project batch limit')
    new = evidence['new_projects']
    eligible = new.keys() | (evidence['file_updates'].keys() & job['projects'].keys())
    if not set(map(str, ids)) <= eligible:
        raise ValueError('Choose only new catalogue projects or saved projects with a proposed file update')
    catalogue = {**job['projects'], **new}
    source = store.get(task_id)
    review_id = store.create(source['filters'], stage='describe', order=ids,
                             projects={str(pid): {k: catalogue[str(pid)][k] for k in ('id', 'name', 'slug', 'url', 'downloads',
                                       'metadata_source') if k in catalogue[str(pid)]} for pid in ids},
                             candidate_review={'job_id': job_id, 'revision': revision, 'proposal_id': task_id},
                             describe_only_ids=ids)
    # A targeted review inherits worker model choices and all the parent limits.
    from .economy import SettingsPatch
    allowed = SettingsPatch.model_fields.keys()
    Economy(store).settings(review_id, SettingsPatch.model_validate({k: v for k, v in settings.items() if k in allowed}))
    return {**engine.launch(review_id), 'parent_job_id': job_id, 'proposal_id': task_id,
            'candidate_count': len(ids), 'next': 'Review saved descriptions or reuse fact extraction for this candidate job only'}


def apply_proposals(engine, job_id, revision, task_id, project_ids, file_ids, override_file_pins=False,
                    candidate_review_job_id=None):
    if not project_ids and not file_ids:
        raise ValueError('Select project IDs and/or exact proposed file IDs explicitly')
    store = engine.store
    task = store.get(task_id)
    report_data = task.get('update_proposals')
    if not report_data or report_data['source_job_id'] != job_id or report_data['revision'] != revision:
        raise ValueError('Proposal belongs to a different job or revision')
    job = store.get(job_id)
    if job['selection_revision'] != revision:
        raise ValueError('Stale update proposal')
    inputs = task['check_options']['inputs']
    from .preflight import plan_input
    if plan_key(plan_input(job, revision, inputs['curseforge_only'])) != report_data['input_key']:
        raise ValueError('Update proposal no longer matches the composition')
    previous = latest(store, job_id, revision, inputs['curseforge_only'], ready_only=True)
    if file_ids and (not previous or previous['task_id'] != report_data['baseline_plan_task']):
        raise ValueError('A different installation plan is now the baseline. Check updates again.')
    new = report_data['new_projects']
    pack = at_revision(job, revision)['pack']
    if not set(map(str, project_ids)) <= new.keys():
        raise ValueError('Choose only new project IDs from this proposal')
    if set(project_ids) & set(map(int, pack['manual'])):
        raise ValueError('A manual placement can only be overridden via explicit pack_edit')
    updates, skipped = {}, []
    for pid, fid in file_ids.items():
        match = report_data['file_updates'].get(pid, {})
        if match.get('candidate', {}).get('id') != fid:
            raise ValueError('Choose only exact file IDs from this proposal')
        if pid in inputs['pins'] and not override_file_pins:
            skipped.append({'project_id': int(pid), 'reason': 'manual_file_pin'})
        else:
            updates[pid] = fid
    if not project_ids and not updates:
        return {'changed': False, 'revision': revision, 'skipped': skipped}
    review_projects = {}
    if candidate_review_job_id:
        reviewed = store.get(candidate_review_job_id)
        if reviewed.get('candidate_review') != {'job_id': job_id, 'revision': revision, 'proposal_id': task_id}:
            raise ValueError('Candidate review must belong to the same saved proposal')
        engine.idle(reviewed)
        review_projects = reviewed['projects']
    fixed_roots = {pid: file['id'] for pid, file in (previous or {}).get('lock', {}).get('files', {}).items()
                   if int(pid) in inputs['roots'] and pid not in updates}
    change = {'projects': {str(pid): review_projects.get(str(pid), new[str(pid)]) for pid in project_ids}, 'updates': updates,
              'reviewed_files': {pid: p for pid, p in review_projects.items() if pid in updates and p.get('description')},
              'skipped': skipped, 'fixed_roots': fixed_roots, 'proposal_task': task_id}
    return schedule(engine, job_id, revision, previous['loader']['version'] if previous else None,
                    inputs['curseforge_only'], update_apply=change)


async def run(engine, task_id, source, task, f, options):
    if options['update_scope'] is not None:
        await preview(engine, task_id, source, task, f, options)
        return
    change = options['update_apply']
    task['projects'].update(change['projects'])
    pids = list(map(int, change['projects']))
    inputs = options['inputs']
    inputs['roots'] = list(dict.fromkeys(inputs['roots'] + pids))
    inputs['pins'] = {**inputs['pins'], **change['updates']}
    options['temporary_pins'] = change['fixed_roots']
    plan = await build_plan(engine, task_id, source, options, f, task['projects'])
    dropped = change['updates'].keys() - plan.get('lock', {}).get('files', {}).keys()
    if plan['ready'] and dropped:
        plan['blockers'].append({'code': 'projects_no_longer_required', 'project_ids': sorted(map(int, dropped)),
                                 'message': 'Some requested files would no longer be part of the required dependency closure'})
        plan['ready'] = False
    if plan['ready']:
        def apply(job, pack):
            job['file_pins'] = inputs['pins']
            changed_descriptions = []
            for key, project in change.get('reviewed_files', {}).items():
                saved = job['projects'][key]
                if project.get('description_sha256') != saved.get('description_sha256'):
                    saved.update({k: v for k, v in project.items() if k.startswith('description') or k == 'metadata'})
                    changed_descriptions.append(int(key))
            if changed_descriptions:
                job['recheck_ids'] = sorted(set(job.get('recheck_ids', [])) | set(changed_descriptions))
            for pid in pids:
                key = str(pid)
                if placement(pack, pid) or key in pack['manual']:
                    raise ValueError('Placement changed after update preview')
                job['projects'].setdefault(key, change['projects'][key])
                place(pack, pid, 'main', None, 'Обновление явно выбрано пользователем', True)
            sync_brief_constraints(job, pack)
            return {'updated_files': change['updates'], 'proposal_task': change['proposal_task'],
                    'changed_description_ids': changed_descriptions,
                    'skipped': change['skipped'], 'new_descriptions': [pid for pid in pids
                        if not job['projects'][str(pid)].get('description')]}
        result = Composition(engine)._commit(options['job_id'], options['revision'], 'apply_updates', apply,
                                              after_commit=save_with_revision(plan))
        if result.get('new_descriptions'):
            engine.launch(options['job_id'])
    path = engine.store.job_dir(task_id) / 'installation-plan.json'
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding='utf-8')
    engine.store.patch(task_id, stage='done', status='completed' if plan['ready'] else 'blocked', check_plan=plan,
                       check_summary=summary(plan, str(path)), check_report_path=str(path))
