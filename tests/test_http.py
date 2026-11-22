import asyncio
import os
import socket
import subprocess
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def test_streamable_http_protocol(engine):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "mod_curator", "serve", "--transport", "http", "--port", str(port)],
                            env={**os.environ, "MOD_CURATOR_HOME": str(engine.store.root)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient() as c:
            for _ in range(50):
                try:
                    await c.get(url)
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(.1)
            else:
                raise AssertionError("HTTP server did not start")
        async with streamable_http_client(url) as (r, w, get_id):
            async with ClientSession(r, w) as s:
                await s.initialize()
                result = await s.call_tool("capabilities", {})
                assert result.structuredContent["local_downloads"] == "POSIX only (supplemental)"
                assert len((await s.list_tools()).tools) == 43
    finally:
        proc.terminate()
        proc.wait(timeout=5)
