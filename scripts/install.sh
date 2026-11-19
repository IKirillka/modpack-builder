#!/usr/bin/env bash
set -euo pipefail
plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
curator_state="${MOD_CURATOR_HOME:-$HOME/.local/share/mod-curator}"
curator_runtime="${MOD_CURATOR_RUNTIME:-$curator_state/runtime}"
python3 -m venv "$curator_runtime"
if compgen -G "$plugin_dir/vendor/*.whl" > /dev/null; then
  "$curator_runtime/bin/python" -m pip install "$plugin_dir"/vendor/*.whl
else
  "$curator_runtime/bin/python" -m pip install "$plugin_dir"
fi
printf 'Modpack Builder runtime installed: %s/bin/mod-curator\n' "$curator_runtime"
printf 'Next: python3 "%s/scripts/launch.py" init --destination "/absolute/path/to/mods" --alias mods\n' "$plugin_dir"

if [[ "${MOD_CURATOR_SKIP_AGENTS:-0}" != "1" ]]; then
  python3 "$plugin_dir/scripts/install_agents.py"
fi
