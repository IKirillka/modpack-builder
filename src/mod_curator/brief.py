"""Persistent intake, explicit project requirements, and catalogue change planning."""
from __future__ import annotations

from copy import deepcopy
from .lifecycle import require_available
import json
import time
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from .models import Filters

FILTER_FIELDS = {
    'minecraft', 'loader', 'categories', 'excluded_categories', 'search',
    'minimum_downloads', 'page_size', 'destination', 'allow_prerelease', 'release_types',
}
QUERY_FIELDS = {'minecraft', 'loader', 'categories', 'excluded_categories', 'search', 'page_size'}


class BriefPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    concept: str | None = Field(default=None, min_length=1, max_length=4000)
    filters: dict | None = None
    hard_requirements: dict[str, str] | None = None
    preferences: dict[str, str] | None = None
    required_ids: list[int] | None = None
    excluded_ids: list[int] | None = None
    alternative_criteria: str | None = Field(default=None, min_length=1, max_length=500)
    reserve: bool | None = None
    final_size: int | None = Field(default=None, ge=1)
    agent_choices: dict[str, str] | None = None


def merge_brief(base, changes):
    result = deepcopy(base)
    for key, value in changes.items():
        if key in ('filters', 'hard_requirements', 'preferences', 'agent_choices') and value is not None:
            # Empty values remove a preference/choice by key. Unmentioned keys survive.
            target = result.setdefault(key, {})
            for name, entry in value.items():
                if key == 'filters' and name == 'source':
                    if not isinstance(entry, str) or entry.casefold() != 'curseforge':
                        raise ValueError('The supported catalogue source is CurseForge')
                    continue
                if key == 'filters' and name not in FILTER_FIELDS:
                    raise ValueError(f'Unsupported filter: {name}')
                if key != 'filters' and entry == '':
                    target.pop(name, None)
                else:
                    target[name] = entry
        else:
            result[key] = value
    for key in ('required_ids', 'excluded_ids'):
        ids = result.get(key) or []
        if any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError('Only positive CurseForge project IDs are supported')
        result[key] = list(dict.fromkeys(ids))
    if set(result['required_ids']) & set(result['excluded_ids']):
        raise ValueError('A project cannot be both required and excluded')
    # An explicit correction clears previous agent-choice provenance for that field.
    choices = result.setdefault('agent_choices', {})
    updated_choices = changes.get('agent_choices') or {}
    changed_fields = set(changes) - {'filters', 'agent_choices'}
    changed_fields.update((changes.get('filters') or {}).keys())
    for key in changed_fields - updated_choices.keys():
        choices.pop(key, None)
    if result.get('filters'):
        # Validate even a partial filter set without manufacturing answers in the draft.
        validate_filters(result)
    return result


def validate_filters(brief):
    return Filters.model_validate({
        'minecraft': '1.21.1', 'loader': 'NeoForge', 'minimum_downloads': 0,
        'destination': 'unconfigured', **brief.get('filters', {}), 'wishes': wishes(brief),
    })


def wishes(brief):
    lines = [brief.get('concept') or 'Pending concept']
    for section, title in [('hard_requirements', 'Hard requirements'), ('preferences', 'Flexible preferences')]:
        for key, value in (brief.get(section) or {}).items():
            lines.append(f'{title} / {key} — {value}')
    if brief.get('alternative_criteria'):
        lines.append('Alternative criteria — ' + brief['alternative_criteria'])
    if brief.get('final_size'):
        lines.append(f"Target main-list size {brief['final_size']}; catalogue coverage is independent.")
    return '\n'.join(lines)


def legacy_brief(job):
    filters = {key: value for key, value in job['filters'].items() if key in FILTER_FIELDS}
    if filters.get('release_types') is None and job.get('release_policy', {}).get('allowed_release_types'):
        filters['release_types'] = job['release_policy']['allowed_release_types']
    return {'concept': job['filters']['wishes'], 'filters': filters, 'required_ids': [],
            'excluded_ids': [], 'hard_requirements': {}, 'preferences': {}, 'agent_choices': {},
            'legacy': True}


