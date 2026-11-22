#!/usr/bin/env python3
"""Start a dedicated normal local Chrome profile. No challenge-solving or stealth."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

from runtime_paths import state_dir


def chrome_executable():
    names = ("chrome.exe", "chromium.exe") if os.name == "nt" else (
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"
    )
    for name in names:
        if binary := shutil.which(name):
            return binary
    if os.name == "nt":
        for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if root := os.environ.get(key):
                path = Path(root) / "Google/Chrome/Application/chrome.exe"
                if path.is_file():
                    return str(path)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--url", default="https://www.curseforge.com/minecraft/search?class=mc-mods&sortBy=total+downloads")
    parser.add_argument("--browser-executable", type=Path, help="Absolute path to an installed Chromium-based browser")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    if args.browser_executable is not None:
        if not args.browser_executable.is_absolute() or not args.browser_executable.is_file():
            parser.error("--browser-executable must be an absolute path to an installed browser")
        if os.name == "nt" and args.browser_executable.suffix.lower() != ".exe":
            parser.error("--browser-executable must point to an .exe")
    binary = str(args.browser_executable) if args.browser_executable is not None else chrome_executable()
    if binary:
        command = [binary]
        profile = state_dir() / "normal-chrome-profile"
    elif os.name != "nt" and shutil.which("flatpak") and subprocess.run(
        ["flatpak", "info", "com.google.Chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0:
        command = ["flatpak", "run", "com.google.Chrome"]
        profile = Path.home() / ".var/app/com.google.Chrome/data/mod-curator-browser"
    else:
        parser.error("Install Chrome/Chromium or specify its executable with --browser-executable")
    profile.mkdir(parents=True, exist_ok=True)
    command += [f"--user-data-dir={profile}", f"--remote-debugging-port={args.port}",
                "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check", args.url]
    log = profile / "curator-browser.log"
    options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with log.open("ab") as stream:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, **options)
    print(f"Browser PID: {process.pid}\nProfile: {profile}\nLocal CDP: http://127.0.0.1:{args.port}\nLog: {log}")
    print("Keep this browser open. Complete any visible security check yourself; the plugin never clicks it.")


if __name__ == "__main__":
    main()
