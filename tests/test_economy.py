from copy import deepcopy
import json
from threading import Barrier, Event

import pytest

from conftest import FakeSource, decision
from mod_curator.brief import BriefPatch
from mod_curator.composition import Composition, PackChange
from mod_curator.economy import Economy, SettingsPatch
from mod_curator.facts import Facts, fragments, referenced_prompt
from mod_curator.refinement import lower_threshold, review_pairs
from mod_curator.store import Store
from test_facts import record


async def job_with_descriptions(engine, filters, count=4):
    jid = engine.store.create(filters.model_dump())
    await engine.run(jid, FakeSource([[(i, 100-i) for i in range(1, count+1)]]))
    return jid


def test_user_presets_and_job_overrides_survive_restart(engine, filters):
    econ = Economy(engine.store)
    jid = engine.store.create(filters.model_dump())
    assert econ.settings()['settings']['workers'] == 1
    econ.settings(patch=SettingsPatch(preset='balanced'))
    econ.settings(jid, SettingsPatch(batches=4))
    fresh = Economy(Store(engine.store.root))
    assert fresh.settings(jid)['settings']['batches'] == 4
    assert fresh.settings()['settings']['batches'] == 3
    assert fresh.settings(jid, reset=True)['settings']['batches'] == 3
    assert fresh.settings(jid, SettingsPatch(preset='faster'))['settings']['workers'] == 2


async def test_preview_does_not_schedule_and_skips_valid_saved_decisions(engine, filters):
    jid = await job_with_descriptions(engine, filters)
    facts = Facts(engine.store)
    job = engine.store.get(jid)
    engine.save_analysis(jid, [decision(job['projects']['1'])], 1)
    facts.prepare(jid, [2])
    batch = facts.claim(jid, 'test')
    facts.commit(batch['batch_id'], [record(2)], {})
    before = engine.store.get(jid)
    service = Economy(engine.store)
    preview = service.preview(jid, batches=1)
    assert preview['counts']['saved_decisions'] == 1
    assert preview['counts']['cache_hits'] == 1
    assert preview['counts']['new'] == 2
    assert preview['estimated_initial_batches'] == 1
    assert engine.store.get(jid) == before and facts.progress(jid)['counts'] == {'done': 1}
    run = service.start(jid, detached=False)
    assert {p['project_id'] for p in facts.claim(jid,'test',run_id=run['run_id'])['projects']} == {3,4}
    assert facts.claim(jid,'legacy')['reason'] == 'budgeted_run_active'


async def test_budget_shared_by_workers_and_retries(engine, filters):
    jid = await job_with_descriptions(engine, filters, 5)
    service = Economy(engine.store)
    service.settings(jid, SettingsPatch(preset='faster', batches=3, projects_per_batch=1))
    run = service.start(jid, detached=False)
    seen = []
    simultaneous = Barrier(2)
    retry_ready = Event()
    def runner(store,batch,effort):
        pid = batch['projects'][0]['project_id']
        seen.append(pid)
        if pid in (1,2) and seen.count(pid) == 1:
            simultaneous.wait(timeout=5)
        if pid == 2:
            assert retry_ready.wait(timeout=5)
        valid = [] if pid == 1 and seen.count(pid) == 1 else [record(pid)]
        Facts(store).commit(batch['batch_id'],valid,{})
        if not valid:
            retry_ready.set()
    result = service.work(run['run_id'], runner)
    assert result['state'] == 'budget_exhausted'
    assert result['used_batches'] == 3 and result['remaining_budget'] == 0
    assert len(seen) == 3 and seen.count(1) == 2
    assert result['retry_assignments'] == 1 and result['target_counts']['queued'] == 3


async def test_stop_drains_before_resuming_same_budget(engine, filters, monkeypatch):
    jid = await job_with_descriptions(engine, filters)
    service = Economy(engine.store)
    service.settings(jid, SettingsPatch(batches=3, projects_per_batch=1))
    run_id = service.start(jid, detached=False)['run_id']
    def runner(store,batch,effort):
        Facts(store).commit(batch['batch_id'], [record(batch['projects'][0]['project_id'])], {})
        service.stop(run_id)
    first = service.work(run_id, runner)
    assert first['state'] == 'paused' and first['used_batches'] == 1
    monkeypatch.setattr(service, 'launch', lambda _: None)
    assert service.resume(run_id)['remaining_budget'] == 2
    second = service.work(run_id, lambda store,batch,effort: Facts(store).commit(batch['batch_id'],[record(batch['projects'][0]['project_id'])],{}))
    assert second['state'] == 'budget_exhausted' and second['used_batches'] == 3


