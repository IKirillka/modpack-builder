from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import os
import stat
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .models import SourceError
from .modrinth import safe_jar_name


def official_url(url):
    u = urlparse(url)
    host = u.hostname or ""
    if (u.scheme != "https" or u.username or u.password or u.port not in (None, 443)
            or not any(host == h or host.endswith("." + h) for h in ("forgecdn.net", "curseforge.com"))):
        raise SourceError("UNAPPROVED_DOWNLOAD_URL")
    return url


def verify(stream, file):
    details = os.fstat(stream.fileno())
    if not stat.S_ISREG(details.st_mode):
        raise SourceError("NOT_A_REGULAR_FILE")
    if details.st_size != file["fileLength"]:
        raise SourceError("LENGTH_MISMATCH")
    digests = {1: hashlib.sha1(), 2: hashlib.md5()}
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        for digest in digests.values():
            digest.update(block)
    hashes = {key: digest.hexdigest() for key, digest in digests.items()}
    usable = [h for h in file.get("hashes", []) if h["algo"] in hashes]
    for h in usable:
        if hashes[h["algo"]].lower() != h["value"].lower():
            raise SourceError("HASH_MISMATCH")
    if not zipfile.is_zipfile(stream):
        raise SourceError("NOT_A_JAR_ZIP")
    return {"integrity": "platform_hash_verified" if usable else "length_and_zip_only", "hashes": hashes}


class DownloadDirectory:
    def __init__(self, path, descriptor=None):
        self.path = path
        self.descriptor = descriptor

    def open_read(self, name):
        if self.descriptor is None:
            from .windows_files import open_read
            return open_read(self.path / name)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.descriptor)
        return os.fdopen(fd, "rb")

    def open_partial(self, name):
        if self.descriptor is None:
            return (self.path / name).open("xb")
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=self.descriptor)
        return os.fdopen(fd, "wb")

    def publish(self, part, name):
        if self.descriptor is None:
            # Windows rename is atomic on the same filesystem and refuses an existing target.
            (self.path / part).rename(self.path / name)
        else:
            os.link(part, name, src_dir_fd=self.descriptor, dst_dir_fd=self.descriptor, follow_symlinks=False)

    def discard(self, name):
        if self.descriptor is None:
            (self.path / name).unlink(missing_ok=True)
        else:
            try:
                os.unlink(name, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass


@contextmanager
def download_directory(destination):
    if os.name == "nt":
        from .windows_files import pinned_directory
        with pinned_directory(destination):
            yield DownloadDirectory(destination)
    else:
        fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield DownloadDirectory(destination, fd)
        finally:
            os.close(fd)


async def fetch_partial(client, parent, file, part, check_cancel):
    current = official_url(file["downloadUrl"])
    for _ in range(6):
        check_cancel()
        async with client.stream("GET", current, follow_redirects=False) as response:
            if response.is_redirect:
                current = official_url(urljoin(current, response.headers["location"]))
                continue
            response.raise_for_status()
            with parent.open_partial(part) as stream:
                length = 0
                async for chunk in response.aiter_bytes():
                    check_cancel()
                    length += len(chunk)
                    if length > file["fileLength"]:
                        raise SourceError("LENGTH_EXCEEDED")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            return
    raise SourceError("TOO_MANY_REDIRECTS")


async def download_one(client, file, destination: Path, check_cancel=lambda: None):
    try:
        name = safe_jar_name(file["fileName"])
        if name.startswith("."):
            raise ValueError("hidden filename")
    except ValueError as exc:
        raise SourceError("UNSAFE_FILENAME") from exc
    target = destination / name
    part = ".mod-curator-" + uuid.uuid4().hex + ".part"
    with download_directory(destination) as parent:
        try:
            existing = parent.open_read(name)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            with existing:
                if not any(h.get("algo") in (1, 2) for h in file.get("hashes", [])):
                    raise SourceError("EXISTING_FILE_NO_HASH: manual inspection required")
                return {"status": "already_verified", "path": str(target), **verify(existing, file)}
        if not file.get("downloadUrl"):
            return {"status": "manual_required", "project_id": file["modId"], "file_id": file["id"],
                    "url": f"https://www.curseforge.com/minecraft/mc-mods/{file.get('_slug', str(file['modId']))}/files/{file['id']}",
                    "instruction": "Automatic URL unavailable from this metadata source. Open the official file page and download manually, or export the set to Prism Launcher."}
        for attempt in range(3):
            check_cancel()
            try:
                await fetch_partial(client, parent, file, part, check_cancel)
                with parent.open_read(part) as stream:
                    info = verify(stream, file)
                check_cancel()
                parent.publish(part, name)
                return {"status": "downloaded", "path": str(target), **info}
            except (httpx.HTTPError, OSError, SourceError) as error:
                if attempt == 2 or isinstance(error, FileExistsError):
                    raise SourceError(f"DOWNLOAD_FAILED: {type(error).__name__}: {error}") from error
            finally:
                parent.discard(part)
            await asyncio.sleep(2 ** attempt)
