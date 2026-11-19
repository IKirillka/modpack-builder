import json
import zipfile

import pytest

from conftest import FakeSource, card, file
from mod_curator.composition import Composition, PackChange
from mod_curator.pack_review import compare_revisions, disputes, rollback, why
from mod_curator.preflight import latest, schedule
from mod_curator.updates import apply_proposals, inspect_candidates, propose, report


LOADER = {'id': 'neoforge-21.1.266', 'version': '21.1.266', 'version_metadata_sha256': 'same'}


@pytest.fixture(autouse=True)
def prism_metadata(monkeypatch):
    async def loader(*args):
        return LOADER
    monkeypatch.setattr('mod_curator.preflight.loader_version', loader)
    monkeypatch.setattr('mod_curator.prism_export.loader_version', loader)


def make_job(engine, filters, *, projects=(1, 2, 3), main=(1,), **extra):
    return engine.store.create(filters.model_dump(), status='awaiting_analysis', stage='awaiting_analysis',
                               projects={str(i): card(i, 100 + i) | {'description': f'Mechanics {i}'} for i in projects},
                               selection=list(main), selection_revision=1, **extra)


async def run_check(engine, source, job, revision=None):
    task = schedule(engine, job, revision)['job_id']
    await engine.run(task, source)
    return engine.store.get(task)


def test_why_missing_reason_duplicate_and_threshold(engine, filters):
    job = make_job(engine, filters, pages=[{'ids': [4], 'number': 2, 'downloads': [50], 'accepted_ids': [],
                                         'boundary_cards': [card(4, 50, 2)]}])
    assert why(engine.store, job, 'Project 2')['status'] == 'reason_not_recorded'
    assert why(engine.store, job, 'Project 2')['reason'] is None
    answer = why(engine.store, job, 'Project 4')
    assert answer['status'] == 'filtered_by_downloads'
    assert answer['page_evidence']['downloads'] == filters.minimum_downloads
    engine.store.mutate(job, lambda d: d['projects']['3'].update(name='Project 2'))
    ambiguous = why(engine.store, job, 'Project 2')
    assert ambiguous['status'] == 'ambiguous'
    assert {p['project_id'] for p in ambiguous['variants']} == {2, 3}
    assert why(engine.store, job, 'missing')['status'] == 'not_found_in_saved_catalogue'
    engine.store.mutate(job, lambda d: d['projects']['2'].update(name='Create: New Age', slug='create-new-age'))
    guess = why(engine.store, job, 'Create')
    assert guess['status'] == 'no_exact_match'
    assert [p['project_id'] for p in guess['variants']] == [2]


async def test_preflight_chain_requires_unavailable_version(engine, filters):
    job = make_job(engine, filters)
    s = FakeSource(files={1: [file(1, [2])], 2: [file(2, [{'modId': 3, 'relationType': 3, 'versionRange': '>=2.0'}])],
                          3: [file(3, versionNumber='1.8')]})
    result = await run_check(engine, s, job)
    assert result['status'] == 'blocked'
    issues = result['check_plan']['blockers']
    edge = next(i for i in issues if i['code'] == 'version_conflict')
    assert edge['chain'] == [1, 2, 3]
    assert edge['version_range'] == '>=2.0' and edge['candidate_version'] == '1.8'
    assert why(engine.store, job, '3')['status'] == 'file_check_blocked'
    assert not list(engine.store.destination('mods').glob('*.zip'))
    with pytest.raises(ValueError, match='blocked or stale'):
        Composition(engine).export_revision(job, 1, 'Test')


async def test_preflight_exact_files_and_changed_availability(engine, filters):
    job = make_job(engine, filters)
    current = file(1, [2], fid=105, allowModDistribution=False)
    dep = file(2, fid=201)
    first = await run_check(engine, FakeSource(files={1: [current], 2: [dep]}), job)
    assert first['status'] == 'completed'
    assert any(w['code'] == 'manual_download' for w in first['check_plan']['warnings'])
    assert not list(engine.store.destination('mods').glob('*.zip'))
    revision = Composition(engine).export_revision(job, 1, 'Test')['job_id']
    await engine.run(revision, FakeSource(files={1: [file(1, [2], fid=106)], 2: [dep]}))
    draft = engine.store.get(revision)
    assert draft['status'] == 'draft'
    assert 'INSTALLATION_PLAN_STALE' in draft['pack_export']['unresolved']
    assert not list(engine.store.destination('mods').glob('*.zip'))
    other = file(1, fid=106)
    other['fileDate'] = '2026-11-19'
    revision = Composition(engine).export_revision(job, 1, 'Test')['job_id']
    await engine.run(revision, FakeSource(files={1: [other, current], 2: [dep]}))
    exported = engine.store.get(revision)['pack_export']
    assert exported['installation_plan_task'] == first['id']
    with zipfile.ZipFile(exported['path']) as archive:
        parsed = json.loads(archive.read('manifest.json'))
    assert {(p['projectID'], p['fileID']) for p in parsed['files']} == {(1, 105), (2, 201)}
    assert parsed['minecraft']['modLoaders'][0]['id'] == LOADER['id']


