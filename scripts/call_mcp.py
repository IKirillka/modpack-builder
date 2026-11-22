#!/usr/bin/env python3
"""Small bridge for the CURRENT agent to call this local MCP server, no model API."""
import asyncio
import json
from pathlib import Path
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    tool = sys.argv[1]
    value = sys.argv[2] if len(sys.argv) > 2 else "{}"
    args = json.loads(Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value)
    async with stdio_client(StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "mod_curator", "serve"], env=dict(os.environ))) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            result = await s.call_tool(tool, args)
            content = result.structuredContent or {"content": [c.text for c in result.content if c.type == "text"]}
            print(json.dumps(content, ensure_ascii=False, indent=2))
            if result.isError:
                raise SystemExit(1)


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
asyncio.run(main())
