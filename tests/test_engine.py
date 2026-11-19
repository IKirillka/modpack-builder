import asyncio

import pytest

from mod_curator.engine import Engine
from mod_curator.models import SourceError
from mod_curator.store import Store
from conftest import FakeSource, card, decision


async def run(engine, filters, pages):
    job = engine.store.create(filters.model_dump())
    source = FakeSource(pages)
    await engine.run(job, source)
    return job, source, engine.store.get(job)


@pytest.mark.parametrize("pages,expected,reason,calls", [
    ([[(1, 100), (2, 90)], [(3, 80), (4, 50), (5, 40)], [(6, 10)]], [1, 2, 3], "catalog_end", [1, 2, 3]),
    ([[(1, 50), (2, 40)]], [], "catalog_end", [1]),
    ([[(1, 49), (2, 40)]], [], "catalog_end", [1]),
    ([[(1, 50), (2, 40)], [(3, 100)]], [3], "catalog_end", [1, 2]),
    ([[(1, 100)], []], [1], "catalog_end", [1, 2]),
    ([[]], [], "catalog_end", [1]),
])
async def test_threshold(engine, filters, pages, expected, reason, calls):
    _, source, d = await run(engine, filters, pages)
    assert d["order"] == expected
    assert d["reason"] == reason
    assert source.calls == calls
    assert source.describe_calls == expected
    assert d["status"] == "awaiting_analysis"


async def test_unknown_and_error_resume(engine, filters):
    job, source, d = await run(engine, filters, [[(1, 100)], SourceError("load failed")])
    assert d["status"] == "error" and d["reason"] == "error"
    assert d["next_page"] == 2 and d["order"] == [1]
    engine.resume(job)
    fresh = Engine(Store(engine.store.root))
    source = FakeSource([[(1, 100)], [(2, 80), (3, 50)]])
    await fresh.run(job, source)
    d = fresh.store.get(job)
    assert d["order"] == [1, 2] and source.calls == [1, 2]
    assert len(d["pages"]) == 2
    _, _, d = await run(engine, filters, [[(1, None)]])
    assert d["status"] == "error" and d["reason"] == "error" and not d["pages"]


@pytest.mark.parametrize("pages", [
    [[(1, 100), (2, 40), (3, 80)], [(4, 90), (5, 10)]],
    [[(1, 100), (2, 90)], [(3, 110), (4, 20)], [(5, 60)]],
])
async def test_sort_violation_scans_to_end(engine, filters, pages):
    _, source, d = await run(engine, filters, pages)
    assert len(source.calls) == len(pages)
    assert not d["early_stop"] and d["reason"] == "catalog_end"
    assert d["completeness"] == "uncertain_catalog_scanned"
    assert set(d["order"]) == {i for p in pages for i, n in p if n > 50}


async def test_duplicates_and_repeated_page(engine, filters):
    _, _, d = await run(engine, filters, [[(1, 100), (2, 90)], [(2, 90), (3, 40)], [(4, 30)]])
    assert d["order"] == [1, 2] and not d["early_stop"]
    assert d["reason"] == "catalog_end"
    _, _, d = await run(engine, filters, [[(1, 100)], [(1, 100)]])
    assert d["status"] == "error" and "REPEATED_PAGE" in d["errors"][-1]["message"]


async def test_cancel_and_resume(engine, filters):
    job = engine.store.create(filters.model_dump())
    engine.cancel(job)
    await engine.run(job, FakeSource([[(1, 100)]]))
    assert engine.store.get(job)["reason"] == "cancelled"
    engine.resume(job)
    await engine.run(job, FakeSource([[(1, 100)]]))
    assert engine.store.get(job)["order"] == [1]


async def test_resume_detects_shift(engine, filters):
    job, _, _ = await run(engine, filters, [[(1, 100)], SourceError("load")])
    engine.resume(job)
    await engine.run(job, FakeSource([[(2, 110)], [(3, 90), (4, 30)]]))
    d = engine.store.get(job)
    assert not d["early_stop"] and any("CATALOG_CHANGED" in w for w in d["warnings"])
    assert d["order"] == [1, 2, 3]
    assert d["pages"][0]["ids"] == [2]


async def test_resume_replays_changed_threshold_counts(engine, filters):
    job, _, _ = await run(engine, filters, [[(1, 110), (2, 50)], [(3, 48)], SourceError("load"), [(4, 49)]])
    engine.resume(job)
    source = FakeSource([[(1, 110), (2, 50)], [(3, 51)], [(4, 49)]])
    await engine.run(job, source)
    assert source.calls == [2, 3]
    assert engine.store.get(job)["order"] == [1, 3]


async def test_threshold_stop_for_explicit_provider_guarantee(engine, filters):
    job = engine.store.create(filters.model_dump())
    source = FakeSource([[(1, 100), (2, 50)], [(3, 40)]], order_guaranteed=True)
    await engine.run(job, source)
    assert source.calls == [1]
    assert engine.store.get(job)["catalogue"]["early_stop_used"]


