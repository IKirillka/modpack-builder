"""Revisioned main/reserve/exclusion lists and constrained edit commands."""
from __future__ import annotations

from copy import deepcopy
import csv
import os
import re
import tempfile
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .brief import filter_plan, legacy_brief, merge_brief, validate_filters
from .models import Filters


class Relationship(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    first: int = Field(gt=0)
    second: int = Field(gt=0)
    kind: Literal['substitute', 'partial_overlap', 'complement', 'incompatibility', 'insufficient_data']
    reason: str = Field(min_length=1, max_length=500)
    unique_first: str | None = None
    unique_second: str | None = None
    source: str | None = None
    versions: str | None = None
    verification: Literal['reported', 'file_metadata', 'reproduced', 'unverified'] = 'unverified'


class PackChange(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    project_id: int = Field(gt=0)
    action: Literal['keep', 'add', 'remove', 'restore', 'move']
    target: Literal['main', 'reserve'] | None = None
    reason: str = Field(default='', max_length=500)
    basis: Literal['supported', 'insufficient_data'] = 'supported'


def pack_state(job):
    if 'composition' in job:
        return deepcopy(job['composition'])
    main = list(job['selection'])
    reserve = list(job.get('legacy_reserve', []))
    # Old free-form notes cannot prove which moves were manual. Protect the latest
    # known placement of every historically edited ID conservatively.
    touched = set()
    for entry in job['history']:
        if entry['note'] != 'proposal':
            diff = entry.get('diff', {})
            touched.update(diff.get('added', []) + diff.get('removed', []))
    manual = deepcopy(job.get('legacy_manual', {}))
    excluded = deepcopy(job.get('legacy_excluded', {}))
    for pid in sorted(touched):
        place = 'main' if pid in main else 'reserve' if pid in reserve else 'excluded'
        manual.setdefault(str(pid), {'list': place, 'reason': 'Сохранённая правка старой ревизии'})
        if place == 'excluded':
            excluded.setdefault(str(pid), {'from': 'main', 'reason': manual[str(pid)]['reason']})
            if excluded[str(pid)]['from'] is None:
                excluded[str(pid)]['from'] = 'main'
    return {'main': main, 'reserve': reserve, 'excluded': excluded, 'manual': manual}


def placement(pack, pid):
    for name in ('main', 'reserve'):
        if pid in pack[name]:
            return name
    return 'excluded' if str(pid) in pack['excluded'] else None


def enforce_main(job, ids):
    """Compatibility guard for old proposal/revision entry points."""
    pack = pack_state(job)
    if any((int(pid) in ids) != (entry['list'] == 'main') for pid, entry in pack['manual'].items()):
        raise ValueError('This change overrides a saved placement. Use an explicit pack_edit command.')
    if set(ids) & set(pack['reserve']):
        raise ValueError('Use pack_edit to move projects out of reserve explicitly.')


def blocked_dependencies(lock, roots, excluded):
    if not lock:
        return {'status': 'not_checked', 'affected': []}
    files = lock.get('files', {})
    affected = []
    unchecked = []
    for root in roots:
        todo, seen = [root], set()
        while todo:
            pid = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            file = files.get(str(pid))
            if not file:
                unchecked.append(root)
                continue
            for dep in file.get('dependencies', []):
                if dep['relationType'] == 3:
                    target = dep['modId']
                    if target in excluded:
                        item = {'root': root, 'dependent': pid, 'required': target, 'file_id': file['id']}
                        if item not in affected:
                            affected.append(item)
                    todo.append(target)
    return {'status': 'cached_exact_files', 'affected': affected, 'unchecked_roots': sorted(set(unchecked))}


def snapshot(job, pack):
    return {'revision': job['selection_revision'], 'pack': deepcopy(pack),
            'brief': deepcopy(job.get('brief') or legacy_brief(job)),
            'filters': deepcopy(job['filters']), 'recheck_ids': list(job.get('recheck_ids', [])),
            'requirements_revision': job['requirements_revision'],
            'overlap_review': job.get('overlap_review'), 'external_projects': deepcopy(job.get('external_projects', {})),
            'file_pins': deepcopy(job.get('file_pins', {})),
            'install_evidence': deepcopy(job.get('install_evidence', [])),
            'replacements': deepcopy(job.get('replacements', [])), 'relationships': deepcopy(job.get('relationships', []))}


class Composition:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def info(self, job_id):
        job = self.store.get(job_id, opened=True)
        pack = pack_state(job)
        return {'job_id': job_id, 'brief': job.get('brief') or legacy_brief(job),
                'revision': job['selection_revision'], 'main': len(pack['main']), 'reserve': len(pack['reserve']),
                'excluded': len(pack['excluded']), 'manual': len(pack['manual']),
                'recheck_count': len(job.get('recheck_ids', [])), 'recheck_scope_unspecified': job.get('recheck_scope_unspecified', False), 'status': job['status'], 'stage': job['stage'],
                'progress': {'cards': job['processed'], 'projects': len(job['projects']), 'decisions': len(job['decisions'])},
                'search_scopes': job.get('search_scopes', []), 'search_scope_results': job.get('search_scope_results', {}),
                'related_reports': job.get('legacy_reports', {}), 'external_projects': external_summary(job.get('external_projects', {}))}

    def read(self, job_id, group='main', offset=0, limit=30, query=None):
        validate_page(offset, limit)
        job = self.store.get(job_id, opened=True)
        pack = pack_state(job)
        ids = [int(pid) for pid in (job['projects'] if group == 'catalogue' else pack[group])]
        if query:
            wanted = normalized(query)
            ids = [pid for pid in ids if wanted in normalized(job['projects'].get(str(pid), {}).get('name', ''))
                   or wanted == normalized(job['projects'].get(str(pid), {}).get('slug', '')) or query == str(pid)]
            exact = [pid for pid in ids if query == str(pid) or wanted in {normalized(job['projects'].get(str(pid), {}).get(k, '')) for k in ('name', 'slug')}]
            ids = exact or ids
        records = []
        for pid in ids[offset:offset+limit]:
            project = job['projects'].get(str(pid), {})
            records.append({'project_id': pid, **{key: project[key] for key in ('name', 'downloads', 'url') if key in project},
                            'list': placement(pack, pid), 'manual': pack['manual'].get(str(pid)),
                            'exclusion': pack['excluded'].get(str(pid)), 'recheck_requested': pid in job.get('recheck_ids', [])})
        return {'revision': job['selection_revision'], 'total': len(ids), 'items': records}

    def review(self, job_id, relationships):
        job = self.store.get(job_id)
        pack = pack_state(job)
        known = set(map(int, job['projects']))
        result = []
        pairs = set()
        for rel in relationships:
            pair = tuple(sorted((rel.first, rel.second)))
            if rel.first == rel.second or not set(pair) <= known or pair in pairs:
                raise ValueError('Relationships require two distinct known IDs and one entry per pair')
            pairs.add(pair)
            if rel.kind == 'partial_overlap' and not (rel.unique_first and rel.unique_second):
                raise ValueError('Describe the distinct mechanics of both partially overlapping projects')
            if rel.kind == 'incompatibility' and not rel.source:
                raise ValueError('A reported incompatibility needs an attributable source')
            row = rel.model_dump(exclude_none=True)
            row['first_list'], row['second_list'] = placement(pack, rel.first), placement(pack, rel.second)
            if rel.kind == 'substitute':
                first, second = (job['projects'][str(pid)].get('downloads') for pid in (rel.first, rel.second))
                row['downloads'] = {rel.first: first, rel.second: second}
                row['preferred_by_downloads'] = (rel.first if first > second else rel.second) if type(first) is int and type(second) is int and first != second else None
                for pid in (rel.first, rel.second):
                    if placement(pack, pid) == 'main' and str(pid) in pack['manual']:
                        row.setdefault('preserve_manual', []).append(pid)
            result.append(row)
        return result

    def update_brief(self, job_id, patch, expected_revision, affected_ids=None):
        job = self.store.get(job_id)
        changed = patch.model_dump(exclude_unset=True)
        old_brief = job.get('brief') or legacy_brief(job)
        brief = merge_brief(old_brief, changed)
        if not brief.get('concept'):
            raise ValueError('A saved brief needs a concept')
        filters = validate_filters(brief)
        plan = filter_plan(job, filters)
        if (old_brief.get('search_scopes') != brief.get('search_scopes') or
                (job.get('search_scopes') and plan['mode'] != 'reuse_existing')):
            plan = {'mode': 'new_query', 'changed': plan['changed'],
                    'search_scopes_before': old_brief.get('search_scopes'),
                    'search_scopes_after': brief.get('search_scopes'),
                    'reason': 'Individual search scopes have their own filters and thresholds. Keep existing work and start a linked brief for changed coverage.'}
        if plan['mode'] != 'reuse_existing':
            return {'job_id': job_id, 'revision': job['selection_revision'], 'saved': False,
                    'catalogue_plan': plan, 'proposed_brief': brief,
                    'next': 'Explain the scope. Keep the current job; start a linked draft only when requested.'}
        if 'destination' in (changed.get('filters') or {}):
            self.store.destination(filters.destination)
        if set(changed) & {'required_ids', 'excluded_ids'}:
            raise ValueError('Change project-specific requirements with explicit pack_edit commands')
        requirements_changed = wishes_changed(old_brief, brief)
        catalogue_filters = job['filters']
        # Keep the captured-page configuration until a different scan is run.
        if 'page_size' in plan['changed']:
            filters.page_size = catalogue_filters['page_size']
            brief['filters']['page_size'] = catalogue_filters['page_size']
            plan['requested_future_page_size'] = plan['changed']['page_size']['to']
        def apply(current, pack):
            if requirements_changed:
                if affected_ids is not None and not set(map(str, affected_ids)) <= current['projects'].keys():
                    raise ValueError('Unknown affected project ID')
                from .refinement import changed_criteria, tracked_rechecks
                criteria = changed_criteria(old_brief, brief)
                tracked, untracked = tracked_rechecks(current, criteria)
                current['requirements_revision'] += 1
                current['recheck_ids'] = sorted(set(current.get('recheck_ids', [])) | set(tracked) | set(affected_ids or []))
                new_criteria = {key for key in criteria if '.' in key and
                                key.split('.', 1)[1] not in (old_brief.get(key.split('.', 1)[0]) or {})}
                scope_unclear = untracked > 0 or bool(new_criteria) or 'concept' in criteria
                current['recheck_scope_unspecified'] = affected_ids is None and scope_unclear
                current['last_recheck_plan'] = {'changed_criteria': sorted(criteria), 'matched_decisions': len(tracked),
                                                'decisions_without_tags': untracked, 'explicit_ids': len(affected_ids or [])}
            current['brief'] = brief
            current['filters'] = filters.model_dump()
            return {'catalogue_plan': plan, 'requirements_revision': current['requirements_revision'],
                    'scope_needs_review': current.get('recheck_scope_unspecified', False),
                    'recheck_plan': current.get('last_recheck_plan') if requirements_changed else None}
        return self._commit(job_id, expected_revision, 'brief', apply)

    async def manual(self, job_id, expected_revision, changes, group=None):
        job = self.store.get(job_id)
        self.engine.idle(job)
        if job['selection_revision'] != expected_revision:
            raise ValueError('Stale composition revision')
        unknown = [change.project_id for change in changes if change.action == 'add'
                   and str(change.project_id) not in job['projects']]
        found, errors = {}, []
        if unknown:
            from .source import CurseForge
            source = CurseForge(self.store.config(), self.store.job_dir(job_id))
            try:
                for pid in dict.fromkeys(unknown):
                    try:
                        project = await source.project(pid)
                        if project['id'] != pid:
                            raise ValueError('Source returned a different project ID')
                        found[str(pid)] = project
                    except Exception as exc:
                        errors.append({'id': pid, 'reason': str(exc)[:250]})
            finally:
                await source.close()
        result = self.apply(job_id, expected_revision, changes, group=group, new_projects=found)
        if errors:
            result['lookup_errors'] = errors
        return result

    def apply(self, job_id, expected_revision, changes, *, mode='manual', group=None, above=None, relationships=(), new_projects=None):
        comparisons = self.review(job_id, relationships) if mode != 'manual' and relationships else []
        if mode == 'reserve_popularity':
            # A candidate must have a direct, full substitute that remains in reserve.
            losers = {}
            for row in comparisons:
                winner = row.get('preferred_by_downloads')
                if winner and row['first_list'] == row['second_list'] == 'reserve':
                    loser = row['second'] if winner == row['first'] else row['first']
                    losers.setdefault(loser, []).append((winner, row['reason']))
            pack = pack_state(self.store.get(job_id))
            pinned_main = {pid for pid in pack['main'] if str(pid) in pack['manual']}
            protected_addons = {row['first'] for row in comparisons if row['kind'] == 'complement' and row['second'] in pinned_main}
            protected_addons |= {row['second'] for row in comparisons if row['kind'] == 'complement' and row['first'] in pinned_main}
            for pid in protected_addons:
                losers.pop(pid, None)
            kept = set(pack['reserve']) - losers.keys() | {int(pid) for pid in pack['manual']}
            changes = [PackChange(project_id=pid, action='remove', reason=f'Полная замена {winner}: {reason[:420]}')
                       for pid, alternatives in losers.items() for winner, reason in alternatives if winner in kept]
            changes = list({change.project_id: change for change in changes}.values())
            group = 'reserve'
        if mode == 'prune' and (above is None or any(change.action != 'remove' for change in changes)):
            raise ValueError('Pruning accepts only removals and requires a strict downloads threshold')
        if mode != 'manual' and any(change.action == 'remove' and not change.reason for change in changes):
            raise ValueError('Give a concrete concept-specific reason for every suggested removal')
        if len({change.project_id for change in changes}) != len(changes):
            raise ValueError('Use one edit per project per revision')

        def apply(job, pack):
            pinned = {int(pid) for pid, entry in pack['manual'].items() if entry['list'] == 'main'}
            complements = {row['first'] for row in comparisons if row['kind'] == 'complement' and row['second'] in pinned}
            complements |= {row['second'] for row in comparisons if row['kind'] == 'complement' and row['first'] in pinned}
            skipped = []
            added_for_description = []
            for change in changes:
                pid = change.project_id
                key = str(pid)
                project = job['projects'].get(key) or (new_projects or {}).get(key)
                old = placement(pack, pid)
                if not project and old is None:
                    skipped.append({'id': pid, 'reason': 'unknown_project'})
                    continue
                if group and old != group:
                    skipped.append({'id': pid, 'reason': 'outside_requested_list'})
                    continue
                downloads = (project or {}).get('downloads')
                if above is not None and (type(downloads) is not int or downloads <= above):
                    skipped.append({'id': pid, 'reason': 'not_strictly_above_threshold'})
                    continue
                if mode != 'manual' and (key in pack['manual'] or change.basis == 'insufficient_data'):
                    skipped.append({'id': pid, 'reason': 'manual_choice' if key in pack['manual'] else 'insufficient_data'})
                    continue
                target = target_list(pack, change, old)
                if mode != 'manual' and target != old:
                    if pid in complements and old in ('main', 'reserve'):
                        skipped.append({'id': pid, 'reason': 'complement_to_manual_basis'})
                        continue
                    if target in ('main', 'reserve') and not job['decisions'].get(key, {}).get('include'):
                        skipped.append({'id': pid, 'reason': 'no_positive_concept_decision'})
                        continue
                if not target:
                    skipped.append({'id': pid, 'reason': 'no_previous_list_choose_target'})
                    continue
                if mode != 'manual' and (change.action == 'remove' and (not project or not project.get('description'))):
                    skipped.append({'id': pid, 'reason': 'insufficient_data'})
                    continue
                place(pack, pid, target, old, change.reason or 'Ручная правка', mode == 'manual')
                if project and key not in job['projects']:
                    job['projects'][key] = project
                    added_for_description.append(pid)
            if mode == 'manual':
                sync_brief_constraints(job, pack)
            return {'skipped': skipped[:15], 'skipped_count': len(skipped), 'new_descriptions': added_for_description}
        result = self._commit(job_id, expected_revision, mode, apply)
        if result.get('new_descriptions'):
            result['worker'] = self.engine.launch(job_id)
        return result

    def plan_sources(self, job_id, expected_revision, *, external=None, evidence=None, replacements=None):
        """Set explicitly provided, version-scoped input sources as a revision."""
        def apply(job, pack):
            if external is not None:
                job['external_projects'] = {
                    item.project_id: {**item.model_dump(), 'source': 'modrinth', 'name': item.project_id,
                                      'url': f'https://modrinth.com/mod/{item.project_id}', 'selected': True}
                    for item in external}
                if len(job['external_projects']) != len(external):
                    raise ValueError('Duplicate external project')
            if evidence is not None:
                job['install_evidence'] = [item.model_dump() for item in evidence]
            if replacements is not None:
                for item in replacements:
                    if item.previous_project in pack['main'] or item.replacement_project not in pack['main']:
                        raise ValueError('Record a replacement after updating the composition')
                job['replacements'] = [item.model_dump() for item in replacements]
            return {}
        return self._commit(job_id, expected_revision, 'plan_sources', apply)

    def undo(self, job_id, expected_revision):
        def apply(job, pack):
            undone = {entry.get('undoes') for entry in job.get('pack_revisions', []) if entry['kind'] == 'undo'}
            entry = next((entry for entry in reversed(job.get('pack_revisions', []))
                          if entry['kind'] != 'undo' and entry['after']['revision'] not in undone), None)
            if not entry or entry['after']['revision'] != expected_revision and pack != entry['after']['pack']:
                raise ValueError('No previous managed edit to undo at this revision')
            before = entry['before']
            job['file_pins'] = deepcopy(before.get('file_pins', {}))
            for field, default in (('external_projects', {}), ('install_evidence', []), ('replacements', [])):
                job[field] = deepcopy(before.get(field, default))
            job['relationships'] = deepcopy(before.get('relationships', []))
            pack.clear()
            pack.update(deepcopy(before['pack']))
            if job['filters'] != before['filters'] or job.get('brief') != before['brief']:
                job['brief'] = before['brief']
                job['filters'] = before['filters']
                if job['requirements_revision'] != before['requirements_revision']:
                    job['requirements_revision'] += 1
                    job['recheck_ids'] = list(before['recheck_ids'])
                    job['recheck_scope_unspecified'] = True
            return {'undoes': entry['after']['revision']}
        return self._commit(job_id, expected_revision, 'undo', apply)

    def _commit(self, job_id, expected_revision, kind, apply, *, after_commit=None):
        def edit(job):
            self.engine.idle(job)
            if job['selection_revision'] != expected_revision:
                raise ValueError('Stale composition revision')
            pack = pack_state(job)
            before = snapshot(job, pack)
            result = apply(job, pack)
            if snapshot(job, pack) == before:
                return {'revision': expected_revision, 'changed': False, 'main': len(pack['main']), 'reserve': len(pack['reserve']), **result}
            dependency_lock = job.get('lock') or job.get('previous_dependency_lock')
            if job.get('lock'):
                job['previous_dependency_lock'] = job['lock']
            job['composition'] = pack
            self.engine._selection(job, pack['main'], f'pack_edit:{kind}')
            job.update(stage='awaiting_analysis', status='awaiting_analysis', overlap_review=None)
            if result.get('new_descriptions'):
                job.update(stage='describe', status='queued', describe_only_ids=result['new_descriptions'], cancel_requested=False)
            after = snapshot(job, pack)
            job.setdefault('pack_revisions', []).append({'before': before, 'after': after, 'kind': kind,
                                                        'at': time.time(), 'undoes': result.get('undoes')})
            diff = changes_between(before['pack'], pack)
            for change in diff:
                if change['to'] == 'excluded':
                    change['reason'] = pack['excluded'][str(change['id'])]['reason']
            return {'revision': after['revision'], 'changed': True, 'main': len(pack['main']), 'reserve': len(pack['reserve']),
                    'change_count': len(diff), 'changes': diff[:20],
                    'dependencies': blocked_dependencies(dependency_lock, pack['main'], set(map(int, pack['excluded']))), **result}
        result = self.store.mutate(job_id, edit, after=after_commit)
        if result['changed']:
            result['table'] = self.write_table(job_id, result['revision'])
        return {'job_id': job_id, **result}

    def write_table(self, job_id, revision):
        job = self.store.get(job_id)
        pack = at_revision(job, revision)['pack']
        path = self.store.job_dir(job_id) / f'composition-r{revision}.csv'
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent, delete=False) as out:
            writer = csv.writer(out)
            writer.writerow(['list', 'project_id', 'name', 'url', 'downloads', 'manual', 'reason'])
            for group in ('main', 'reserve', 'excluded'):
                for pid in pack[group]:
                    project = job['projects'].get(str(pid), {})
                    manual = pack['manual'].get(str(pid), {})
                    reason = pack['excluded'].get(str(pid), {}).get('reason', manual.get('reason', ''))
                    writer.writerow([group, pid, project.get('name', ''), project.get('url', ''), project.get('downloads', ''), bool(manual), reason])
        os.replace(out.name, path)
        return str(path)

    def export_revision(self, job_id, revision, name, loader_version=None, curseforge_only=False):
        job = self.store.get(job_id)
        selected = at_revision(job, revision)
        pack = selected['pack']
        if not pack['main'] and (curseforge_only or not selected['external_projects']):
            raise ValueError('Choose a nonempty main list')
        from .engine import fingerprint
        from .preflight import filters_for, latest, plan_input, plan_key
        filters = filters_for(job, revision)
        plan = latest(self.store, job_id, revision, curseforge_only)
        if not plan and (selected.get('install_evidence') or (selected['external_projects'] and not curseforge_only)):
            raise ValueError("Run preflight before exporting external files or dependency evidence.")
        if plan:
            if not plan['ready'] or plan['input_key'] != plan_key(plan_input(job, revision, curseforge_only)):
                raise ValueError('The saved installation plan is blocked or stale. Run preflight again.')
            if loader_version and loader_version != plan['loader']['version']:
                raise ValueError('Loader differs from the saved plan. Run preflight with the requested loader version.')
        self.store.destination(filters.destination)
        # Freeze the revision into a separate export job. Current edits can continue.
        export_id = self.store.create(filters.model_dump(), stage='export', selection=pack['main'],
                selection_revision=revision, composition=pack, brief=selected['brief'],
                projects={str(pid): job['projects'][str(pid)] for pid in pack['main'] if str(pid) in job['projects']},
                confirmed={'revision': revision, 'fingerprint': fingerprint(pack['main']), 'at': time.time()},
                export_options={'name': name, 'loader_version': loader_version},
                file_pins=selected.get('file_pins', {}), export_plan=plan,
                exported_from={'job_id': job_id, 'revision': revision, 'external_projects_omitted': selected['external_projects'] if curseforge_only else {}})
        return {**self.engine.launch(export_id), 'source_job_id': job_id, 'revision': revision,
                'installation_plan_task': plan['task_id'] if plan else None,
                'external_projects_omitted': external_summary(selected['external_projects']) if curseforge_only else {}}


def external_summary(projects):
    return {pid: {key: value for key, value in item.items() if key in ('source','project_id','name','url','selected')}
            for pid, item in projects.items()}


def at_revision(job, revision):
    if revision == job['selection_revision']:
        return snapshot(job, pack_state(job))
    for entry in reversed(job.get('pack_revisions', [])):
        for key in ('after', 'before'):
            if entry[key]['revision'] == revision:
                return entry[key]
    # Legacy history contains only main IDs, not the complete historical brief/reserve.
    raise ValueError('This revision has no complete stored composition snapshot')


def target_list(pack, change, old):
    if change.action == 'remove':
        return 'excluded'
    if change.action == 'restore':
        return change.target or pack['excluded'].get(str(change.project_id), {}).get('from')
    if change.action == 'keep':
        return change.target or (old if old in ('main', 'reserve') else None)
    return change.target or ('main' if change.action == 'add' else None)


def place(pack, pid, target, old, reason, manual):
    key = str(pid)
    if old != target:
        for group in ('main', 'reserve'):
            if pid in pack[group]:
                pack[group].remove(pid)
        if target == 'excluded':
            pack['excluded'][key] = {'from': old, 'reason': reason}
        else:
            pack['excluded'].pop(key, None)
            pack[target].append(pid)
    if manual:
        pack['manual'][key] = {'list': target, 'reason': reason}
        if target == 'excluded':
            pack['manual'][key]['from'] = pack['excluded'][key]['from']


def sync_brief_constraints(job, pack):
    brief = job.setdefault('brief', legacy_brief(job))
    brief['required_ids'] = [int(pid) for pid, row in pack['manual'].items() if row['list'] == 'main']
    brief['excluded_ids'] = [int(pid) for pid, row in pack['manual'].items() if row['list'] == 'excluded']


def changes_between(before, after):
    ids = set(before['main'] + before['reserve'] + after['main'] + after['reserve'])
    ids |= set(map(int, before['excluded'].keys() | after['excluded'].keys() | before['manual'].keys() | after['manual'].keys()))
    return [{'id': pid, 'from': placement(before, pid), 'to': placement(after, pid), 'manual': str(pid) in after['manual']}
            for pid in sorted(ids) if placement(before, pid) != placement(after, pid)
            or before['manual'].get(str(pid)) != after['manual'].get(str(pid))]


def manual_exclusions(job):
    return {int(pid) for pid, entry in pack_state(job)['manual'].items() if entry['list'] == 'excluded'} if 'composition' in job or 'legacy_reports' in job else set()


def normalized(text):
    return re.sub(r'[^\w]+', '', text.casefold())


def validate_page(offset, limit):
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError('offset >= 0; limit 1..100')


def wishes_changed(before, after):
    return any(before.get(key) != after.get(key) for key in
               ('concept', 'hard_requirements', 'preferences', 'extras', 'alternative_criteria', 'final_size', 'reserve'))


def open_prism_import(store, export_job_id):
    """Open Prism's new-instance import for a recorded, intact export only."""
    import hashlib
    import shutil
    import subprocess
    from pathlib import Path

    job = store.get(export_job_id)
    export = job.get('pack_export', {})
    if job['status'] != 'exported' or not export.get('ready'):
        raise ValueError('A completed Prism ZIP export is required')
    archive = Path(export['path'])
    if not archive.is_file() or archive.is_symlink():
        raise ValueError('The recorded archive is missing or no longer a regular file')
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != export.get('sha256'):
        raise ValueError('The archive checksum changed. Export a fresh ZIP.')
    from .platform_support import background_options, prism_executable

    binary = prism_executable()
    if binary:
        command = [binary]
    elif shutil.which('flatpak') and subprocess.run(
        ['flatpak', 'info', 'org.prismlauncher.PrismLauncher'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
    ).returncode == 0:
        command = ['flatpak', 'run', f'--filesystem={archive.parent}:ro', 'org.prismlauncher.PrismLauncher']
    else:
        return {'status': 'manual_import_required', 'archive': str(archive)}
    with (store.job_dir(export_job_id) / 'prism-import.log').open('ab') as log:
        process = subprocess.Popen([*command, '--import', str(archive)], stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, **background_options())
    return {'status': 'import_requested', 'archive': str(archive), 'pid': process.pid,
            'next': 'Confirm creation of a new instance in Prism. Check the launcher for import status.'}
