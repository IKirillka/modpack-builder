import copy
import json
import zipfile

import pytest

from conftest import FakeSource, decision, file
from mod_curator.brief import BriefPatch, Intake
from mod_curator.composition import Composition, PackChange, Relationship, pack_state
from mod_curator.engine import Engine
from mod_curator.facts import Facts
from mod_curator.store import Store


async def ready_job(engine, filters):
    job = engine.store.create(filters.model_dump())
    await engine.run(job, FakeSource([[(1, 9000), (2, 7000), (3, 6000), (4, 5000), (5, 4999)]]))
    engine.save_analysis(job, [decision(p) for p in engine.store.get(job)['projects'].values()], 1)
    engine.propose(job, [1], 'Different functions', 0)
    return job, Composition(engine)


async def test_vague_draft_restart_then_start_preserves_each_answer(engine):
    intake = Intake(engine)
    draft = intake.save(BriefPatch())
    assert 'concept' in draft['missing'] and not engine.store.jobs()
    assert [item['fields'] for item in draft['next_questions']] == [['concept']]
    draft = intake.save(BriefPatch(concept='Factories', preferences={'decor': 'only structural'}), draft['draft_id'], expected_revision=1)
    fresh = Engine(Store(engine.store.root))
    fresh.launch = engine.launch
    intake = Intake(fresh)
    assert intake.open(draft['draft_id'])['brief']['preferences']['decor'] == 'only structural'
    draft = intake.save(BriefPatch(filters={'minecraft':'1.21.1','loader':'NeoForge','categories':['create'], 'minimum_downloads':50, 'release_types':[1,2]},
                                   agent_choices={'minimum_downloads':'keep rare projects'}, required_ids=[1], excluded_ids=[],
                                   delegated_defaults=['extras','alternative_criteria']), draft['draft_id'], expected_revision=2)
    assert draft['ready'] and draft['effective_filters']['destination'] == 'mods'
    with pytest.raises(ValueError, match='stale'):
        intake.start(draft['draft_id'], 2)
    job = intake.start(draft['draft_id'], 3)['job_id']
    assert intake.start(draft['draft_id'], 3)['job_id'] == job
    await fresh.run(job, FakeSource([[(1, 100),(2,90)]]))
    saved = fresh.store.get(job)
    assert len(fresh.store.jobs()) == 1 and saved['selection'] == [1]
    assert saved['brief']['agent_choices']['minimum_downloads'] == 'keep rare projects'
    assert saved['composition']['manual']['1']['list'] == 'main'
    assert Composition(fresh).info(job)['progress']['projects'] == 2


def test_detailed_brief_single_missing_field_and_explicit_copy(engine, filters):
    intake = Intake(engine)
    config = {**filters.model_dump(exclude={'wishes', 'loader'}), 'categories': ['create']}
    draft = intake.save(BriefPatch(concept='Factory', filters=config, alternative_criteria='Functions before downloads', reserve=True,
                                   required_ids=[], excluded_ids=[], delegated_defaults=['extras']))
    assert draft['missing'] == ['loader']
    draft = intake.save(BriefPatch(filters={'loader':'NeoForge'}, required_ids=[123]),draft['draft_id'],expected_revision=1)
    jid = intake.start(draft['draft_id'], 2)['job_id']
    assert intake.save(BriefPatch(), copy_from_job=jid)['brief']['required_ids'] == []
    assert intake.save(BriefPatch(), copy_from_job=jid, carry_manual=True)['brief']['required_ids'] == [123]


async def test_review_reserve_only_pins_strict_prune_restore_undo(engine, filters):
    job, edit = await ready_job(engine, filters)
    def change(pid, action, target=None, basis='supported'):
        return PackChange(project_id=pid, action=action, target=target, reason='Дублирует выбранную производственную цепочку', basis=basis)
    r = edit.apply(job, 1, [change(1,'keep')])
    r = edit.apply(job, r['revision'], [change(2,'add','reserve'),change(3,'add','reserve'),change(4,'add','reserve'),change(5,'add','reserve')], mode='auto')
    pairs = [Relationship(first=2, second=3, kind='substitute', reason='Same exact process'),
             Relationship(first=1, second=4, kind='complement', reason='Adds a process to pinned basis'),
             Relationship(first=2, second=4, kind='substitute', reason='Same role in this pack'),
             Relationship(first=2, second=5, kind='partial_overlap', reason='Shares input',unique_first='Heat',unique_second='Mixing')]
    before = engine.store.get(job)
    assert edit.review(job,pairs)[0]['preferred_by_downloads'] == 2
    assert engine.store.get(job) == before
    r = edit.apply(job,r['revision'],[],mode='reserve_popularity',relationships=pairs)
    assert pack_state(engine.store.get(job))['reserve'] == [2,4,5]
    assert r['main'] == 1 and r['changes'][0]['id'] == 3
    with pytest.raises(ValueError,match='saved placement'):
        engine.propose(job, [], 'Bad', r['revision'])
    r = edit.apply(job,r['revision'],[change(1,'remove'),change(4,'remove'),change(2,'remove',basis='insufficient_data')],mode='prune',above=5000)
    assert r['skipped_count'] == 3 and not r['changed']
    r = edit.apply(job,r['revision'],[change(3,'restore'),change(999,'remove')])
    assert r['reserve'] == 4 and r['skipped_count'] == 1
    r = edit.undo(job,r['revision'])
    assert r['main'] == 1 and r['reserve'] == 3
    r = edit.undo(job,r['revision'])
    assert r['reserve'] == 4 and engine.store.get(job)['selection'] == [1]


