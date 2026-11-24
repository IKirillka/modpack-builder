#!/usr/bin/env python3
"""Real website/MCP trial: no fake source. Never auto-confirms a mod selection."""
import argparse
import asyncio
from datetime import date
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--transport", choices=["http", "browser", "cdp"], default="http")
    p.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    p.add_argument("--output", type=Path, default=Path("evidence/live-trial.json"))
    args = p.parse_args()
    # Deliberately separate from the user's configured mod folder.
    state = Path(tempfile.mkdtemp(prefix="mod-curator-live-state-")).resolve()
    dest = Path(tempfile.mkdtemp(prefix="mod-curator-live-mods-")).resolve()
    (state / "config.json").write_text(json.dumps({"destinations": {"trial": str(dest)},
        "transport": args.transport, "browser_profile": str(state / "browser"), "headless": True, "cdp_url": args.cdp_url}), encoding="utf-8")
    params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "mod_curator", "serve"],
                                  env={**os.environ, "MOD_CURATOR_HOME": str(state)})
    report = {"client_date": date.today().isoformat(), "source": "REAL CurseForge website (no fixtures)",
              "transport": args.transport, "state_directory": str(state), "destination": str(dest)}
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            report["mcp_protocol"] = init.protocolVersion
            report["tool_count"] = len((await session.list_tools()).tools)
            f = dict(minecraft="1.21.1", loader="NeoForge", categories=["create"], page_size=10,
                     minimum_downloads=30000000, wishes="Create factory and kitchen automation", destination="trial")
            report["filters"] = f
            begin = time.monotonic()
            start = (await session.call_tool("start_collection", {"filters": f})).structuredContent
            report["start_call_seconds"] = time.monotonic() - begin
            jid = start["job_id"]
            report["job_id"] = jid
            while True:
                status = (await session.call_tool("job_status", {"job_id": jid})).structuredContent
                if status["status"] not in ("running", "queued") or (
                    time.monotonic() - begin > 20 and status.get("recoverable_interruption")
                ):
                    break
                await asyncio.sleep(2)
            report["observed_wall_seconds"] = time.monotonic() - begin
            report["status"] = status
            report["results"] = (await session.call_tool("job_results", {"job_id": jid})).structuredContent
            report["stages_not_reached"] = {"descriptions": "Collection did not finish" if status["stage"] == "collect" else "see status",
                "agent_analysis": "Requires collected full descriptions and current-agent decisions; not measured by this script",
                "download": "No user-confirmed trial selection; not started, not measured"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
asyncio.run(main())
