"""Real Win32 filesystem and credential tests, run by the Windows CI job."""
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Requires Win32 filesystem APIs")


def test_pinned_directory_prevents_rename_and_refuses_junction(tmp_path):
    from mod_curator.windows_files import pinned_directory

    original = tmp_path / "Каталог mods"
    original.mkdir()
    with pinned_directory(original):
        with pytest.raises(PermissionError):
            original.rename(tmp_path / "moved")
    original.rename(tmp_path / "moved")
    junction = tmp_path / "junction"
    subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J",
                    str(junction), str(tmp_path / "moved")], capture_output=True, check=True)
    try:
        with pytest.raises(OSError, match="reparse point"):
            with pinned_directory(junction):
                pytest.fail("opened a junction")
    finally:
        junction.rmdir()


def test_windows_vault_roundtrip_uses_an_isolated_account(tmp_path, monkeypatch):
    from mod_curator import credentials

    monkeypatch.delenv(credentials.ENV, raising=False)
    credentials.store_key(tmp_path, "local-windows-test-value")
    try:
        assert credentials.effective_key(tmp_path) == "local-windows-test-value"
        assert (tmp_path / credentials.MARKER).read_bytes() == b""
    finally:
        credentials.remove_key(tmp_path)
    assert credentials.effective_key(tmp_path) is None
