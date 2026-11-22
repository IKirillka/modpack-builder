"""Shared paths for installing and starting the runtime outside the plugin."""
import os
from pathlib import Path


def state_dir():
    return Path(os.environ.get("MOD_CURATOR_HOME") or Path.home() / ".local/share/mod-curator").expanduser().resolve()


def runtime_dir():
    return Path(os.environ.get("MOD_CURATOR_RUNTIME") or state_dir() / "runtime").expanduser().resolve()


def runtime_python(runtime, windows):
    return runtime / ("Scripts/python.exe" if windows else "bin/python")