async def test_preferences_and_technical_plan_reuse_existing_data(engine, filters):
    job, edit = await ready_job(engine, filters)
    before = engine.store.get(job)
    facts = Facts(engine.store)
    facts.prepare(job)
    queue = facts.progress(job)
    r = edit.update_brief(job, BriefPatch(preferences={'decor':'only functional'}), 1, [2])
    changed = engine.store.get(job)
    assert changed['projects'] == before['projects'] and changed['decisions'] == before['decisions']
    assert changed['recheck_ids'] == [2] and facts.progress(job) == queue
    plan = edit.update_brief(job, BriefPatch(filters={'categories':['create']}), r['revision'])
    assert plan['catalogue_plan']['mode'] == 'new_query' and engine.store.get(job) == changed
    plan = edit.update_brief(job, BriefPatch(filters={'minimum_downloads':1}), r['revision'])
    assert plan['catalogue_plan']['mode'] == 'saved_pages'
    child = Intake(engine).save(BriefPatch(filters={'minimum_downloads':1}, all_categories=True,
                                          delegated_defaults=['constraints','extras','alternative_criteria']), copy_from_job=job)
    child_id = Intake(engine).start(child['draft_id'], child['revision'])['job_id']
    source = FakeSource([[(1, 9000),(6,3000)]])
    await engine.run(child_id, source)
    assert source.describe_calls == [6]


async def test_export_selected_revision_and_dependency_exclusion(engine, filters, monkeypatch):
    job, edit = await ready_job(engine, filters)
    r = edit.apply(job,1,[PackChange(project_id=2,action='add')])
    engine.store.patch(job,lock={'files': {'1':file(1,[2]),'2':file(2)}, 'roots':[1,2]})
    r = edit.apply(job,r['revision'],[PackChange(project_id=2,action='remove')])
    assert r['dependencies']['affected'][0]['root'] == 1 and r['dependencies']['affected'][0]['required'] == 2
    main_before = copy.deepcopy(engine.store.get(job))
    async def loader(*_):
        return {'id':'neoforge-21.1.266'}
    monkeypatch.setattr('mod_curator.prism_export.loader_version', loader)
    source=FakeSource(files={1:[file(1,[2])],2:[file(2)]})
    old_export=edit.export_revision(job,r['revision']-1,'Historical test')
    await engine.run(old_export['job_id'],source)
    pack = engine.store.get(old_export['job_id'])['pack_export']
    with zipfile.ZipFile(pack['path']) as archive:
        assert [row['projectID'] for row in json.loads(archive.read('manifest.json'))['files']] == [1,2]
    latest=edit.export_revision(job,r['revision'],'Blocked test')
    await engine.run(latest['job_id'],source)
    assert 'manually excluded project 2' in engine.store.get(latest['job_id'])['pack_export']['unresolved']
    assert engine.store.get(job) == main_before


async def test_separate_prism_import_uses_only_recorded_archive(engine, filters, monkeypatch):
    from hashlib import sha256
    from types import SimpleNamespace
    from mod_curator.composition import open_prism_import
    job = engine.store.create(filters.model_dump())
    archive = engine.store.destination('mods') / 'fixture.zip'
    archive.write_bytes(b'exported bytes')
    engine.store.patch(job, status='exported', pack_export={'ready':True, 'path':str(archive), 'sha256':sha256(archive.read_bytes()).hexdigest()})
    calls = []
    monkeypatch.setattr('shutil.which', lambda _: '/usr/bin/prismlauncher')
    monkeypatch.setattr('subprocess.Popen', lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(pid=99))
    assert open_prism_import(engine.store, job)['status'] == 'import_requested'
    assert calls == [['/usr/bin/prismlauncher', '--import', str(archive)]]
    archive.write_bytes(b'changed bytes')
    with pytest.raises(ValueError, match='checksum'):
        open_prism_import(engine.store, job)
    assert len(calls) == 1


