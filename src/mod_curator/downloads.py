from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .models import Cancelled, SourceError


def official_url(url):
    u = urlparse(url)
    host = u.hostname or ""
    if (u.scheme != "https" or u.username or u.password or u.port not in (None, 443)
            or not any(host == h or host.endswith("." + h) for h in ("forgecdn.net", "curseforge.com"))):
        raise SourceError("UNAPPROVED_DOWNLOAD_URL")
    return url


def file_hashes(path):
    hashes = {1: hashlib.sha1(), 2: hashlib.md5()}
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            for h in hashes.values():
                h.update(b)
    return {k: h.hexdigest() for k, h in hashes.items()}


def verify(path, file):
    if path.stat().st_size != file["fileLength"]:
        raise SourceError("LENGTH_MISMATCH")
    hashes = file_hashes(path)
    usable = [h for h in file.get("hashes", []) if h["algo"] in hashes]
    for h in usable:
        if hashes[h["algo"]].lower() != h["value"].lower():
            raise SourceError("HASH_MISMATCH")
    if not zipfile.is_zipfile(path):
        raise SourceError("NOT_A_JAR_ZIP")
    return {"integrity": "platform_hash_verified" if usable else "length_and_zip_only", "hashes": hashes}


async def download_one(client, file, destination: Path, check_cancel=lambda: None):
    name = file["fileName"]
    if (Path(name).name != name or "/" in name or "\\" in name or ":" in name
            or not name.lower().endswith(".jar") or name.startswith(".")):
        raise SourceError("UNSAFE_FILENAME")
    # Hold a directory descriptor: parent symlink swaps cannot redirect writes on POSIX.
    if not hasattr(os, "O_NOFOLLOW") or os.name != "posix":
        raise SourceError("PLATFORM_UNSUPPORTED: secure downloader currently requires POSIX")
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    part = ".mod-curator-" + uuid.uuid4().hex + ".part"
    part_path = Path(f"/proc/self/fd/{fd}") / part if Path("/proc/self/fd").exists() else destination / part
    target = destination / name
    try:
        try:
            existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            try:
                # Never reuse a same-sized but unverifiable file.
                if not any(h.get("algo") in (1, 2) for h in file.get("hashes", [])):
                    raise SourceError("EXISTING_FILE_NO_HASH: manual inspection required")
                stable_path = Path(f"/proc/self/fd/{existing}")
                info = verify(stable_path if stable_path.exists() else target, file)
                return {"status": "already_verified", "path": str(target), **info}
            finally:
                os.close(existing)
        url = file.get("downloadUrl")
        if not url:
            return {"status": "manual_required", "project_id": file["modId"], "file_id": file["id"],
                    "url": f"https://www.curseforge.com/minecraft/mc-mods/{file.get('_slug', str(file['modId']))}/files/{file['id']}",
                    "instruction": "Automatic URL unavailable from this metadata source. Open the official file page and download manually, or export the set to Prism Launcher."}
        for attempt in range(3):
            check_cancel()
            try:
                current = official_url(url)
                for redirect in range(6):
                    check_cancel()
                    async with client.stream("GET", current, follow_redirects=False) as r:
                        if r.is_redirect:
                            from urllib.parse import urljoin
                            current = official_url(urljoin(current, r.headers["location"]))
                            continue
                        r.raise_for_status()
                        raw_fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                        with os.fdopen(raw_fd, "wb") as stream:
                            length = 0
                            async for chunk in r.aiter_bytes():
                                check_cancel()
                                length += len(chunk)
                                if length > file["fileLength"]:
                                    raise SourceError("LENGTH_EXCEEDED")
                                stream.write(chunk)
                            stream.flush()
                            os.fsync(stream.fileno())
                        break
                else:
                    raise SourceError("TOO_MANY_REDIRECTS")
                info = verify(part_path, file)
                check_cancel()
                # Atomic no-clobber publish. A concurrent new target fails with FileExistsError.
                os.link(part, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                os.unlink(part, dir_fd=fd)
                return {"status": "downloaded", "path": str(target), **info}
            except Cancelled:
                raise
            except (httpx.HTTPError, OSError, SourceError) as e:
                try:
                    os.unlink(part, dir_fd=fd)
                except FileNotFoundError:
                    pass
                if attempt == 2 or isinstance(e, FileExistsError):
                    raise SourceError(f"DOWNLOAD_FAILED: {type(e).__name__}: {e}") from e
                await asyncio.sleep(2 ** attempt)
    finally:
        try:
            os.unlink(part, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)
