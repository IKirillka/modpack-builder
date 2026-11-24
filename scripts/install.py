#!/usr/bin/env python3
"""Install the runtime and prepare a Windows-compatible local marketplace."""
import json
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from runtime_paths import runtime_dir, runtime_python, state_dir


def prepare_marketplace(source, marketplace, python, custom_state=None):
    """Use the installed Python directly so Codex needs no Python alias or shell."""
    entries = (
        ".agents/plugins/marketplace.json",
        ".codex-plugin/plugin.json",
        "plugin.json",
        "skills/curate-minecraft-mods/SKILL.md",
    )
    for relative in entries:
        target = marketplace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    for name in (".mcp.json", "mcp.json"):
        config = json.loads((source / name).read_text(encoding="utf-8"))
        server = config["mcpServers"]["minecraft_mod_curator"]
        server.update(command=str(python), args=["-X", "utf8", "-m", "mod_curator", "serve", "--transport", "stdio"])
        if custom_state is not None:
            server["env"] = {"MOD_CURATOR_HOME": str(custom_state)}
        (marketplace / name).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Modpack Builder requires Python 3.11 or newer.")
    parser = argparse.ArgumentParser(description="Install or update Modpack Builder for the current user.")
    parser.add_argument("--with-chromium", action="store_true",
                        help="Install Playwright Chromium for the optional browser transport")
    options = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    runtime = runtime_dir()
    python = runtime_python(runtime, os.name == "nt")
    # Updating an existing venv does not require rewriting python.exe.
    if not python.is_file():
        import venv
        venv.EnvBuilder(with_pip=True).create(runtime)
    import tomllib
    version = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    wheel = source / "vendor" / f"minecraft_mod_curator-{version}-py3-none-any.whl"
    subprocess.run([str(python), "-X", "utf8", "-m", "pip", "install", str(wheel if wheel.is_file() else source)], check=True)
    if options.with_chromium:
        subprocess.run([str(python), "-X", "utf8", "-m", "playwright", "install", "chromium"], check=True)
    if os.environ.get("MOD_CURATOR_SKIP_AGENTS") != "1":
        subprocess.run([sys.executable, "-X", "utf8", str(source / "scripts/install_agents.py")], check=True)
    if os.name == "nt":
        marketplace = state_dir() / "windows-marketplace"
        prepare_marketplace(source, marketplace, python, state_dir() if os.environ.get("MOD_CURATOR_HOME") else None)
        print(f"Codex local marketplace: {marketplace}")
    print(f"Modpack Builder runtime: {python}")


if __name__ == "__main__":
    main()