def test_intake_distinguishes_unanswered_questions_from_authorized_defaults(engine):
    intake = Intake(engine)
    received = intake.save(BriefPatch(concept='Create + Aeronautics', filters={
        'minecraft': '1.21.1', 'loader': 'NeoForge', 'categories': [], 'minimum_downloads': 1000
    }, required_ids=[328085], extras={'decoration': 'skip'}))
    assert not received['ready']
    assert received['missing'] == ['search_scope', 'release_types', 'constraints', 'extras', 'alternative_criteria']
    assert [q['fields'] for q in received['next_questions']] == [['search_scope'], ['release_types']]
    with pytest.raises(ValueError, match='search_scope'):
        intake.start(received['draft_id'], received['revision'])
    assert not engine.store.jobs()

    amended = intake.save(BriefPatch(filters={'categories': ['create']},
                                     delegated_defaults=['release_types', 'constraints', 'extras', 'alternative_criteria'],
                                     unresolved={'threshold': '«До 1 000» — верхняя граница или остановиться на пороге?'}),
                          received['draft_id'], expected_revision=received['revision'])
    assert not amended['ready'] and amended['next_questions'][0]['fields'] == ['threshold']
    done = intake.save(BriefPatch(unresolved={'threshold': ''}), amended['draft_id'], expected_revision=amended['revision'])
    assert done['ready'] and done['next_questions'] == []
    assert done['brief']['required_ids'] == [328085]
    assert done['brief']['extras']['decoration'] == 'skip'
    assert done['effective_filters']['release_types'] == [1]
    assert done['brief']['agent_choices']['alternative_criteria']
    assert 'decoration' not in done['brief']['agent_choices']


async def test_independent_thresholds_resume_and_preserve_one_union(engine):
    from mod_curator.models import SourceError
    from mod_curator.pack_review import page_evidence
    from mod_curator.verification import audit_collection
    from mod_curator import captures
    from test_source import html as website_html

    intake = Intake(engine)
    draft = intake.save(BriefPatch(concept='Create factories + optimization',
        filters={'minecraft': '1.21.1', 'loader': 'NeoForge', 'release_types': [1, 2]},
        search_scopes=[{'label': 'Create', 'categories': ['create'], 'minimum_downloads': 1000},
                       {'label': 'Performance', 'categories': ['performance'], 'minimum_downloads': 50000}],
        required_ids=[], excluded_ids=[5], extras={'decoration':'skip', 'quality_of_life':'allow', 'performance':'prefer'},
        alternative_criteria='Choose downloads only at equal function', reserve=True))
    assert draft['ready'] and 'minimum_downloads' not in draft['effective_filters']
    job = intake.start(draft['draft_id'], draft['revision'])['job_id']

    class TwoScopes(FakeSource):
        def __init__(self, interrupted=False):
            super().__init__()
            self.interrupted = interrupted

        async def page(self, f, number):
            from mod_curator.source import Page, catalog_url
            from conftest import card
            category = f.categories[0]
            self.calls.append((category, f.minimum_downloads, number))
            if category == 'performance' and self.interrupted:
                raise SourceError('SITE_BLOCKED')
            cards = [(2, 75000), (3, 1001), (1, 1000)] if category == 'create' else [(2, 75000), (5, 60000), (4, 50000)]
            records = [card(pid, downloads, number, pos) for pos, (pid, downloads) in enumerate(cards, 1)]
            # Real parser shape verified in audit with a checked category.
            blocks = [{'id': c['id'], 'name': c['name'], 'slug': c['slug'], 'downloadsCount': c['downloads']} for c in records]
            out = website_html(blocks, category=category)
            index = 0 if category == 'create' else 1
            evidence = engine.store.job_dir(job) / f'search-{index+1}'
            evidence.mkdir(exist_ok=True)
            captures.write(evidence / 'catalog-1.html', out)
            return Page(records, False, len(records), catalog_url(f, number))

    first = TwoScopes(interrupted=True)
    await engine.run(job, first)
    pending = engine.store.get(job)
    assert pending['status'] == 'error' and pending['search_scope_index'] == 1
    assert not first.describe_calls
    engine.resume(job)
    second = TwoScopes()
    await engine.run(job, second)
    assert second.calls == [('performance', 50000, 1)]
    saved = engine.store.get(job)
    assert saved['order'] == [2, 3, 5] and sorted(second.describe_calls) == [2, 3, 5]
    assert list(saved['projects']['2']['search_matches']) == ['Create', 'Performance']
    assert saved['composition']['excluded']['5']['reason'] == 'Задано в ТЗ'
    assert page_evidence(saved, 4)['search_scope'] == 'Performance'
    assert page_evidence(saved, 4)['minimum_downloads'] == 50000
    report = audit_collection(engine.store, job)
    assert report['no_omissions_in_captured_pages'] and report['collected_projects'] == 3
    assert len(report['search_scopes']) == 2
