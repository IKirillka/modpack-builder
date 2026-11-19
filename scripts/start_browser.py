#!/usr/bin/env python3
"""Start a dedicated normal local Chrome profile. No challenge-solving or stealth."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=9222)
parser.add_argument("--url", default="https://www.curseforge.com/minecraft/search?class=mc-mods&sortBy=total+downloads")
args = parser.parse_args()
binary = next((shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if shutil.which(name)), None)
if binary:
    command = [binary]
    profile = Path(os.environ.get("MOD_CURATOR_HOME", Path.home() / ".local/share/mod-curator")) / "normal-chrome-profile"
elif shutil.which("flatpak") and subprocess.run(["flatpak", "info", "com.google.Chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
    command = ["flatpak", "run", "com.google.Chrome"]
    profile = Path.home() / ".var/app/com.google.Chrome/data/mod-curator-browser"
else:
    parser.error("Install a supported local Chrome/Chromium browser, or start one with a dedicated profile and loopback CDP manually")
profile.mkdir(parents=True, exist_ok=True)
command += [f"--user-data-dir={profile}", f"--remote-debugging-port={args.port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check", args.url]
log = profile / "curator-browser.log"
with log.open("ab") as stream:
    process = subprocess.Popen(command, stdout=stream, stderr=stream, start_new_session=True)
print(f"Browser PID: {process.pid}\nProfile: {profile}\nLocal CDP: http://127.0.0.1:{args.port}\nLog: {log}")
print("Keep this browser open. Complete any visible security check yourself; the plugin never clicks it.")
