"""Read-only explanations, local similarity and revision history."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from .composition import Composition, at_revision, changes_between, normalized, placement, validate_page
from .facts import cache_key
from .models import Filters, LOADERS, SourceError
from .preflight import latest, schedule
from .refinement import tokens

def label(project, pid=None):
    return {'project_id': project.get('id', project.get('project_id', pid)), 'source': project.get('source', 'curseforge'),
            **{k: project[k] for k in ('name', 'slug', 'url', 'downloads') if k in project}}


def find_project(store, job, revision, query, related=()):
    wanted = normalized(query)
    if not wanted:
        raise ValueError('Project name, ID or URL required')
    scoped = [*related, *job['projects'].values(), *revision['external_projects'].values()]
    exact = []
    partial = []
    def match(p):
        pid = str(p.get('id', p.get('project_id', '')))
        fname = normalized(p.get('name', ''))
        url = p.get('url', '').rstrip('/')
        slug = normalized(p.get('slug', url.rsplit('/', 1)[-1]))
        if query in (pid, url, f"{p.get('source', 'curseforge')}:{pid}") or wanted in (fname, slug):
            exact.append(p)
        elif len(wanted) >= 3 and wanted in fname:
            partial.append(p)
    seen_ids = {p.get('id') for p in scoped}
    for page in job['pages']:
        scoped.extend(p for p in page.get('boundary_cards', []) if p['id'] not in seen_ids)
        seen_ids.update(p['id'] for p in page.get('boundary_cards', []))
    for project in scoped:
        match(project)
    # Boundary cards are intentionally absent from the project table. Reuse stored
    # identity evidence, or the verified source HTML for older jobs.
    if not query.isdecimal():
        from .source import parse_catalog
        for page in job['pages']:
            without_metadata = set(page['ids']) - seen_ids
            if not without_metadata or not page.get('url'):
                continue
            recorded = parse_qs(urlparse(page['url']).query)
            data = dict(job['filters'])
            loader = next((name for name, values in LOADERS.items()
                           if recorded.get('gameVersionTypeId') == [str(values[0])]), data['loader'])
            data.update(loader=loader, categories=recorded.get('categories', [''])[0].split(',') if recorded.get('categories') else [],
                        excluded_categories=recorded.get('excludedCategories', [''])[0].split(',') if recorded.get('excludedCategories') else [],
                        search=recorded.get('search', [''])[0], minecraft=recorded.get('version', [data['minecraft']])[0],
                        page_size=int(recorded.get('pageSize', [data['page_size']])[0]))
            path = store.root / 'jobs' / job['id'] / f"catalog-{page['number']}.html"
            if not path.is_file():
                continue
            try:
                evidence = parse_catalog(path.read_text(), page['url'], Filters.model_validate(data), page['number'])
            except (ValueError, SourceError, OSError):
                continue
            for card in evidence.cards:
                if card['id'] in without_metadata:
                    match(card)
    matches = {(p.get('source', 'curseforge'), str(p.get('id', p.get('project_id')))): p for p in exact}
    if len(matches) == 1:
        return next(iter(matches.values())), None
    near = matches or {(p.get('source', 'curseforge'), str(p.get('id', p.get('project_id')))): p for p in partial}
    if near:
        return None, {'status': 'ambiguous' if matches else 'no_exact_match', 'total': len(near),
                      'variants': [label(p) for p in list(near.values())[:20]],
                      'reason': None}
    if query.isdecimal():
        return {'id': int(query)}, None
    return None, {'status': 'not_found_in_saved_catalogue', 'query': query, 'reason': None}


def page_evidence(job, pid):
    for page in job['pages']:
        if pid in page['ids']:
            index = page['ids'].index(pid)
            values = page.get('downloads', [])
            if index >= len(values) or type(values[index]) is not int:
                continue
            accepted = page.get('accepted_ids')
            return {'page': page['number'], 'position': index + 1, 'downloads': values[index], 'url': page.get('url'),
                    'accepted': None if accepted is None else pid in accepted,
                    'minimum_downloads': page.get('minimum_downloads', job['filters']['minimum_downloads'])}
    return None


def why(store, job_id, query, revision=None, curseforge_only=False):
    job = store.get(job_id)
    revision = job['selection_revision'] if revision is None else revision
    state = at_revision(job, revision)
    plan = latest(store, job_id, revision, curseforge_only)
    related = (plan or {}).get('projects', {}).values()
    project, early = find_project(store, job, state, query.strip(), related)
    common = {'job_id': job_id, 'revision': revision, 'effective_filters': state['brief']['filters'],
              'collection_reason': job.get('collection_reason'), 'collection_completeness': job['completeness']}
    if early:
        return {**common, **early}
    card = label(project)
    pid = card['project_id']
    if card['source'] != 'curseforge':
        return {**common, 'project': card, 'status': 'external_selected' if project.get('selected') else 'external_record',
                'reason': None, 'basis': 'saved_external_project'}
    pid, key = int(pid), str(pid)
    pack = state['pack']
    where = placement(pack, pid)
    choice = pack['manual'].get(key)
    decision = job['decisions'].get(key) if state['requirements_revision'] == job['requirements_revision'] else None
    if decision is None:
        decision = next((x['decisions'].get(key) for x in reversed(job.get('decision_history', []))
                         if x['requirements_revision'] == state['requirements_revision']), None)
    file = (plan or {}).get('lock', {}).get('files', {}).get(key)
    issues = [i for i in (plan or {}).get('blockers', [])
              if pid in (i.get('project_id'), i.get('dependency_id')) or pid in i.get('chain', [])]
    out = {**common, 'project': card, 'placement': where, 'manual': choice, 'page_evidence': page_evidence(job, pid),
           'file_check': {'task_id': plan['task_id'], 'file_id': file['id'] if file else None, 'issues': issues[:5]} if plan else None}
    if where == 'excluded' or choice:
        reason = (choice or {}).get('reason') or pack['excluded'].get(key, {}).get('reason')
        return {**out, 'status': 'manual_' + where if choice and where else where,
                'reason': reason or None, 'basis': 'saved_revision'}
    if where in ('main', 'reserve'):
        return {**out, 'status': where, 'reason': decision.get('explanation') if decision else None,
                'basis': 'saved_revision', 'decision_evidence': decision.get('evidence_sha256') if decision else None}
    if file and plan['ready']:
        return {**out, 'status': 'required_dependency', 'reason': None, 'basis': 'saved_installation_plan'}
    if issues:
        return {**out, 'status': 'file_check_blocked', 'reason': issues[0]['message'], 'basis': 'saved_file_check'}
    if decision and not decision.get('include'):
        provenance = job.get('decision_basis', {}).get(key, {})
        status = 'insufficient_data' if provenance.get('status') == 'uncertain' else 'excluded_by_concept'
        return {**out, 'status': status, 'reason': decision.get('explanation') or None,
                'basis': 'saved_decision', 'evidence_sha256': decision.get('evidence_sha256')}
    evidence = out['page_evidence']
    if evidence and evidence['accepted'] is False and evidence['downloads'] <= evidence['minimum_downloads']:
        return {**out, 'status': 'filtered_by_downloads', 'reason': 'downloads <= minimum_downloads',
                'threshold_at_collection': evidence['minimum_downloads'], 'basis': 'saved_catalogue_page'}
    if key in job['projects']:
        return {**out, 'status': 'reason_not_recorded' if project.get('description') else 'insufficient_data',
                'reason': project.get('description_error') if not project.get('description') else None,
                'basis': 'saved_catalogue'}
    return {**out, 'status': 'observed_without_saved_decision' if evidence else 'not_found_in_saved_catalogue',
            'reason': None, 'basis': 'saved_catalogue_pages'}


def cached_facts(store, projects):
    with store.connect() as db:
        facts = {}
        for pid, key, value, provenance in db.execute('SELECT project_id,cache_key,record,provenance FROM fact_cache'):
            desc = projects.get(str(pid), {}).get('description')
            if desc and cache_key(pid, desc)[0] == key:
                facts[pid] = {**json.loads(value), 'review_reasons': json.loads(provenance).get('review_flags', [])}
    return facts


def similar(store, job_id, query, revision=None, offset=0, limit=10):
    validate_page(offset, limit)
    job = store.get(job_id)
    revision = job['selection_revision'] if revision is None else revision
    state = at_revision(job, revision)
    project, early = find_project(store, job, state, query.strip())
    if early:
        return early
    focus = label(project)
    if focus['source'] != 'curseforge':
        return {'status': 'no_cached_mechanics', 'project': focus}
    facts = cached_facts(store, job['projects'])
    base = facts.get(focus['project_id'], {})
    mechanics = base.get('mechanics', [])
    if not mechanics:
        return {'status': 'no_cached_mechanics', 'project': focus}
    known_pairs = {frozenset((p['first'], p['second'])): p for p in state.get('relationships', [])}
    base_terms = [(m, tokens(m)) for m in mechanics]
    ranked = []
    for pid, candidate in facts.items():
        if pid == focus['project_id']:
            continue
        terms = [(m, tokens(m)) for m in candidate['mechanics']]
        pairs = [(left, right, len(a & b)) for left, a in base_terms for right, b in terms if len(a & b) >= 2]
        relation = known_pairs.get(frozenset((focus['project_id'], pid)))
        if not pairs and not relation:
            continue
        ranked.append((bool(relation), sum(n for _, _, n in pairs), pid, pairs, relation))
    ranked.sort(reverse=True, key=lambda r: (r[0], r[1], -r[2]))
    items = [{'project': label(job['projects'][str(pid)]), 'placement': placement(state['pack'], pid),
              'possibly_shared_mechanics': [{'focus': left, 'candidate': right} for left, right, _ in pairs[:5]],
              'focus_mechanics_without_evidence_of_replacement': [m for m in mechanics if m not in {a for a, _, _ in pairs}],
              'saved_relationship': relation} for _, _, pid, pairs, relation in ranked[offset:offset + limit]]
    return {'revision': revision, 'project': focus, 'total': len(ranked), 'offset': offset, 'items': items,
            'basis': 'cached_mechanics; unreviewed overlaps are candidates only'}


def revision_numbers(job):
    return sorted({job['selection_revision'], *(j for e in job.get('pack_revisions', []) for j in
                    (e['before']['revision'], e['after']['revision'])), *(h['revision'] for h in job['history'])})


def history_state(job, revision):
    try:
        return at_revision(job, revision), False
    except ValueError:
        history = next((h for h in job['history'] if h['revision'] == revision), None)
        if not history:
            raise
        return {'pack': {'main': history['ids'], 'reserve': [], 'excluded': {}, 'manual': {}}}, True


def historical_files(store, job, revision, curseforge_only):
    """Use a successful older export only when its lock belongs to this revision."""
    if plan := latest(store, job['id'], revision, curseforge_only, ready_only=True):
        return plan['lock']['files']
    if (job['selection_revision'] == revision and job.get('pack_export', {}).get('ready')
            and not job.get('external_projects')):
        return job.get('lock', {}).get('files')
    with store.connect() as db:
        rows = db.execute("""SELECT json_extract(data,'$.lock.files'), json_extract(data,'$.exported_from.external_projects_omitted')
                            FROM jobs WHERE json_extract(data,'$.exported_from.job_id')=?
                            AND json_extract(data,'$.exported_from.revision')=?
                            AND json_extract(data,'$.pack_export.ready')=1 ORDER BY rowid DESC""", (job['id'], revision))
        for files, omitted in rows:
            if files and (curseforge_only or not json.loads(omitted or '{}')):
                return json.loads(files)
    return None


def compare_revisions(store, job_id, revision=None, previous=None, curseforge_only=False, offset=0, limit=30):
    validate_page(offset, limit)
    job = store.get(job_id)
    revision = job['selection_revision'] if revision is None else revision
    if previous is None:
        previous = next((i for i in reversed(revision_numbers(job)) if i < revision), None)
    if previous is None:
        raise ValueError('No earlier revision recorded')
    before, partial_before = history_state(job, previous)
    after, partial_after = history_state(job, revision)
    left, right = before['pack'], after['pack']
    if partial_before or partial_after:
        left, right = [{**p, 'reserve': [], 'excluded': {}, 'manual': {}} for p in (left, right)]
    items = [{'kind': 'placement', **d, 'project': label(job['projects'].get(str(d['id']), {}), d['id'])}
             for d in changes_between(left, right)]
    a, b = (historical_files(store, job, r, curseforge_only) for r in (previous, revision))
    if a and b:
        for pid in sorted(a.keys() | b.keys(), key=int):
            v1, v2 = a.get(pid, {}), b.get(pid, {})
            rs = [{json.dumps(d, sort_keys=True) for d in f.get('dependencies', []) if d['relationType'] == 3} for f in (v1, v2)]
            # Relations may contain list-valued ranges. Compare canonical JSON.
            if v1.get('id') != v2.get('id') or rs[0] != rs[1]:
                items.append({'kind': 'file', 'project_id': int(pid), 'before': v1.get('id'), 'after': v2.get('id'),
                              'required_before': [d for d in v1.get('dependencies', []) if d['relationType'] == 3],
                              'required_after': [d for d in v2.get('dependencies', []) if d['relationType'] == 3]})
    return {'job_id': job_id, 'from_revision': previous, 'to_revision': revision,
            'placement_coverage': 'main_only_legacy_history' if partial_before or partial_after else 'all_lists',
            'file_coverage': 'both_saved_file_sets' if a and b else 'one_or_both_file_sets_missing',
            'total': len(items), 'offset': offset, 'items': items[offset:offset + limit]}


def rollback(engine, job_id, revision, project_id, curseforge_only=False):
    job = engine.store.get(job_id)
    if revision != job['selection_revision']:
        raise ValueError('Rollback requires the current composition revision')
    current = latest(engine.store, job_id, revision, curseforge_only, ready_only=True)
    key = str(project_id)
    active = (current or {}).get('lock', {}).get('files', {}).get(key)
    if not active:
        raise ValueError('Current revision needs a ready plan containing this project')
    for old in reversed(revision_numbers(job)):
        if old >= revision:
            continue
        entry = (historical_files(engine.store, job, old, curseforge_only) or {}).get(key)
        if entry and entry['id'] != active['id']:
            return schedule(engine, job_id, revision, current['loader']['version'], curseforge_only,
                            rollback={'project_id': project_id, 'file_id': entry['id'], 'from_revision': old,
                                      'fixed_roots': {pid: f['id'] for pid, f in current['lock']['files'].items()
                                                      if int(pid) in current['inputs']['roots'] and pid != key}})
    raise ValueError('No distinct previous file ID in saved revision plans')


def record_relationships(engine, job_id, revision, relationships):
    comp = Composition(engine)
    rows = comp.review(job_id, relationships)
    def apply(job, pack):
        entries = {tuple(sorted((row['first'], row['second']))): row for row in job.get('relationships', [])}
        for row in rows:
            entries[tuple(sorted((row['first'], row['second'])))] = row
        job['relationships'] = list(entries.values())
        return {'relationship_count': len(rows)}
    return comp._commit(job_id, revision, 'relationship_evidence', apply)


def disputes(store, job_id, maximum, full_ids=None):
    """One bounded deep-review cohort. Payloads always respect the saved limits."""
    from .economy import Economy
    from .facts import normalize
    job = store.get(job_id)
    revision = job['selection_revision']
    state = at_revision(job, revision)
    settings = Economy(store).settings(job_id)['settings']
    if not 1 <= maximum <= 100:
        raise ValueError('Specify 1–100 as the maximum number of projects')
    facts = cached_facts(store, job['projects'])
    pack = state['pack']
    links = {}
    for pair in state.get('relationships', []):
        if pair['kind'] in ('insufficient_data', 'incompatibility', 'partial_overlap'):
            for pid in (pair['first'], pair['second']):
                links[pid] = links.get(pid, 0) + 1
    plan = latest(store, job_id, revision)
    blockers = {pid for item in (plan or {}).get('blockers', []) for pid in item.get('chain', [])}
    dependent_counts = {}
    for f in (plan or {}).get('lock', {}).get('files', {}).values():
        for dep in f.get('dependencies', []):
            if dep['relationType'] == 3:
                pid = dep['modId']
                dependent_counts[pid] = dependent_counts.get(pid, 0) + 1
    rechecks = set(job.get('recheck_ids', []))
    candidates = []
    focus = set(pack['main'] + pack['reserve']) | links.keys() | blockers | rechecks | dependent_counts.keys()
    for pid in sorted(focus):
        p = job['projects'].get(str(pid), {})
        fact = facts.get(pid, {})
        decision = job['decisions'].get(str(pid), {})
        reasons = ([reason for match, reason in [
            (pid in blockers, 'file_check_blocker'),
            (pid in rechecks, 'requirements_changed'),
            (not p.get('description'), 'description_unavailable'),
            (bool(links.get(pid)), 'recorded_relationship_requires_review'),
            (bool(decision.get('uncertainties')), 'saved_decision_uncertainty'),
            (bool(fact) and fact.get('sufficiency') != 'sufficient', 'insufficient_cached_facts'),
            (bool(fact.get('review_reasons')), 'cached_facts_flagged'),
            (pid in pack['main'] and not (decision or fact), 'no_saved_assessment'),
        ] if match])
        if reasons:
            priority = (pid in blockers, dependent_counts.get(pid, 0), pid in pack['main'], links.get(pid, 0))
            candidates.append((priority, pid, reasons))
    candidates.sort(key=lambda x: (*x[0], -x[1]), reverse=True)
    chosen = candidates[:min(maximum, settings['projects_per_batch'])]
    allowed = {pid for _, pid, _ in chosen}
    requested = set(full_ids or ())
    if not requested <= allowed:
        raise ValueError('Request descriptions only for IDs in this bounded cohort')
    remaining = settings['chars_per_batch']
    items = []
    for _, pid, reasons in chosen:
        p = job['projects'].get(str(pid), {})
        fact = facts.get(pid, {})
        item = {'project': label(p, pid), 'reasons': reasons, 'required_by_count': dependent_counts.get(pid, 0),
                'list': placement(pack, pid), 'facts': {k: fact[k] for k in ('purpose', 'mechanics', 'integrations',
                    'requirements', 'limitations', 'uncertainties', 'sufficiency') if k in fact}}
        if pid in requested:
            desc = normalize(p.get('description') or '')
            if not desc:
                item['description_status'] = 'unavailable'
            elif len(desc) <= remaining:
                item['description'] = desc
                remaining -= len(desc)
            else:
                item.update(description_status='exceeds_remaining_char_budget', description_chars=len(desc))
        items.append(item)
    return {'job_id': job_id, 'revision': revision, 'requested_maximum': maximum, 'open_count': len(candidates),
            'shortlisted': len(items), 'outside_this_cohort': len(candidates) - len(items),
            'full_descriptions_delivered': sum('description' in i for i in items),
            'chars_delivered': settings['chars_per_batch'] - remaining, 'settings': {
                k: settings[k] for k in ('projects_per_batch', 'chars_per_batch', 'model', 'effort', 'retry_model', 'retry_effort')},
            'items': items}
