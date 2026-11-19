#!/usr/bin/env python3
"""Relocatable plugin entry point. All user state stays outside the plugin cache."""
import os
from pathlib import Path
import subprocess
import sys

home = Path(os.environ.get("MOD_CURATOR_HOME", Path.home() / ".local/share/mod-curator")).expanduser()
runtime = Path(os.environ.get("MOD_CURATOR_RUNTIME", home / "runtime"))
python = runtime / "bin/python"
if not python.is_file():
    print("Modpack Builder: run bash scripts/install.sh from the unpacked plugin first.", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(subprocess.call([str(python), "-m", "mod_curator", *sys.argv[1:]]))