async def test_updates_preserve_manual_placement_and_explicit_pins_then_rollback(engine, filters):
    job = make_job(engine, filters, file_pins={'2': 200}, main=(1, 2),
                   composition={'main': [1, 2], 'reserve': [3], 'excluded': {},
                                'manual': {'1': {'list': 'main', 'reason': 'Fixed base'}, '3': {'list': 'reserve', 'reason': 'Keep'}}})
    old = {1: [file(1)], 2: [file(2)]}
    await run_check(engine, FakeSource(files=old), job)
    new = {1: [file(1, [5], fid=101), *old[1]], 2: [file(2, fid=202), *old[2]], 5: [file(5)]}
    new[1][0]['fileDate'] = new[2][0]['fileDate'] = '2026-11-19'
    preview = propose(engine, job, 1, 1, True, None, False)['job_id']
    await engine.run(preview, FakeSource([[(1, 100), (4, 51)], [(8, 500), (6, 50)]], new))
    evidence = report(engine.store, preview, 0, 10)
    assert evidence['catalogue_coverage']['cards_checked'] == 2
    assert not evidence['catalogue_coverage']['complete_for_filters']
    assert any(row.get('id') == 4 for row in evidence['items'])
    assert engine.store.get(job)['selection_revision'] == 1
    candidate = inspect_candidates(engine, job, 1, preview, [1, 4])['job_id']
    candidate_source = FakeSource()
    await engine.run(candidate, candidate_source)
    assert candidate_source.describe_calls == [1, 4]
    assert engine.store.get(job)['projects']['1']['description'] == 'Mechanics 1'
    task = apply_proposals(engine, job, 1, preview, [], {'1': 101, '2': 202},
                           candidate_review_job_id=candidate)['job_id']
    await engine.run(task, FakeSource(files=new))
    r2 = engine.store.get(job)
    assert r2['selection_revision'] == 2
    assert r2['composition']['manual']['1']['reason'] == 'Fixed base'
    assert r2['composition']['reserve'] == [3]
    assert r2['file_pins'] == {'1': 101, '2': 200}
    assert r2['recheck_ids'] == [1]
    assert 'rotational factories' in r2['projects']['1']['description']
    assert latest(engine.store, job, 2)['lock']['files']['5']['id'] == 500
    diff = compare_revisions(engine.store, job)['items']
    assert {r['project_id'] for r in diff} == {1, 5}
    t = rollback(engine, job, 2, 1)['job_id']
    await engine.run(t, FakeSource(files={1: [new[1][0]], 2: old[2], 5: new[5]}))
    assert engine.store.get(t)['status'] == 'blocked'
    assert engine.store.get(job)['selection_revision'] == 2
    t = rollback(engine, job, 2, 1)['job_id']
    await engine.run(t, FakeSource(files=new))
    assert engine.store.get(job)['file_pins']['1'] == 100
    assert latest(engine.store, job, 3)['lock']['files']['1']['id'] == 100
    assert '5' not in latest(engine.store, job, 3)['lock']['files']


def test_bounded_disputes_do_not_fetch_unassigned_descriptions(engine, filters):
    job = make_job(engine, filters, main=(1, 2, 3), recheck_ids=[1, 2, 3])
    first = disputes(engine.store, job, 2)
    assert first['shortlisted'] == 2
    assert first['outside_this_cohort'] == 1
    assert first['full_descriptions_delivered'] == 0
    second = disputes(engine.store, job, 2, [1, 2])
    assert second['full_descriptions_delivered'] == 2
    with pytest.raises(ValueError, match='bounded cohort'):
        disputes(engine.store, job, 2, [3])


def test_compare_saved_placements_and_manual_absence(engine, filters):
    job = make_job(engine, filters)
    Composition(engine).apply(job, 1, [PackChange(project_id=1, action='remove', reason='User choice')])
    diff = compare_revisions(engine.store, job, 2, 1)
    assert [(i['id'], i['from'], i['to']) for i in diff['items']] == [(1, 'main', 'excluded')]
    assert why(engine.store, job, '1')['reason'] == 'User choice'
    assert why(engine.store, job, '1', 1)['status'] == 'main'