async def test_fragments_reconstruct_evidence_without_breaking_legacy_cache(engine, filters):
    jid = await job_with_descriptions(engine, filters, 2)
    facts = Facts(engine.store)
    facts.prepare(jid)
    batch = facts.claim(jid,'test')
    assert '1|Full description for 1:' in referenced_prompt(batch)
    rows = [record(1), record(2)]
    for row, indices in zip(rows, ([1], [0])):
        row.pop('evidence')
        row['evidence_ids'] = indices
    result = facts.commit(batch['batch_id'],rows,{})
    assert result['saved'] == 1 and list(result['retry_or_failed']) == ['2']
    cached = facts.get(jid,1)['facts']
    assert cached['evidence'] == [fragments(batch['projects'][0]['description'])[0]]
    assert len(facts.claim(jid,'retry')['projects']) == 1


async def test_criterion_routing_and_review_pairs_are_local(engine, filters):
    jid = await job_with_descriptions(engine, filters, 3)
    editor = Composition(engine)
    r = editor.update_brief(jid, BriefPatch(preferences={'decor':'include','energy':'optional'}),0)
    facts = Facts(engine.store)
    facts.prepare(jid)
    batch = facts.claim(jid,'test')
    one, two = record(1), record(2)
    one.update(purpose='Кинетическое хранение жидкостей', mechanics=['Резервуар жидкостей для кинетической логистики'])
    two.update(purpose='Хранение жидкостей', mechanics=['Резервуар жидкостей с регулировкой'])
    facts.commit(batch['batch_id'],[one,two,record(3)],{})
    facts.save_selections(jid,[
        {'project_id':1,'decision':'matches','reason':'Хранение','criteria':['preferences.decor']},
        {'project_id':2,'decision':'matches','reason':'Резервуар','criteria':['preferences.energy']},
    ],r['requirements_revision'])
    r = editor.apply(jid,r['revision'],[PackChange(project_id=1,action='add'),PackChange(project_id=2,action='add',target='reserve')])
    before = engine.store.get(jid)
    results = review_pairs(engine.store,jid)
    assert [(x['first'],x['second']) for x in results['pairs']] == [(1,2)]
    assert engine.store.get(jid) == before and not results['complete_equivalence_review']
    response = editor.update_brief(jid,BriefPatch(preferences={'decor':'omit'}),r['revision'])
    assert response['recheck_plan']['matched_decisions'] == 1
    assert engine.store.get(jid)['recheck_ids'] == [1]
    assert facts.get(jid,1)['facts']['purpose'] == one['purpose']
    facts.save_selections(jid,[{'project_id':1,'decision':'matches','reason':'Механика','criteria':['preferences.decor']}],response['requirements_revision'])
    assert engine.store.get(jid)['recheck_ids'] == []


@pytest.mark.parametrize('source_reason', ['threshold','catalog_end'])
async def test_lower_threshold_reuses_saved_pages_and_only_describes_new(engine, filters, monkeypatch, source_reason):
    from mod_curator.source import Page
    from conftest import card
    saved = [[(1,90),(2,50)],[(3,35),(4,10)]]
    filters.minimum_downloads = 50
    old = engine.store.create(filters.model_dump())
    initial = FakeSource(saved, order_guaranteed=source_reason == 'threshold')
    if source_reason == 'catalog_end':
        engine.store.patch(old,early_stop=False)
    await engine.run(old,initial)
    page_records = engine.store.get(old)['pages']
    assert engine.store.get(old)['collection_reason'] == source_reason
    for item in page_records:
        (engine.store.job_dir(old)/f"catalog-{item['number']}.html").write_text(json.dumps(saved[item['number']-1]))
    def parse(raw, url, _, number):
        return Page([card(pid, count, number, index+1) for index,(pid,count) in enumerate(json.loads(raw))],
                    number<len(saved),4,url)
    monkeypatch.setattr('mod_curator.refinement.parse_catalog',parse)
    d = engine.store.get(old)
    engine.save_analysis(old,[decision(d['projects']['1'])],1)
    before = deepcopy(engine.store.get(old))
    fork = lower_threshold(engine,old,10,0)['job_id']
    live = FakeSource(saved)
    await engine.run(fork,live)
    fresh=engine.store.get(fork)
    assert fresh['order'] == [1,2,3]
    assert fresh['decisions'] == before['decisions']
    assert live.calls == ([1,2] if source_reason=='threshold' else [])
    assert live.describe_calls == [2,3]
    assert engine.store.get(old) == before


