#!/usr/bin/env python3
"""Build a wheel and one self-contained plugin ZIP, excluding credentials/state."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
import tomllib

import jsonschema

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
for name in ("plugin", "mcp"):
    jsonschema.validate(json.loads((root / f"{name}.json").read_text(encoding="utf-8")),
                        json.loads((root / f"evidence/{name}.schema.json").read_text(encoding="utf-8")))
instructions = (root / "src/mod_curator/instructions.md").read_text(encoding="utf-8")
skill = "---\nname: curate-minecraft-mods\ndescription: Modpack Builder finds and analyzes real CurseForge catalogue mods, revise selections and export complete manifests for Prism Launcher through local MCP. Use for creating or correcting a Minecraft mod pack.\n---\n\n" + instructions
(root / "skills/curate-minecraft-mods").mkdir(parents=True, exist_ok=True)
(root / "skills/curate-minecraft-mods/SKILL.md").write_text(skill, encoding="utf-8")
if "--package-only" not in sys.argv:
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation"], cwd=root, check=True)
(root / "vendor").mkdir(exist_ok=True)
wheel = root / "dist" / f"minecraft_mod_curator-{version}-py3-none-any.whl"
for old in (root / "vendor").glob("*.whl"):
    if old.name != wheel.name:
        old.unlink()
shutil.copy2(wheel, root / "vendor" / wheel.name)
archive = root / f"dist/minecraft-mod-curator-{version}-plugin.zip"
# An allowlist keeps local captures, state and historical reports out of releases.
include_files = (
    "plugin.json", "mcp.json", ".mcp.json", "pyproject.toml", "README.md", "LICENSE",
    ".gitignore", "requirements.lock.txt", ".agents/plugins/marketplace.json",
    ".codex-plugin/plugin.json", "skills/curate-minecraft-mods/SKILL.md",
    "docs/INSTALL.md", "docs/WINDOWS.md", "docs/AUDIT_WINDOWS.md", "docs/P0_WINDOWS_TRIAL.md",
    "docs/P0_VALIDATION.md", "docs/BACKLOG_WINDOWS_AUDIT.md", "docs/JOB_CLEANUP.md", "docs/MCP.md", "examples/filters.json",
    "evidence/plugin.schema.json", "evidence/mcp.schema.json", f"vendor/{wheel.name}",
)
paths = [root / p for p in include_files]
for directory, pattern in (
    ("src/mod_curator", "*.py"), ("src/mod_curator", "*.md"), ("tests", "*.py"),
    ("scripts", "*.py"), ("scripts", "*.sh"), (".codex/agents", "*.toml"),
):
    paths.extend((root / directory).glob(pattern))
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(set(paths)):
        z.write(p, "minecraft-mod-curator/" + p.relative_to(root).as_posix())
checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [archive, wheel]}
(root / "dist/SHA256SUMS.json").write_text(json.dumps(checksums, indent=2))
print(archive)
