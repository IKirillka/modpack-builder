#!/usr/bin/env python3
"""Install only this plugin's two managed roles, never replace unrelated agents."""
from pathlib import Path
import os
import tomllib
root=Path(__file__).resolve().parents[1]
folder=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'agents';folder.mkdir(parents=True,exist_ok=True)
marker='# Managed by Minecraft Mod Curator\n'
for name in ['mod_facts_luna','mod_facts_luna_high']:
    source=root/'.codex/agents'/(name+'.toml');data=marker+source.read_text();tomllib.loads(data)
    target=folder/(name+'.toml')
    if target.exists():
        installed=target.read_text()
        if not installed.startswith(marker):
            raise SystemExit(f'Refusing to overwrite unrelated custom agent: {target}')
        print(f'Kept existing role: {name}')
        continue
    target.write_text(data)
    print(f'Installed role: {name}')
