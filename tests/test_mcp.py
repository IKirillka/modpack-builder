import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import FakeSource, decision


async def test_stdio_protocol_and_state_between_connections(engine, filters):
    job = engine.store.create(filters.model_dump())
    await engine.run(job, FakeSource([[(1, 100), (2, 80)]]))
    params = StdioServerParameters(command=sys.executable, args=["-m", "mod_curator", "serve"],
                                  env={**os.environ, "MOD_CURATOR_HOME": str(engine.store.root)})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as s:
            init = await s.initialize()
            assert "No JAR download" in init.instructions
            tools = (await s.list_tools()).tools
            assert len(tools) == 41
            cap = (await s.call_tool("capabilities", {})).structuredContent
            assert cap["destinations"]["mods"] == str(engine.store.destination("mods"))
            status = (await s.call_tool("job_status", {"job_id": job})).structuredContent
            assert status["collected"] == 2
            batch = (await s.call_tool("analysis_batch", {"job_id": job})).structuredContent
            saved = await s.call_tool("save_analysis", {"job_id": job, "requirements_revision": 1,
                                                       "decisions": [decision(p) for p in batch["projects"]]})
            assert not saved.isError
            selected = (await s.call_tool("propose_selection", {"job_id": job, "project_ids": [1, 2],
                            "overlap_review": "Complementary rotational factories", "expected_revision": 0})).structuredContent
            assert selected["revision"] == 1
            invalid = await s.call_tool("confirm_and_install", {"job_id": job, "revision": 0, "selection_fingerprint": "bad"})
            assert invalid.isError
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as s:
            await s.initialize()
            selected = (await s.call_tool("get_selection", {"job_id": job})).structuredContent
            assert [p["id"] for p in selected["items"]] == [1, 2]
            revised = await s.call_tool("revise_selection", {"job_id": job, "project_ids": [1], "expected_revision": 1})
            assert revised.structuredContent["diff"]["removed"] == [2]
            report = await s.call_tool("installation_report", {"job_id": job})
            assert not report.structuredContent["installation_complete"]
            assert not list(engine.store.destination("mods").iterdir())


async def test_status_cursor_exposes_worker_disappearance_and_stage_error(engine, filters):
    from filelock import FileLock
    from mod_curator.server import make_server

    job = engine.store.create(filters.model_dump(), status="running")
    server = make_server(engine)
    async def status(**extra):
        return await server._tool_manager.call_tool("job_status", {"job_id": job, **extra})

    with FileLock(engine.store.job_dir(job) / "worker.lock"):
        before = await status()
        assert before["worker_active"]
        assert not (await status(since=before["cursor"]))["changed"]
    after = await status(since=before["cursor"])
    assert after["changed"] and after["recoverable_interruption"] and not after["worker_active"]
    engine.store.patch(job, status="error", errors=[{"message": "Catalogue validation failed"}])
    assert (await status())["latest_errors"][0]["message"] == "Catalogue validation failed"
