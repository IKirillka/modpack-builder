import json
import time

import pytest
from filelock import FileLock

from mod_curator.lifecycle import CleanupPatch, DAY, JobLifecycle
from mod_curator.server import make_server


def job(engine, filters, **overrides):
    data = {"status": "completed", "stage": "done", **overrides}
    return engine.store.create(filters.model_dump(), **data)


def age(engine, job_id, days):
    stamp = time.time() - days * DAY
    with engine.store.connect() as db:
        db.execute("""UPDATE jobs SET data=json_set(data,'$.created_at',?,'$.updated_at',?)
                      WHERE id=?""", (stamp, stamp, job_id))
        db.execute("UPDATE job_usage SET opened_at=? WHERE job_id=?", (stamp, job_id))


async def move(service, job_id):
    preview = service.preview_delete(job_id)
    return await service.delete(job_id, job_id, preview["preview_token"])


async def test_preview_confirmation_and_restore_preserve_exact_job(engine, filters):
    study = job(engine, filters, title="Disposable revision", selection_revision=3,
                history=[{"revision": 1}, {"revision": 2}])
    archive = engine.store.destination("mods") / "existing-export.zip"
    archive.write_bytes(b"test archive")
    engine.store.patch(study, pack_export={"path": str(archive)})
    report = engine.store.job_dir(study) / "report.json"
    report.write_text('{"report":"kept"}')
    with engine.store.connect() as db:
        db.execute("INSERT INTO installation_plans VALUES(?,?,0,1,?)", (study, 3, '{"ready":true}'))
    service = JobLifecycle(engine.store)
    before = engine.store.get(study)
    preview = service.preview_delete(study)
    assert preview["job_id"] == study and preview["status"] == "completed"
    assert preview["impact"]["revisions"] == [1, 2, 3]
    assert preview["impact"]["installation_plan_counts_by_revision"] == {3: 1}
    assert preview["impact"]["reports"] == ["report.json"]
    assert preview["impact"]["archive"] == str(archive)
    with pytest.raises(ValueError, match="exact job ID"):
        await service.delete(study, "different", preview["preview_token"])
    with pytest.raises(ValueError, match="Preview"):
        await service.delete(study, study, "not the preview token")
    assert engine.store.get(study) == before
    assert (await service.delete(study, study, preview["preview_token"]))["status"] == "in_trash"
    assert service.trash()["items"][0]["reason"] == "manual"
    assert service.trash()["items"][0]["title"] == "Disposable revision"
    with pytest.raises(ValueError, match="in trash"):
        engine.store.get(study)
    assert report.is_file() and archive.is_file()
    assert service.restore(study)["status"] == "restored"
    assert engine.store.get(study) == before
    with engine.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM installation_plans WHERE job_id=?", (study,)).fetchone()[0] == 1


async def test_delete_waits_for_worker_and_fences_new_work(engine, filters):
    study = job(engine, filters, status="running", stage="collect")
    service = JobLifecycle(engine.store)
    lock = FileLock(engine.store.job_dir(study) / "worker.lock")
    with lock:
        token = service.preview_delete(study)["preview_token"]
        assert (await service.delete(study, study, token, wait_seconds=0))["status"] == "waiting_for_stop"
        assert engine.store.get(study)["cancel_requested"]
        with pytest.raises(ValueError, match="stopping"):
            job(engine, filters, exported_from={"job_id": study})
        engine.store.patch(study, status="cancelled")
        assert (await service.delete(study, study, token, wait_seconds=0))["status"] == "waiting_for_stop"
    assert (await service.delete(study, study, token))["status"] == "in_trash"


async def test_dependency_recheck_and_restore_order(engine, filters):
    parent = job(engine, filters)
    service = JobLifecycle(engine.store)
    token = service.preview_delete(parent)["preview_token"]
    child = job(engine, filters, exported_from={"job_id": parent})
    rejected = await service.delete(parent, parent, token)
    assert rejected["status"] == "blocked_by_references"
    assert rejected["blockers"] == [{"id": child, "location": "active", "relation": "exported_from.job_id"}]
    assert "preview_token" not in service.preview_delete(parent)
    assert (await move(service, child))["status"] == "in_trash"
    assert (await move(service, parent))["status"] == "in_trash"
    assert service.restore(child)["blockers"][0]["id"] == parent
    assert service.preview_delete(parent, permanent=True)["blockers"][0]["id"] == child
    service.restore(parent)
    assert service.restore(child)["status"] == "restored"