async def test_analysis_and_user_edits(engine, filters):
    job, _, d = await run(engine, filters, [[(1, 100), (2, 80)]])
    with pytest.raises(ValueError, match="Analyze every"):
        engine.propose(job, [1], "Overlaps reviewed", 0)
    batch = engine.analysis_batch(job)
    for p in batch["projects"]:
        engine.save_analysis(job, [decision(p)], batch["requirements_revision"])
    selected = engine.propose(job, [1, 2], "Distinct factory functions", 0)
    with pytest.raises(ValueError, match="Stale"):
        engine.confirm(job, 0, "bad")
    diff = await engine.revise(job, [1, 3], selected["revision"])
    assert diff["diff"] == {"added": [3], "removed": [2], "unchanged": [1]}
    await engine.run(job, FakeSource())
    batch = engine.analysis_batch(job)
    assert [p["id"] for p in batch["projects"]] == [3]
    engine.save_analysis(job, [decision(batch["projects"][0])], 1)
    selected = engine.propose(job, [1, 3], "Added project complements 1; 2 removed", 2)
    engine.confirm(job, selected["revision"], selected["fingerprint"])
    assert engine.store.get(job)["stage"] == "install"
    assert not (engine.store.destination("mods") / "project-1.jar").exists()


async def test_wishes_invalidate_and_unmodified_confirm(engine, filters):
    job, _, d = await run(engine, filters, [[(1, 100)]])
    engine.save_analysis(job, [decision(d["projects"]["1"])], 1)
    s = engine.propose(job, [1], "No overlapping mechanics", 0)
    await engine.revise(job, [1], 1, wishes="Only exploration")
    d = engine.store.get(job)
    assert d["requirements_revision"] == 2 and not d["decisions"]
    with pytest.raises(ValueError, match="Stale requirements"):
        engine.save_analysis(job, [decision(d["projects"]["1"])], 1)
    engine.save_analysis(job, [decision(d["projects"]["1"])], 2)
    s = engine.propose(job, [1], "New preferences reviewed", 2)
    engine.confirm(job, s["revision"], s["fingerprint"])
    assert len(engine.store.get(job)["decisions"]) == 1


async def test_no_jar_without_confirmation(engine, filters):
    job, source, d = await run(engine, filters, [[(1, 100)]])
    assert source.file_calls == []
    with pytest.raises(ValueError, match="confirmation missing"):
        await engine.install(job, source)


async def test_identical_edit_preserves_revision_and_analysis(engine, filters):
    job, _, d = await run(engine, filters, [[(1, 100)]])
    engine.save_analysis(job, [decision(d["projects"]["1"])], 1)
    selected = engine.propose(job, [1], "No overlap", 0)
    result = await engine.revise(job, [1], 1)
    assert result["selection"] == selected
    assert not result["diff"]["added"] and len(engine.store.get(job)["decisions"]) == 1


async def test_partial_install_returns_instead_of_spinning(engine, filters):
    from conftest import file
    job, _, d = await run(engine, filters, [[(1, 100)]])
    engine.save_analysis(job, [decision(d["projects"]["1"])], 1)
    s = engine.propose(job, [1], "No overlap", 0)
    engine.confirm(job, s["revision"], s["fingerprint"])
    source = FakeSource(files={1: [file(1)]})
    await asyncio.wait_for(engine.run(job, source), 5)
    assert engine.store.get(job)["status"] == "partial"
    assert engine.store.get(job)["downloads"]["1"]["status"] == "manual_required"


async def test_missing_descriptions_do_not_block_available_analysis(engine, filters):
    class Partial(FakeSource):
        async def describe(self, project):
            if project['id'] == 2:
                raise SourceError('EMPTY_DESCRIPTION')
            return await super().describe(project)
    job=engine.store.create(filters.model_dump())
    await engine.run(job,Partial([[(1,100),(2,80)]]))
    state=engine.status(job)
    assert state['stage']=='awaiting_analysis'
    assert state['catalogue']['finished'] and state['catalogue']['reason']=='catalog_end'
    assert state['descriptions_available']==1 and state['unresolved_descriptions']==1
    batch=engine.analysis_batch(job)
    assert [p['id'] for p in batch['projects']]==[1]
    assert batch['insufficient_data'][0]['id']==2
    engine.save_analysis(job,[decision(batch['projects'][0])],1)
    engine.propose(job,[1],'Available descriptions reviewed; project 2 deferred, insufficient data',0)
    fresh=Engine(Store(engine.store.root))
    assert fresh.status(job)['analysis_complete_for_available']
    assert not fresh.status(job)['analysis_complete_for_entire_set']
    assert fresh.store.get(job)['projects']['2']['description_state']=='insufficient_data'
    assert not fresh.store.get(job)['confirmed']
