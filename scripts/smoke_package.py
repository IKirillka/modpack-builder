#!/usr/bin/env python3
"""Install the actual ZIP in a clean, relocated runtime, then call its MCP."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile
import tomllib
from datetime import date

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
trial = Path(tempfile.mkdtemp(prefix="mod curator тест-")).resolve()
with zipfile.ZipFile(root / f"dist/minecraft-mod-curator-{version}-plugin.zip") as z:
    z.extractall(trial)
plugin = trial / "minecraft-mod-curator"
env = {**os.environ, "MOD_CURATOR_HOME": str(trial / "state"), "MOD_CURATOR_SKIP_AGENTS": "1",
       "MOD_CURATOR_RUNTIME": str(trial / "state/runtime")}
begin = time.monotonic()
installer = [sys.executable, "-X", "utf8", str(plugin / "scripts/install.py")]
if sys.platform == "win32":
    installer = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "install.cmd"]
with (trial / "install.log").open("w", encoding="utf-8") as log:
    subprocess.run(installer, cwd=plugin, env=env, stdout=log, stderr=log, check=True)
subprocess.run([sys.executable, str(plugin / "scripts/launch.py"), "init", "--destination", str(trial / "mods"), "--alias", "smoke"],
               env=env, stdout=subprocess.DEVNULL, check=True)
install_seconds = time.monotonic() - begin
command, args, settings = sys.executable, [str(plugin / "scripts/launch.py"), "serve"], env
runtime = trial / "state/runtime" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
if sys.platform == "win32":
    market_mcp = trial / "state/windows-marketplace/.mcp.json"
    server = json.loads(market_mcp.read_text(encoding="utf-8"))["mcpServers"]["minecraft_mod_curator"]
    command, args, settings = server["command"], server["args"], {**env, **server.get("env", {})}


async def check():
    async with stdio_client(StdioServerParameters(command=command, args=args, env=settings)) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            cap = (await s.call_tool("capabilities", {})).structuredContent
            assert cap["destinations"]["smoke"] == str(trial / "mods")
            assert cap["version"] == version
            assert cap["display_name"] == init.serverInfo.name == "Modpack Builder"
            assert "Windows" in cap["local_downloads"]
            assert {"pack_review", "pack_plan_sources", "verify_prism_export", "pack_check", "pack_check_result", "preview_delete_job", "restore_job", "preview_cleanup"} <= {t.name for t in tools.tools}
            draft = (await s.call_tool("pack_brief", {"action": "save", "patch": {
                "concept": "Create factory", "filters": {"minecraft": "1.21.1", "loader": "NeoForge",
                                                            "categories": ["create"], "minimum_downloads": 1000}
            }})).structuredContent
            assert not draft["ready"] and draft["next_questions"][0]["fields"] == ["release_types"]
            incomplete = await s.call_tool("pack_brief", {"action": "start", "draft_id": draft["draft_id"],
                                                           "expected_revision": draft["revision"]})
            assert incomplete.isError
            ready = (await s.call_tool("pack_brief", {"action": "save", "draft_id": draft["draft_id"],
                "expected_revision": draft["revision"], "patch": {
                    "filters": {"release_types": [1, 2]},
                    "delegated_defaults": ["constraints", "extras", "alternative_criteria"]
                }})).structuredContent
            assert ready["ready"] and not ready["next_questions"] and ready["effective_filters"]["release_types"] == [1, 2]
            assert not (await s.call_tool("list_jobs", {})).structuredContent["result"]
            # Start an isolated, already-cancelled fixture. The installed CLI worker
            # must outlive the launcher that created it and see the same SQLite job.
            fixture = ("from mod_curator.engine import Engine; engine = Engine(); "
                       f"job = engine.store.create({ready['effective_filters']!r}, cancel_requested=True); "
                       "print(engine.launch(job)['job_id'])")
            result = await asyncio.to_thread(subprocess.check_output, [str(runtime), "-X", "utf8", "-c", fixture],
                                             env=env, text=True, encoding="utf-8", timeout=20)
            job_id = result.strip()
            deadline = time.monotonic() + 20
            while True:
                status = (await s.call_tool("job_status", {"job_id": job_id})).structuredContent
                if status["status"] == "cancelled" and not status["worker_active"]:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"Detached worker did not finish: {status}")
                await asyncio.sleep(0.5)
            assert (trial / "state/jobs" / job_id / "report.json").is_file()
            assert (plugin / "skills/curate-minecraft-mods/SKILL.md").exists()
            return {"protocol": init.protocolVersion, "tools": len(tools.tools), "capabilities": cap,
                    "intake": "passed", "detached_worker": "passed"}


report = {"client_date": date.today().isoformat(), "method": "Actual ZIP relocation, fresh venv wheel install, standard MCP client",
          "result": "passed", "trial_root": str(trial), "install_seconds": install_seconds,
          "wheel_sha256": hashlib.sha256(next((plugin / "vendor").glob("*.whl")).read_bytes()).hexdigest(),
          "mcp": asyncio.run(check()), "client_ui_installation_tested": False}
(root / "evidence/package-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
