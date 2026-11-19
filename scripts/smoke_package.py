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
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
trial = Path(tempfile.mkdtemp(prefix="mod-curator-package-smoke-"))
with zipfile.ZipFile(root / f"dist/minecraft-mod-curator-{version}-plugin.zip") as z:
    z.extractall(trial)
plugin = trial / "minecraft-mod-curator"
env = {**os.environ, "MOD_CURATOR_HOME": str(trial / "state"), "MOD_CURATOR_SKIP_AGENTS": "1"}
begin = time.monotonic()
with (trial / "install.log").open("w") as log:
    subprocess.run(["bash", str(plugin / "scripts/install.sh")], env=env, stdout=log, stderr=log, check=True)
subprocess.run([sys.executable, str(plugin / "scripts/launch.py"), "init", "--destination", str(trial / "mods"), "--alias", "smoke"],
               env=env, stdout=subprocess.DEVNULL, check=True)
install_seconds = time.monotonic() - begin


async def check():
    async with stdio_client(StdioServerParameters(command=sys.executable,
            args=[str(plugin / "scripts/launch.py"), "serve"], env=env)) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            cap = (await s.call_tool("capabilities", {})).structuredContent
            assert cap["destinations"]["smoke"] == str(trial / "mods")
            assert cap["version"] == version
            assert cap["display_name"] == init.serverInfo.name == "Modpack Builder"
            assert {"pack_review", "pack_check", "pack_check_result", "preview_delete_job", "restore_job", "preview_cleanup"} <= {t.name for t in tools.tools}
            assert not (await s.call_tool("list_jobs", {})).isError
            assert (plugin / "skills/curate-minecraft-mods/SKILL.md").exists()
            return {"protocol": init.protocolVersion, "tools": len(tools.tools), "capabilities": cap}


report = {"client_date": date.today().isoformat(), "method": "Actual ZIP relocation, fresh venv wheel install, standard MCP client",
          "result": "passed", "trial_root": str(trial), "install_seconds": install_seconds,
          "wheel_sha256": hashlib.sha256(next((plugin / "vendor").glob("*.whl")).read_bytes()).hexdigest(),
          "mcp": asyncio.run(check()), "client_ui_installation_tested": False}
(root / "evidence/package-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