def filter_plan(job, new_filters):
    old = Filters.model_validate({**legacy_brief(job)['filters'], 'wishes': job['filters']['wishes']})
    before, after = old.model_dump(), new_filters.model_dump()
    changed = {k: {'from': before.get(k), 'to': after.get(k)} for k in FILTER_FIELDS if before.get(k) != after.get(k)}
    changed_query = QUERY_FIELDS & changed.keys()
    if changed_query - {'page_size'}:
        return {'mode': 'new_query', 'changed': changed, 'start_page': 1,
                'reason': 'Changed catalogue filters use a different query. Reuse matching cached descriptions and facts.'}
    if 'minimum_downloads' not in changed:
        return {'mode': 'reuse_existing', 'changed': changed,
                'reason': 'Catalogue coverage is unchanged. Release policy applies at exact file resolution. Page size applies to future scans.'}
    if 'page_size' in changed:
        return {'mode': 'new_query', 'changed': changed, 'start_page': 1,
                'reason': 'Page boundaries change with page size. Reuse matching cached descriptions and facts.'}
    if after['minimum_downloads'] >= before['minimum_downloads']:
        return {'mode': 'saved_subset', 'changed': changed, 'reason': 'Filter the saved catalogue by exact download counts.'}
    pages = job['pages']
    if job.get('collection_reason') == 'catalog_end':
        return {'mode': 'saved_pages', 'changed': changed,
                'reason': 'Reprocess the saved catalogue pages for newly qualifying projects. Fetch only missing descriptions.'}
    if job.get('collection_reason') == 'threshold' and pages:
        return {'mode': 'extend_boundary', 'changed': changed, 'start_page': pages[-1]['number'],
                'reason': 'Reuse saved pages and revisit the boundary. Continue the scan from there. Page shifts require a fresh scan.'}
    return {'mode': 'new_query', 'changed': changed, 'start_page': 1,
            'reason': 'A verified catalogue boundary is unavailable. Reuse matching cached descriptions and facts.'}