async def test_lower_threshold_detects_shift_without_overwriting_origin(engine, filters, monkeypatch):
    from mod_curator.source import Page
    from conftest import card
    old = engine.store.create(filters.model_dump())
    await engine.run(old,FakeSource([[(1,90),(2,50)]], order_guaranteed=True))
    (engine.store.job_dir(old)/'catalog-1.html').write_text('saved')
    monkeypatch.setattr('mod_curator.refinement.parse_catalog',lambda raw,url,f,n:Page([card(1,90),card(2,50)],False,2,url))
    before = engine.store.get(old)
    fork = lower_threshold(engine,old,10,0)['job_id']
    await engine.run(fork,FakeSource([[(1,90),(3,40)]]))
    assert 'CATALOG_SHIFT_AT_BOUNDARY' in engine.store.get(fork)['errors'][-1]['message']
    assert engine.store.get(old) == before


async def test_worker_models_are_scoped_and_route_only_retries(engine, filters):
    jid = await job_with_descriptions(engine, filters, 3)
    facts = Facts(engine.store)
    facts.prepare(jid, [1])
    failed = facts.claim(jid, 'fixture')
    facts.commit(failed['batch_id'], [], {})

    service = Economy(engine.store)
    service.settings(patch=SettingsPatch(model='gpt-6-luna', retry_model='gpt-6.2-sol', retry_effort='low'))
    settings = service.settings(jid, SettingsPatch(preset='balanced', batches=2))
    assert [(item['model'], item['effort']) for item in settings['agent_roles']] == [
        ('gpt-6-luna', 'medium'), ('gpt-6.2-sol', 'low'),
    ]
    assert all(item['task'] for item in settings['agent_roles'])
    assert service.preview(jid)['estimated_initial_batches'] == 2
    run = service.start(jid, detached=False)
    # A preference change applies to a future run only.
    service.settings(jid, SettingsPatch(model='gpt-6.1-terra'))
    seen = []

    def simulate(store, batch, effort):
        ids = [p['project_id'] for p in batch['projects']]
        seen.append((batch['agent'], ids))
        assert effort == batch['agent']['effort']
        Facts(store).commit(batch['batch_id'], [record(pid) for pid in ids], {})

    result = service.work(run['run_id'], simulate)
    assert result['state'] == 'complete' and result['used_batches'] == 2
    assert seen == [
        ({'role':'retry','model':'gpt-6.2-sol','effort':'low'}, [1]),
        ({'role':'extract','model':'gpt-6-luna','effort':'medium'}, [2,3]),
    ]
    assert service.settings(jid)['settings']['model'] == 'gpt-6.1-terra'
    switched = service.settings(jid, SettingsPatch(preset='economy'))
    assert switched['settings']['model'] == 'gpt-6.1-terra'
    assert switched['agent_roles'][1]['model'] == 'gpt-6.2-sol'
    inherited = service.settings(jid, SettingsPatch(retry_model='inherit', retry_effort='inherit'))
    assert inherited['agent_roles'][1]['model'] == 'gpt-6.1-terra'


async def test_worker_does_not_accept_substituted_model(engine, filters, monkeypatch):
    from mod_curator.extraction_worker import run_batch
    jid = await job_with_descriptions(engine, filters, 1)
    facts = Facts(engine.store)
    facts.prepare(jid)
    batch = facts.claim(jid, 'fixture')
    batch['agent'] = {'role':'retry','model':'gpt-6.2-sol','effort':'low'}

    class SimulatedCodex:
        returncode = 0
        def __init__(self, args, stdin, stdout, **kwargs):
            from io import StringIO
            self.stdin = StringIO()
            assert args[args.index('-m')+1] == 'gpt-6.2-sol'
            assert 'model_reasoning_effort="low"' in args
            stdout.write('{"type":"thread.started","thread_id":"01234567-1234-1234-1234-0123456789ab"}\n')

        def wait(self, timeout):
            return 0

    monkeypatch.setattr('mod_curator.extraction_worker.codex_command', lambda: ['fixture-codex'])
    monkeypatch.setattr('mod_curator.extraction_worker.subprocess.Popen', SimulatedCodex)
    monkeypatch.setattr('mod_curator.extraction_worker.observe_session', lambda _: {'model':'gpt-6-luna','reasoning_effort':'low'})
    with pytest.raises(RuntimeError, match='MODEL_NOT_VERIFIED'):
        run_batch(engine.store, batch)
    assert facts.get(jid, 1) is None
