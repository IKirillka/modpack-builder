#!/usr/bin/env python3
"""Start a dedicated normal local Chrome profile. No challenge-solving or stealth."""
import argparse
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from runtime_paths import state_dir


def chrome_executable():
    names = ("chrome.exe", "chromium.exe", "msedge.exe") if os.name == "nt" else (
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"
    )
    for name in names:
        if binary := shutil.which(name):
            return binary
    if os.name == "nt":
        for suffix in ("Google/Chrome/Application/chrome.exe", "Chromium/Application/chrome.exe",
                       "Microsoft/Edge/Application/msedge.exe"):
            for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                if root := os.environ.get(key):
                    path = Path(root) / suffix
                    if path.is_file():
                        return str(path)
    return None


def cdp_ready(port):
    connection = HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/json/version")
        response = connection.getresponse()
        if response.status != 200:
            return False
        document = json.loads(response.read(32768))
        return isinstance(document, dict) and bool(document.get("webSocketDebuggerUrl"))
    except (OSError, HTTPException, ValueError):
        return False
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--url", default="https://www.curseforge.com/minecraft/search?class=mc-mods&sortBy=total+downloads")
    parser.add_argument("--browser-executable", type=Path, help="Absolute path to an installed Chromium-based browser")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    address = f"http://127.0.0.1:{args.port}"
    if cdp_ready(args.port):
        print(f"Browser CDP already available: {address}. Use the open browser window.")
        return
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
        parser.error("Install Chrome/Chromium/Edge or specify its executable with --browser-executable")
    if args.port != 9222:
        profile = profile.with_name(f"{profile.name}-{args.port}")
    profile.mkdir(parents=True, exist_ok=True)
    command += [f"--user-data-dir={profile}", f"--remote-debugging-port={args.port}",
                "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check", args.url]
    log = profile / "curator-browser.log"
    options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with log.open("ab") as stream:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, **options)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if cdp_ready(args.port):
            print(f"Browser PID: {process.pid}\nProfile: {profile}\nLocal CDP: {address}\nLog: {log}")
            print("Keep this browser open. Complete any visible security check yourself.")
            return
        if process.poll() not in (None, 0):
            break
        time.sleep(0.25)
    parser.exit(1, f"Browser CDP did not start at {address}. See {log} and retry with a free port.\n")


if __name__ == "__main__":
    main()