class Intake:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def save(self, patch, draft_id=None, copy_from_job=None, carry_manual=False, expected_revision=None):
        if draft_id and (copy_from_job or carry_manual):
            raise ValueError('Copy preferences/manual choices only when opening a new draft')
        if carry_manual and not copy_from_job:
            raise ValueError('Choose the old job before carrying manual choices')
        from .composition import pack_state
        origin = self.store.get(copy_from_job) if copy_from_job else None
        base = (origin.get('brief') or legacy_brief(origin)) if origin else {}
        base = deepcopy(base)
        base.pop('legacy', None)
        if not carry_manual:
            base.pop('required_ids', None)
            base.pop('excluded_ids', None)
        manual = deepcopy(pack_state(origin)['manual']) if origin and carry_manual else {}
        if origin and carry_manual:
            base['required_ids'] = [int(key) for key, entry in manual.items() if entry['list'] == 'main']
            base['excluded_ids'] = [int(key) for key, entry in manual.items() if entry['list'] == 'excluded']
        with self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if copy_from_job:
                require_available(connection, copy_from_job)
            if draft_id:
                data = self._read(connection, draft_id)
                if data.get('job_id') or expected_revision != data['revision']:
                    raise ValueError('Draft started or revision changed. Read the current draft.')
            else:
                draft_id = uuid.uuid4().hex
                data = {'draft_id': draft_id, 'revision': 0, 'brief': base, 'carried_manual': manual,
                        'copy_from_job': copy_from_job}
            data['brief'] = merge_brief(data['brief'], patch.model_dump(exclude_unset=True))
            data['revision'] += 1
            connection.execute('INSERT OR REPLACE INTO intake_drafts VALUES(?,?,?)',
                               (draft_id, json.dumps(data, ensure_ascii=False), time.time()))
        return self.summary(data)

    @staticmethod
    def _read(connection, draft_id):
        row = connection.execute('SELECT data FROM intake_drafts WHERE id=?', (draft_id,)).fetchone()
        if not row:
            raise ValueError('Unknown intake draft')
        return json.loads(row[0])

    def open(self, draft_id=None):
        with self.store.connect() as connection:
            if draft_id:
                return self.summary(self._read(connection, draft_id))
            return [{'draft_id': d['draft_id'], 'revision': d['revision'], 'concept': d['brief'].get('concept')}
                    for (raw,) in connection.execute('SELECT data FROM intake_drafts ORDER BY updated_at DESC')
                    if not (d := json.loads(raw)).get('job_id')][:20]

    def summary(self, data):
        brief = data['brief']
        destinations = list(self.store.config()['destinations'])
        missing = [] if brief.get('concept') else ['concept']
        missing += [key for key in ('minecraft', 'loader', 'minimum_downloads') if key not in brief.get('filters', {})]
        destination = brief.get('filters', {}).get('destination')
        if destination not in destinations and (destination or len(destinations) != 1):
            missing.append('destination')
        return {key: data[key] for key in ('draft_id', 'revision', 'brief', 'job_id') if key in data} | {
            'ready': not missing, 'missing': missing, 'destinations': destinations,
            'source': 'CurseForge', 'sort': 'Total Downloads descending',
            'effective_filters': None if missing else self.effective(brief).model_dump(exclude={'wishes'}),
            'defaults_to_show': [] if missing else [key for key, value in self.effective(brief).model_dump(exclude={'wishes'}).items()
                                if key not in brief.get('filters', {}) and value is not None],
        }

    def effective(self, brief):
        values = deepcopy(brief)
        if not values.setdefault('filters', {}).get('destination'):
            destinations = list(self.store.config()['destinations'])
            if len(destinations) == 1:
                values['filters']['destination'] = destinations[0]
        return validate_filters(values)

    def start(self, draft_id, expected_revision):
        with self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            data = self._read(connection, draft_id)
            if data.get('job_id'):
                return {'job_id': data['job_id'], 'already_started': True}
            if expected_revision != data['revision'] or not self.summary(data)['ready']:
                raise ValueError('Brief incomplete or stale. Read draft before starting.')
            brief = data['brief']
            filters = self.effective(brief)
            self.store.destination(filters.destination)
            effective = filters.model_dump(exclude={'wishes'})
            for key, value in effective.items():
                if key not in brief['filters'] and value is not None:
                    brief['agent_choices'].setdefault(key, 'Default of the installed plugin')
            brief['filters'] = effective
            manual = data['carried_manual']
            for key in ('required_ids', 'excluded_ids'):
                placement = 'main' if key == 'required_ids' else 'excluded'
                manual = {pid: entry for pid, entry in manual.items()
                          if entry['list'] != placement or int(pid) in brief[key]}
                for pid in brief[key]:
                    manual[str(pid)] = {'list': placement, 'reason': 'Задано в ТЗ'}
            main = [int(pid) for pid, entry in manual.items() if entry['list'] == 'main']
            reserve = [int(pid) for pid, entry in manual.items() if entry['list'] == 'reserve']
            excluded = {pid: {'from': entry.get('from'), 'reason': entry['reason']}
                        for pid, entry in manual.items() if entry['list'] == 'excluded'}
            job_id = self.store.create(filters.model_dump(), connection=connection, brief=brief,
                    pending_additions=main + reserve, selection=main,
                    composition={'main': main, 'reserve': reserve, 'excluded': excluded, 'manual': manual},
                    reuse_descriptions_from=data.get('copy_from_job'))
            data['job_id'] = job_id
            connection.execute('UPDATE intake_drafts SET data=?,updated_at=? WHERE id=?',
                               (json.dumps(data, ensure_ascii=False), time.time(), draft_id))
        return self.engine.launch(job_id)
