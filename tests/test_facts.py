import json
import sqlite3
import time
import pytest
from mod_curator.facts import Facts,cache_key,normalize,evidence_text
from mod_curator.store import Store
from mod_curator.engine import Engine
from conftest import FakeSource,decision


def record(pid):
    return dict(project_id=pid,purpose='Фабрика',mechanics=['Вращательная логистика'],categories=['automation'],
                integrations=['Create'],requirements=[],limitations=[],evidence=[f'Full description for {pid}:'],uncertainties=[],sufficiency='sufficient')

async def setup_job(engine,filters,count=4):
    job=engine.store.create(filters.model_dump())
    await engine.run(job,FakeSource([[(i,100) for i in range(1,count+1)]]))
    return job,Facts(engine.store)


def test_cache_key_versions_model_independent():
    key,_=cache_key(1,' mechanism \n restriction ')
    assert key==cache_key(1,'mechanism\nrestriction')[0]
    assert key!=cache_key(2,'mechanism\nrestriction')[0]
    assert key!=cache_key(1,'changed')[0]
    for arg in ['language','schema','instructions']:
        assert key!=cache_key(1,'mechanism\nrestriction',**{arg:'v2'})[0]
    assert 'must not' in normalize('  must not\nJoin our discord\nvalue')
    assert evidence_text('Experience\n: some fact')==evidence_text('Experience: some fact')
    assert evidence_text('their')!=evidence_text('your')

async def test_cache_hit_reuse_and_requirements_preserve(engine,filters):
    job,f=await setup_job(engine,filters)
    f.prepare(job);b=f.claim(job,'luna')
    assert f.commit(b['batch_id'],[record(i) for i in range(1,5)],{'model':'gpt-6-luna','effort':'medium'})['saved']==4
    assert f.prepare(job)['cache_hits']==4
    assert not f.claim(job,'different-model')['batch_id']
    f.save_selections(job,[{'project_id':1,'decision':'matches','reason':'Подходит фабрике'}],1)
    before=engine.store.get(job)
    assert f.get(job,1)['facts']['purpose']=='Фабрика'
    await engine.revise(job,[1],0,wishes='Other mechanics')
    after=engine.store.get(job)
    assert after['requirements_revision']==2 and not after['decisions']
    assert after['decision_history'] and f.prepare(job)['cache_hits']==4
    with pytest.raises(ValueError,match='Stale'):
        f.save_selections(job,[{'project_id':1,'decision':'matches','reason':'old'}],1)
    # Description changes invalidate facts, without destroying the old cache/decisions.
    engine.store.mutate(job,lambda d:d['projects']['1'].update(description='Changed source',description_sha256='b'*64))
    assert f.prepare(job)['cache_hits']==3
    assert len(f.claim(job,'refresh')['projects'])==1

async def test_nonoverlapping_leases_global_cap_expiry_stale_submit(engine,filters):
    job,f=await setup_job(engine,filters,6);f.prepare(job)
    a=f.claim(job,'one',limit=2);b=f.claim(job,'two',limit=2)
    assert not {p['project_id'] for p in a['projects']}&{p['project_id'] for p in b['projects']}
    assert f.claim(job,'third')['reason']=='two_workers_active'
    with engine.store.connect() as c:
        c.execute('UPDATE fact_queue SET lease_until=0 WHERE batch_id=?',(a['batch_id'],))
        c.execute('UPDATE fact_batches SET lease_until=0 WHERE id=?',(a['batch_id'],))
    reclaimed=f.claim(job,'replacement',limit=2)
    assert [p['project_id'] for p in a['projects']]==[p['project_id'] for p in reclaimed['projects']]
    with pytest.raises(ValueError,match='Stale'):
        f.commit(a['batch_id'],[record(1),record(2)],{})
    f.commit(reclaimed['batch_id'],[record(1),record(2)],{})
    with pytest.raises(ValueError): f.commit(reclaimed['batch_id'],[record(1),record(2)],{})

async def test_missing_duplicate_invalid_only_retry_bad_ids(engine,filters):
    job,f=await setup_job(engine,filters);f.prepare(job);b=f.claim(job,'one')
    bad=record(3);bad['evidence']=['invented quote']
    r=f.commit(b['batch_id'],[record(1),record(2),record(2),bad],{})
    assert r['saved']==1 and set(r['retry_or_failed'])=={'2','3','4'}
    b=f.claim(job,'retry');assert {p['project_id'] for p in b['projects']}=={2,3,4}
    with pytest.raises(ValueError,match='unassigned'):
        f.commit(b['batch_id'],[record(999)],{})
    f.fail(b['batch_id'],'network error')
    b=f.claim(job,'last');f.commit(b['batch_id'],[],{})
    assert f.progress(job)['counts']=={'done':1,'failed':3}
    f.prepare(job)  # does not reset attempts and silently spend indefinitely
    assert not f.claim(job,'again')['batch_id']
    f.prepare(job,[2],force=True);assert len(f.claim(job,'explicit')['projects'])==1