async def test_age_statuses_pin_references_and_status_poll_on_startup(engine, filters):
    eligible = job(engine, filters)
    opened = job(engine, filters)
    pinned = job(engine, filters)
    referenced = job(engine, filters)
    paused = job(engine, filters)
    running = job(engine, filters, status="running")
    draft = job(engine, filters, status="draft")
    cancelled = job(engine, filters, status="cancelled")
    recent = job(engine, filters)
    job(engine, filters, extension={"source_job_id": referenced})
    service = JobLifecycle(engine.store)
    service.pin(pinned)
    for study in (eligible, opened, pinned, referenced, paused, running, draft, cancelled):
        age(engine, study, 31)
    age(engine, recent, 29)
    with engine.store.connect() as db:
        db.execute("INSERT INTO economy_runs(id,job_id,settings,state,created_at) VALUES(?,?,?,?,?)",
                   ("paused-run", paused, "{}", "paused", time.time()))
    server = make_server(engine)
    await server._tool_manager.call_tool("job_status", {"job_id": eligible})
    await server._tool_manager.call_tool("pack_brief", {"action": "job", "job_id": opened})
    assert service.settings()["enabled"] is False and service.settings()["days"] == 30
    patch = CleanupPatch(enabled=True, statuses=["completed", "cancelled"])
    preview = service.preview_cleanup(patch)
    assert {r["job_id"] for r in preview["candidates"]} == {eligible, cancelled}
    assert {r["job_id"]: r["reason"] for r in preview["skipped"]} == {
        pinned: "pinned", referenced: "referenced", paused: "analysis_or_worker_active_or_paused",
    }
    assert not service.trash()["items"]
    applied = service.configure(patch, preview["preview_token"])
    assert {r["job_id"] for r in applied["initial_run"]["moved"]} == {eligible, cancelled}
    assert service.settings()["purge_after_days"] is None
    old_run = service.settings()["last_auto_run"]
    overdue_later = job(engine, filters)
    age(engine, overdue_later, 31)
    make_server(engine)
    assert service.settings()["last_auto_run"] == old_run
    assert engine.store.get(overdue_later)
    with engine.store.connect() as db:
        db.execute("UPDATE cleanup_settings SET last_auto_run=?", (time.time() - 25 * 3600,))
    make_server(engine)
    assert any(row["job_id"] == overdue_later for row in service.trash()["items"])


async def test_purge_preserves_shared_cache_settings_and_artifacts(engine, filters):
    study = job(engine, filters)
    other = job(engine, filters)
    report = engine.store.job_dir(study) / "report.json"
    report.write_text("{}")
    outside = engine.store.destination("mods") / "keep.zip"
    outside.write_bytes(b"keep")
    prism = engine.store.destination("mods") / "instance" / "instance.cfg"
    prism.parent.mkdir()
    prism.write_text("name=Fixture")
    with engine.store.connect() as db:
        db.execute("INSERT INTO fact_cache VALUES('shared',1,'hash','schema','instructions','ru','{}','{}',0)")
        db.execute("INSERT INTO fact_queue(job_id,project_id,cache_key,description_hash,payload,state) VALUES(?,1,'shared','hash','{}','done')", (study,))
        db.execute("INSERT INTO economy_settings VALUES('default','{\"workers\":2}')")
        db.execute("INSERT INTO installation_plans VALUES(?,1,0,0,'{}')", (study,))
    service = JobLifecycle(engine.store)
    await move(service, study)
    with engine.store.connect() as db:
        db.execute("UPDATE job_trash SET trashed_at=? WHERE id=?", (time.time() - 8 * DAY, study))
    assert service.sweep()["purged"] == []
    assert report.is_file()
    selection = CleanupPatch(purge_after_days=7)
    preview = service.preview_cleanup(selection)
    assert preview["purge_candidates"] == [study]
    service.configure(selection, preview["preview_token"])
    assert service.sweep()["purged"] == [study]
    assert not report.exists()
    assert outside.read_bytes() == b"keep" and engine.store.get(other)
    assert prism.read_text() == "name=Fixture"
    with engine.store.connect() as db:
        assert db.execute("SELECT cache_key FROM fact_cache").fetchone() == ("shared",)
        assert json.loads(db.execute("SELECT data FROM economy_settings WHERE scope='default'").fetchone()[0]) == {"workers": 2}
        assert db.execute("SELECT COUNT(*) FROM installation_plans WHERE job_id=?", (study,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM fact_queue WHERE job_id=?", (study,)).fetchone()[0] == 0


async def test_destructive_cleanup_lock_and_protected_job_zip(engine, filters):
    study = job(engine, filters)
    age(engine, study, 31)
    original = JobLifecycle(engine.store)
    contender = JobLifecycle(engine.store)
    with original.lock:
        assert contender.sweep()["status"] == "already_running"
    report_dir = engine.store.job_dir(study)
    exported = report_dir / "kept.zip"
    exported.write_bytes(b"keep")
    assert [r["job_id"] for r in contender.sweep()["moved"]] == [study]
    assert not contender.sweep()["moved"]
    preview = contender.preview_delete(study, permanent=True)
    assert preview["blockers"][0]["location"] == "filesystem"
    assert "preview_token" not in preview
    assert exported.read_bytes() == b"keep"
