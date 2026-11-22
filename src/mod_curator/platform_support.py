"""Launch local programs without depending on shell quoting or console encoding."""
import os
from pathlib import Path
import shutil
import subprocess


def background_options():
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def windows_program(*suffixes):
    """Find a program installed in standard Windows app locations."""
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(key)
        if root:
            for suffix in suffixes:
                candidate = Path(root) / suffix
                if candidate.is_file():
                    return str(candidate)
    return None


def prism_executable():
    override = os.environ.get("MOD_CURATOR_PRISM_EXECUTABLE")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file() or (os.name == "nt" and candidate.suffix.lower() != ".exe"):
            raise ValueError("MOD_CURATOR_PRISM_EXECUTABLE must point to a local Prism executable")
        return str(candidate.resolve())
    binary = shutil.which("prismlauncher.exe" if os.name == "nt" else "prismlauncher")
    if binary is None and os.name == "nt":
        return windows_program("PrismLauncher/prismlauncher.exe", "Programs/PrismLauncher/prismlauncher.exe")
    return binary


def codex_command():
    override = os.environ.get("MOD_CURATOR_CODEX_EXECUTABLE")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file() or (os.name == "nt" and candidate.suffix.lower() != ".exe"):
            raise ValueError("MOD_CURATOR_CODEX_EXECUTABLE must point to a local Codex executable")
        return [str(candidate.resolve())]
    if os.name != "nt":
        return ["codex"]
    binary = shutil.which("codex.exe")
    if binary:
        return [binary]
    shim = shutil.which("codex.cmd")
    node = shutil.which("node.exe")
    if shim and node:
        entry = Path(shim).parent / "node_modules/@openai/codex/bin/codex.js"
        if entry.is_file():
            return [node, str(entry)]
    raise FileNotFoundError("Codex CLI executable unavailable; set MOD_CURATOR_CODEX_EXECUTABLE to codex.exe")
