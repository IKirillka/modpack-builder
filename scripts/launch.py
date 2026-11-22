#!/usr/bin/env python3
"""Relocatable plugin entry point. All user state stays outside the plugin cache."""
import os
import subprocess
import sys

from runtime_paths import runtime_dir, runtime_python


def main():
    python = runtime_python(runtime_dir(), os.name == "nt")
    if not python.is_file():
        print("Modpack Builder: run scripts/install.py with Python 3.11+ first.", file=sys.stderr)
        return 1
    return subprocess.call([str(python), "-X", "utf8", "-m", "mod_curator", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