async def test_oversized_intact_and_deferred_not_excluded(engine,filters):
    job,f=await setup_job(engine,filters,2)
    engine.store.mutate(job,lambda d:d['projects']['1'].update(description='Substantive limitation. '*1000))
    engine.store.mutate(job,lambda d:d['projects']['2'].pop('description'))
    assert f.prepare(job)['deferred']==1
    b=f.claim(job,'long',max_chars=1000)
    assert b['oversized_single'] and len(b['projects'])==1
    assert b['projects'][0]['description'].endswith('limitation.')
    r=record(1);r.update(sufficiency='insufficient',uncertainties=['Описание не устанавливает механику'],evidence=['Substantive limitation.'])
    f.commit(b['batch_id'],[r],{})
    with pytest.raises(ValueError,match='uncertain'):
        f.save_selections(job,[{'project_id':1,'decision':'excludes','reason':'нет фактов'}],1)
    f.save_selections(job,[{'project_id':1,'decision':'uncertain','reason':'нет фактов'}],1)
    assert engine.store.get(job)['decision_basis']['1']['status']=='uncertain'


def test_migration_preserves_legacy_rows_exactly(tmp_path):
    root=tmp_path/'legacy';root.mkdir();db=root/'state.sqlite3'
    raw=json.dumps({'id':'old','decisions':{'1':{'include':True}},'descriptions':'original'})
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,data TEXT NOT NULL)');c.execute('INSERT INTO jobs VALUES(?,?)',('old',raw))
    s=Store(root)
    with s.connect() as c:
        assert c.execute('SELECT data FROM jobs').fetchone()[0]==raw
        assert c.execute('SELECT count(*) FROM fact_cache').fetchone()[0]==0
        assert c.execute('SELECT version FROM curator_migrations').fetchone()[0]==1
    assert Store(root).get('old')['decisions']['1']['include']

async def test_heartbeat_stale_facts_and_quality_review(engine,filters):
    job,f=await setup_job(engine,filters,1);f.prepare(job);b=f.claim(job,'one')
    assert f.heartbeat(b['batch_id'])['renewed']
    f.commit(b['batch_id'],[record(1)],{'model':'gpt-6-luna','review_flags':['Important ambiguous production detail']})
    assert f.records(job,review_only=True)['total']==1
    assert f.progress(job)['needs_sol_review']==1
    engine.store.mutate(job,lambda d:d['projects']['1'].update(description='source changed'))
    assert f.get(job,1) is None
    assert f.records(job)['total'] == 0 and f.records(job)['stale_excluded'] == 1

async def test_two_workers_across_different_jobs(engine,filters):
    first,f=await setup_job(engine,filters,2);second,_=await setup_job(engine,filters,2)
    f.prepare(first);f.prepare(second)
    a=f.claim(first,'one',limit=1);b=f.claim(second,'two',limit=1)
    assert f.claim(first,'third')['reason']=='two_workers_active'
    f.fail(a['batch_id'],'worker crashed')
    assert f.claim(second,'replacement')['batch_id']


async def test_two_jobs_reuse_completed_work_without_second_model_claim(engine, filters):
    first, facts = await setup_job(engine, filters, 1)
    second, _ = await setup_job(engine, filters, 1)
    facts.prepare(first)
    facts.prepare(second)
    lease = facts.claim(first, 'one')
    assert not facts.claim(second, 'two')['batch_id']
    facts.commit(lease['batch_id'], [record(1)], {})
    restarted = Facts(Store(engine.store.root))
    assert not restarted.claim(second, 'restart')['batch_id']
    assert restarted.get(second, 1)['facts']['project_id'] == 1
    assert restarted.prepare(second, force=True)['cache_hits'] == 0
    restarted.prepare(second)
    assert restarted.claim(second, 'explicit refresh')['batch_id']


async def test_changed_descriptions_never_dispatched_as_current(engine, filters):
    job, facts = await setup_job(engine, filters, 2)
    facts.prepare(job)
    lease = facts.claim(job, 'first', limit=1)
    def change(state):
        for project in state['projects'].values():
            project['description'] = 'New description'
    engine.store.mutate(job, change)
    assert facts.commit(lease['batch_id'], [record(1)], {})['stale_cached'] == [1]
    assert not facts.claim(job, 'second')['batch_id']
    assert facts.progress(job)['counts'] == {'stale': 2}
    facts.prepare(job)
    assert {p['description'] for p in facts.claim(job, 'fresh')['projects']} == {'New description'}
