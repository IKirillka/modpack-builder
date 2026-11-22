"""Incremental catalogue extension and local retrieval of pairs for review."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import heapq
import json
import math
import re

from . import captures
from .brief import legacy_brief
from .composition import pack_state
from .models import Filters, SourceError
from .source import parse_catalog


def lower_threshold(engine, job_id, minimum_downloads, revision):
    origin = engine.store.get(job_id)
    if origin.get('search_scopes'):
        raise ValueError('This job uses independent search scopes. Set the new threshold on the affected scope in a linked brief.')
    engine.idle(origin)
    if revision != origin['selection_revision']:
        raise ValueError('Stale selection revision')
    if not 0 <= minimum_downloads < origin['filters']['minimum_downloads']:
        raise ValueError('The new strict download threshold must be lower')
    pages = origin['pages']
    if origin.get('collection_reason') not in ('threshold', 'catalog_end') or not pages:
        raise ValueError('A finished catalogue with saved pages is required')
    directory = engine.store.job_dir(job_id)
    if [page['number'] for page in pages] != list(range(1, len(pages)+1)) or any(
        not captures.exists(directory / f'catalog-{page["number"]}.html') for page in pages
    ):
        raise ValueError('Saved HTML pages are missing or not contiguous. The old job is intact; start a fresh scan only if requested.')
    pack = pack_state(origin)
    brief = deepcopy(origin.get('brief') or legacy_brief(origin))
    brief['filters']['minimum_downloads'] = minimum_downloads
    brief.setdefault('agent_choices', {}).pop('minimum_downloads', None)
    filters = Filters.model_validate({**origin['filters'], 'minimum_downloads': minimum_downloads})
    # Snapshot the old decisions and lists. The separate job protects the original
    # if the catalogue has shifted or the new scan is interrupted.
    initial = {key: deepcopy(origin[key]) for key in (
        'projects', 'decisions', 'decision_basis', 'decision_history', 'external_projects', 'release_policy',
        'history', 'requirements_revision', 'selection_revision', 'recheck_ids', 'recheck_scope_unspecified',
        'file_pins', 'relationships', 'install_evidence', 'replacements',
    ) if key in origin}
    initial.update(brief=brief, composition=pack, selection=pack['main'], early_stop=origin['early_stop'],
                   extension={'source_job_id': job_id, 'source_revision': revision,
                              'old_threshold': origin['filters']['minimum_downloads'],
                              'source_reason': origin['collection_reason'], 'pages': pages,
                              'existing_projects': list(origin['projects'])})
    if not origin['early_stop']:
        initial['warnings'] = ['Source catalogue ordering was unverified; compare every card.']
    child = engine.store.create(filters.model_dump(), **initial)
    return {**engine.launch(child), 'source_job_id': job_id, 'reused_project_records': len(initial['projects']),
            'saved_pages': len(pages), 'first_live_page': len(pages) if origin['collection_reason'] == 'threshold' else None,
            'range': {'above_strict': minimum_downloads, 'old_threshold_inclusive': origin['filters']['minimum_downloads']}}


class ExtensionSource:
    def __init__(self, store, job, source):
        self.source = source
        extension = job['extension']
        self.parent = store.job_dir(extension['source_job_id'])
        self.target = store.job_dir(job['id'])
        self.pages = extension['pages']
        self.boundary = extension['source_reason'] == 'threshold'
        self.checked_boundary = False

    def __getattr__(self, name):
        return getattr(self.source, name)

    async def page(self, filters, number):
        if number > len(self.pages):
            return await self.source.page(filters, number)
        saved = self.pages[number - 1]
        raw = captures.read(self.parent / f'catalog-{number}.html')
        parsed = parse_catalog(raw, saved['url'], filters, number)
        if ([card['id'] for card in parsed.cards] != saved['ids']
                or [card['downloads'] for card in parsed.cards] != saved['downloads']
                or parsed.total != saved['total']):
            raise SourceError('SAVED_PAGE_CHANGED: old captures do not match the recorded catalogue')
        if self.boundary and number == len(self.pages) and not self.checked_boundary:
            live = await self.source.page(filters, number)
            if [card['id'] for card in live.cards] != saved['ids'] or live.total != saved['total']:
                raise SourceError('CATALOG_SHIFT_AT_BOUNDARY: a fresh query is needed. Original job is intact.')
            # Maintain the exact observed counters of the archived prefix. Live
            # boundary validation establishes identity, not a frozen site snapshot.
            self.checked_boundary = True
        captures.write(self.target / f'catalog-{number}.html', raw)
        if hasattr(self.source, 'seed_projects'):
            self.source.seed_projects({card['id']: card for card in parsed.cards})
        return parsed


def changed_criteria(before, after):
    result = set()
    for key in ('concept','alternative_criteria','final_size','reserve'):
        if before.get(key) != after.get(key):
            result.add(key)
    for section in ('preferences','hard_requirements','extras'):
        old, new = before.get(section) or {}, after.get(section) or {}
        result.update(f'{section}.{key}' for key in old.keys() | new.keys() if old.get(key) != new.get(key))
    return result


def tracked_rechecks(job, criteria):
    basis = job.get('decision_basis', {})
    matched = [int(pid) for pid, entry in basis.items() if criteria.intersection(entry.get('criteria', []))]
    untracked = sum(not basis.get(pid, {}).get('criteria') for pid in job['decisions'])
    return matched, untracked


def review_pairs(store, job_id, group='both', focus=None, limit=20, offset=0):
    """Retrieve review leads. No semantic relationships are asserted here."""
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError('Invalid pagination')
    job = store.get(job_id)
    pack = pack_state(job)
    ids = list(dict.fromkeys(pack['main'] + pack['reserve'])) if group == 'both' else pack[group]
    if focus is not None and focus not in ids:
        raise ValueError('Focus project must be in the chosen list')
    selected_ids = set(ids)
    with store.connect() as connection:
        from .facts import cache_key
        records = {pid: json.loads(record) for pid,key,record in connection.execute(
            'SELECT project_id,cache_key,record FROM fact_cache ORDER BY created_at')
            if pid in selected_ids and job['projects'].get(str(pid), {}).get('description')
            and key == cache_key(pid,job['projects'][str(pid)]['description'])[0]}
    by_token = defaultdict(list)
    texts = {}
    for pid in ids:
        fact = records.get(pid)
        if fact:
            terms = tokens(' '.join(fact.get('mechanics', [])) + ' ' + fact['purpose'])
        else:
            decision = job['decisions'].get(str(pid), {})
            terms = tokens(decision.get('functions', ''))
        if not terms:
            continue
        texts[pid] = terms
        for word in terms:
            by_token[word].append(pid)
    scores = Counter()
    overlap = defaultdict(list)
    max_frequency = max(5, len(texts) // 4)
    for token, projects in by_token.items():
        if not 2 <= len(projects) <= max_frequency or (focus is not None and focus not in projects):
            continue
        weight = math.log1p(len(texts)/len(projects))
        for index, first in enumerate(projects):
            for second in projects[index+1:]:
                if focus is not None and focus not in (first,second):
                    continue
                pair = tuple(sorted((first,second)))
                scores[pair] += weight
                overlap[pair].append((weight, token))
    eligible = [(score / math.sqrt(len(texts[a])*len(texts[b])), a, b) for (a,b), score in scores.items()
                if len(overlap[a,b]) >= 2]
    ranked = heapq.nlargest(offset+limit, eligible)[offset:]
    return {'pairs': [{'first': a, 'second': b,
                       'shared_terms': [word for _, word in sorted(overlap[a,b],reverse=True)[:5]],
                       'names': [job['projects'].get(str(pid), {}).get('name', str(pid)) for pid in (a,b)],
                       'evidence': ['facts' if pid in records else 'saved_decision' for pid in (a,b)]}
                      for _,a,b in ranked],
            'summaries': {str(pid): {'purpose': records[pid]['purpose'],
                                       'mechanics_sample': records[pid]['mechanics'][:3]} if pid in records
                          else {'purpose': job['decisions'].get(str(pid), {}).get('functions', '')}
                          for pid in dict.fromkeys(pid for _,a,b in ranked for pid in (a,b))},
            'suggested_total': len(eligible), 'projects_with_summary': len(texts), 'projects_in_scope': len(ids),
            'scope': group, 'focus': focus, 'complete_equivalence_review': False,
            'next_offset': offset+len(ranked), 'policy': 'Review leads only. Do not classify, remove, or replace based solely on retrieval.'}


def tokens(text):
    stop = {'create','minecraft','forge','fabric','neoforge','добавляет','проект','механизм',
            'механизмы','создание','модификация','модификации','сборки','сборка','блоков','версии',
            'поддержка','использование','различные','который','можно','через','других'}
    return {word for word in re.findall(r'[^\W\d_]{4,}', text.casefold()) if word not in stop}
