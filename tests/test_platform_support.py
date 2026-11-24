import json
from pathlib import Path
import subprocess
import sys

import jsonschema

from mod_curator import captures
from mod_curator.platform_support import background_options, prism_executable


ROOT = Path(__file__).resolve().parents[1]


def test_utf8_capture_matches_legacy_without_locale_conversion(tmp_path):
    capture = tmp_path / "catalog-18.html"
    body = "Расширение 🚪\r\n精密な機構" * 40
    capture.write_bytes(body.encode("utf-8"))
    assert captures.read(capture) == body
    captures.write(capture, body)
    assert captures.read(capture) == body
    assert capture.read_bytes() == body.encode("utf-8")
    assert not list(tmp_path.glob("*.part"))
    capture.unlink()
    assert captures.exists(capture)
    assert captures.read(capture) == body


def test_marketplace_config_has_absolute_runtime_with_unicode_and_spaces(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from install import prepare_marketplace
    from runtime_paths import runtime_python

    state = tmp_path / "Сборки p"
    runtime = runtime_python(state / "runtime", windows=True)
    market = state / "windows-marketplace"
    prepare_marketplace(ROOT, market, runtime, state)
    data = json.loads((market / "mcp.json").read_text(encoding="utf-8"))
    jsonschema.validate(data, json.loads((ROOT / "evidence/mcp.schema.json").read_text(encoding="utf-8")))
    server = data["mcpServers"]["minecraft_mod_curator"]
    assert Path(server["command"]).is_absolute()
    assert Path(server["command"]).parts[-2:] == ("Scripts", "python.exe")
    assert server["args"] == ["-X", "utf8", "-m", "mod_curator", "serve", "--transport", "stdio"]
    assert server["env"] == {"MOD_CURATOR_HOME": str(state)}
    assert (market / ".codex-plugin/plugin.json").is_file()
    assert (market / "skills/curate-minecraft-mods/SKILL.md").is_file()


def test_platform_process_options_and_portable_prism(tmp_path, monkeypatch):
    executable = tmp_path / "Prism portable" / ("prismlauncher.exe" if sys.platform == "win32" else "prismlauncher")
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setenv("MOD_CURATOR_PRISM_EXECUTABLE", str(executable))
    assert prism_executable() == str(executable)
    if sys.platform == "win32":
        assert background_options()["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert background_options() == {"start_new_session": True}


def test_browser_detects_edge_without_path_and_checks_cdp(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import start_browser

    edge = tmp_path / "Microsoft/Edge/Application/msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()
    if sys.platform == "win32":
        monkeypatch.setattr(start_browser.shutil, "which", lambda name: None)
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        assert Path(start_browser.chrome_executable()) == edge

    class CDPHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/json/version" else 404)
            self.end_headers()
            self.wfile.write(b'{"webSocketDebuggerUrl":"ws://127.0.0.1/devtools/browser/fixture"}')

    with HTTPServer(("127.0.0.1", 0), CDPHandler) as server:
        thread = Thread(target=server.handle_request)
        thread.start()
        try:
            assert start_browser.cdp_ready(server.server_port)
        finally:
            thread.join(timeout=2)
