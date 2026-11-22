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
SCOPED_FIELDS = {'categories', 'excluded_categories', 'search', 'minimum_downloads'}


class SearchScope(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    categories: list[str] = Field(default_factory=list)
    excluded_categories: list[str] = Field(default_factory=list)
    search: str = Field(default='', max_length=200)
    minimum_downloads: int = Field(ge=0)
    all_categories: bool = False


ExtraPolicy = Literal['prefer', 'allow', 'skip']
DefaultDecision = Literal['release_types', 'constraints', 'extras', 'alternative_criteria']


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
    search_scopes: list[SearchScope] | None = None
    all_categories: bool | None = None
    extras: dict[Literal['decoration', 'quality_of_life', 'performance'], ExtraPolicy] | None = None
    delegated_defaults: list[DefaultDecision] = Field(default_factory=list)
    unresolved: dict[str, str] | None = None



def merge_brief(base, changes):
    result = deepcopy(base)
    changes = changes.copy()
    delegated = changes.pop('delegated_defaults', [])
    answers = set(result.get('answered_constraints', ()))
    for key in ('required_ids', 'excluded_ids'):
        if key in changes:
            if changes[key] is None:
                answers.discard(key)
            else:
                answers.add(key)
    result['answered_constraints'] = sorted(answers)
    for key, value in changes.items():
        if key in ('filters', 'hard_requirements', 'preferences', 'agent_choices', 'extras', 'unresolved') and value is not None:
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
    filters = result.setdefault('filters', {})
    if 'allow_prerelease' in (changes.get('filters') or {}) and 'release_types' not in changes['filters']:
        filters.pop('release_types', None)
    if 'release_types' in delegated and not has_release_choice(filters):
        filters['release_types'] = [1]
        choices['release_types'] = 'User delegated. Stable releases by default.'
    if 'constraints' in delegated:
        result['answered_constraints'] = ['excluded_ids', 'required_ids']
        choices['constraints'] = 'User delegated. No additional mandatory projects or exclusions.'
    if 'extras' in delegated:
        extras = result.get('extras') or {}
        for key, value in {'decoration': 'skip', 'quality_of_life': 'allow', 'performance': 'allow'}.items():
            if key not in extras:
                extras[key] = value
                choices[key] = 'User delegated this optional role.'
        result['extras'] = extras
    if 'alternative_criteria' in delegated and not result.get('alternative_criteria'):
        result['alternative_criteria'] = ('Keep compatible complements. Prefer useful functions; '
                                           'use downloads to break ties between genuinely equivalent options.')
        choices['alternative_criteria'] = 'User delegated how to rank alternatives.'
    if 'extras' in changes:
        for key in (changes['extras'] or {}):
            if key not in updated_choices:
                choices.pop(key, None)
    if 'constraints' not in delegated and {'required_ids', 'excluded_ids'} & changes.keys():
        choices.pop('constraints', None)
    # Validate partial inputs without manufacturing choices in the draft.
    validate_filters(result)
    effective_scopes(result)
    return result


def has_release_choice(filters):
    return filters.get('release_types') is not None or filters.get('allow_prerelease') is not None


def effective_scopes(brief):
    base = validate_filters(brief)
    scopes = brief.get('search_scopes') or []
    result = []
    seen = set()
    labels = set()
    for raw in scopes:
        scope = SearchScope.model_validate(raw)
        if scope.label.casefold() in labels:
            raise ValueError('Each search scope needs a distinct label')
        if not (scope.categories or scope.search or scope.all_categories):
            raise ValueError('Choose categories, a search term, or explicitly allow all categories for every scope')
        filters = Filters.model_validate({**base.model_dump(),
                    **scope.model_dump(exclude={'label', 'all_categories'})})
        plan = filters.model_dump(exclude={'wishes'})
        signature = json.dumps(plan, sort_keys=True)
        if signature in seen:
            raise ValueError('Two search scopes use the same effective filters')
        seen.add(signature)
        labels.add(scope.label.casefold())
        result.append({'label': scope.label, 'filters': plan})
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
    descriptions = {'prefer': 'prioritize relevant projects', 'allow': 'allow when useful',
                    'skip': 'skip standalone projects of this role'}
    for key, value in (brief.get('extras') or {}).items():
        lines.append(f"Optional role / {key} — {descriptions[value]}")
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


def next_questions(missing, brief, unresolved, destinations):
    if unresolved:
        return [{'fields': [key], 'question': question} for key, question in list(unresolved.items())[:3]]
    extras = {'decoration': 'декор', 'quality_of_life': 'QoL', 'performance': 'оптимизация'}
    questions = {
        'concept': 'Что ты хочешь делать в игре? Какие механики должны стать основой сборки?',
        'minecraft': 'На какой версии Minecraft собираем?',
        'loader': 'Какой загрузчик используем?',
        'search_scope': 'В каких категориях или по каким запросам искать? Например, Create для аддонов и Performance для оптимизации.',
        'minimum_downloads': 'Какой строгий нижний порог скачиваний нужен? Если по разделам пороги разные, укажи каждый отдельно.',
        'release_types': 'Какие файлы допустимы: только релизы, также бета или также альфа?',
        'constraints': 'Какие моды обязательны, а какие исключить? Если жёстких ограничений нет, так и скажи.',
        'extras': 'Что включать из дополнительных ролей: ' + ', '.join(
            label for key, label in extras.items() if key not in (brief.get('extras') or {})) + '?',
        'alternative_criteria': 'Как выбирать между заменами: по нужным функциям, по скачиваниям среди равноценных или по другому правилу?',
        'destination': 'Какую папку выбрать для ZIP?' if destinations else 'Для экспорта нужна зарегистрированная локальная папка.',
    }
    answered = brief.get('answered_constraints') or []
    if answered == ['required_ids']:
        questions['constraints'] = 'Какие моды или механики нужно исключить? Можно ответить «ограничений нет».'
    elif answered == ['excluded_ids']:
        questions['constraints'] = 'Какие моды обязательны? Можно ответить «нет обязательных».'
    if 'concept' in missing:
        return [{'fields': ['concept'], 'question': questions['concept']}]
    for cluster in (('minecraft', 'loader'), ('search_scope', 'minimum_downloads', 'release_types'),
                    ('constraints', 'extras', 'alternative_criteria'), ('destination',)):
        if fields := [key for key in cluster if key in missing]:
            return [{'fields': [key], 'question': questions[key]} for key in fields]
    return []


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
            if origin:
                base.pop('answered_constraints', None)
        manual = deepcopy(pack_state(origin)['manual']) if origin and carry_manual else {}
        if origin and carry_manual:
            base['required_ids'] = [int(key) for key, entry in manual.items() if entry['list'] == 'main']
            base['excluded_ids'] = [int(key) for key, entry in manual.items() if entry['list'] == 'excluded']
            base['answered_constraints'] = ['excluded_ids', 'required_ids']
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
        known = brief.get('filters') or {}
        scopes = effective_scopes(brief)
        missing = [] if brief.get('concept') else ['concept']
        missing.extend(key for key in ('minecraft', 'loader') if not known.get(key))
        if not (scopes or known.get('categories') or known.get('search') or brief.get('all_categories')):
            missing.append('search_scope')
        if not scopes and known.get('minimum_downloads') is None:
            missing.append('minimum_downloads')
        if not has_release_choice(known):
            missing.append('release_types')
        if set(brief.get('answered_constraints', ())) != {'required_ids', 'excluded_ids'}:
            missing.append('constraints')
        if set(brief.get('extras') or {}) != {'decoration', 'quality_of_life', 'performance'}:
            missing.append('extras')
        if not brief.get('alternative_criteria'):
            missing.append('alternative_criteria')
        destination = known.get('destination')
        if destination not in destinations and (destination or len(destinations) != 1):
            missing.append('destination')
        unresolved = {k: v for k, v in (brief.get('unresolved') or {}).items() if v}
        effective = self.effective(brief).model_dump(exclude={'wishes'}) if not missing and not unresolved else None
        for scope in scopes:
            if effective:
                scope['filters']['destination'] = effective['destination']
        displayed_filters = {key: value for key, value in effective.items()
                             if not scopes or key not in SCOPED_FIELDS} if effective is not None else None
        return {key: data[key] for key in ('draft_id', 'revision', 'brief', 'job_id') if key in data} | {
            'ready': effective is not None, 'missing': missing, 'unresolved': unresolved,
            'next_questions': next_questions(missing, brief, unresolved, destinations),
            'destinations': destinations, 'source': 'CurseForge', 'sort': 'Total Downloads descending',
            'effective_filters': displayed_filters, 'search_scopes': scopes,
            'defaults_to_show': [key for key, value in (displayed_filters or {}).items()
                                 if key not in known and value is not None],
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
            if expected_revision != data['revision']:
                raise ValueError('Brief revision is stale. Read the current draft.')
            summary = self.summary(data)
            if not summary['ready']:
                fields = list(dict.fromkeys([*summary['missing'], *summary['unresolved']]))
                raise ValueError('Brief incomplete. Resolve before starting: ' + ', '.join(fields))
            brief = data['brief']
            filters = self.effective(brief)
            scopes = summary['search_scopes']
            self.store.destination(filters.destination)
            effective = filters.model_dump(exclude={'wishes'})
            brief['filters'] = {
                key: value for key, value in effective.items()
                if not scopes or key not in SCOPED_FIELDS or key in brief['filters']
            }
            for key in summary['defaults_to_show']:
                brief['agent_choices'].setdefault(key, 'Default of the installed plugin')
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
                    reuse_descriptions_from=data.get('copy_from_job'),
                    **({'search_scopes': scopes, 'search_scope_index': 0} if scopes else {}))
            data['job_id'] = job_id
            connection.execute('UPDATE intake_drafts SET data=?,updated_at=? WHERE id=?',
                               (json.dumps(data, ensure_ascii=False), time.time(), draft_id))
        return self.engine.launch(job_id)
