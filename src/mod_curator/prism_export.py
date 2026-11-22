"""CurseForge manifest v1 export, checked against Prism 11.1.1 importer."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
import zipfile
import asyncio

import httpx

from . import DISPLAY_NAME
from .models import SourceError

LOADER_UIDS = {"NeoForge": "net.neoforged", "Forge": "net.minecraftforge",
               "Fabric": "net.fabricmc.fabric-loader", "Quilt": "org.quiltmc.quilt-loader"}


async def loader_version(filters, requested=None):
    uid = LOADER_UIDS[filters.loader]
    async with httpx.AsyncClient(timeout=40) as client:
        for attempt in range(3):
            root = await client.get("https://meta.prismlauncher.org/v1/index.json")
            root.raise_for_status()
            package = next((p for p in root.json()["packages"] if p["uid"] == uid), None)
            if package is None:
                raise SourceError("PRISM_METADATA_PACKAGE_MISSING")
            r = await client.get(f"https://meta.prismlauncher.org/v1/{uid}/index.json")
            r.raise_for_status()
            index_hash = hashlib.sha256(r.content).hexdigest()
            if index_hash != package.get("sha256"):
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
                    continue
                raise SourceError("PRISM_METADATA_INCONSISTENT: loader index checksum differs from root index; export stays draft")
            candidates = []
            for item in r.json()["versions"]:
                if requested and item["version"] != requested:
                    continue
                if item.get("type") not in ("release", "stable"):
                    continue
                req = [v for v in item.get("requires", []) if v.get("uid") == "net.minecraft"]
                if filters.loader in ("Forge", "NeoForge"):
                    if not any(v.get("equals") == filters.minecraft for v in req):
                        continue
                elif req and not any(v.get("equals") in (None, filters.minecraft) for v in req):
                    continue
                candidates.append(item)
            if not candidates:
                raise SourceError(f"LOADER_VERSION_UNRESOLVED: {filters.loader} for Minecraft {filters.minecraft}")
            chosen = max(candidates, key=lambda v: (v.get("releaseTime", ""), v["version"]))
            detail = await client.get(f"https://meta.prismlauncher.org/v1/{uid}/{chosen['version']}.json")
            detail.raise_for_status()
            detail_hash = hashlib.sha256(detail.content).hexdigest()
            if detail_hash != chosen.get("sha256"):
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
                    continue
                raise SourceError("PRISM_METADATA_INCONSISTENT: concrete loader version checksum mismatch; export stays draft")
            obj = detail.json()
            if obj.get("uid") != uid or obj.get("version") != chosen["version"]:
                raise SourceError("PRISM_METADATA_VERSION_IDENTITY_MISMATCH")
            if any(v.get("equals") != filters.minecraft for v in obj.get("requires", []) if v.get("uid") == "net.minecraft" and v.get("equals")):
                raise SourceError("PRISM_METADATA_MINECRAFT_MISMATCH")
            return {"id": filters.loader.lower() + "-" + chosen["version"], "primary": True,
                    "version": chosen["version"], "uid": uid,
                    "metadata_source": f"https://meta.prismlauncher.org/v1/{uid}/index.json",
                    "metadata_chain_verified": True, "loader_index_sha256": index_hash,
                    "version_metadata_sha256": detail_hash}


def build_manifest(lock, filters, loader, name, version="1.0.0", *, has_verified_external=False):
    if (not lock.get("files") or not lock.get("roots")) and not (
        has_verified_external and not lock.get("files") and not lock.get("roots")
    ):
        raise SourceError("DRAFT_ONLY: nonempty resolved roots and file closure required")
    entries, seen_files = [], set()
    files = {int(pid): file for pid, file in lock["files"].items()}
    for pid, f in files.items():
        if f.get("modId") != pid or type(f.get("id")) is not int or f["id"] <= 0:
            raise SourceError("DRAFT_ONLY: invalid project/file identity")
        if filters.minecraft not in f.get("gameVersions", []) or filters.loader not in f.get("gameVersions", []):
            raise SourceError("DRAFT_ONLY: incompatible file")
        if not f.get("_details_complete", True) or "dependencies" not in f:
            raise SourceError("DRAFT_ONLY: file dependency information incomplete")
        for dep in f["dependencies"]:
            if dep["relationType"] == 3 and dep["modId"] not in files:
                raise SourceError(f"DRAFT_ONLY: required project {dep['modId']} missing from manifest")
            if dep["relationType"] == 3 and dep.get("fileId") and files[dep["modId"]]["id"] != dep["fileId"]:
                raise SourceError("DRAFT_ONLY: contradictory dependency file pin")
        if f["id"] in seen_files:
            raise SourceError("DRAFT_ONLY: duplicate file ID")
        seen_files.add(f["id"])
        entries.append({"projectID": pid, "fileID": f["id"], "required": True})
    if not set(lock["roots"]) <= files.keys():
        raise SourceError("DRAFT_ONLY: unresolved selected roots")
    if not re.fullmatch(re.escape(filters.loader.lower()) + r"-[A-Za-z0-9._+-]+", loader["id"]):
        raise SourceError("DRAFT_ONLY: no concrete loader version")
    return {"minecraft": {"version": filters.minecraft, "modLoaders": [{"id": loader["id"], "primary": True}]},
            "manifestType": "minecraftModpack", "manifestVersion": 1, "name": name,
            "version": version, "author": f"{DISPLAY_NAME} user", "files": entries, "overrides": "overrides"}


def write_pack(manifest, destination: Path, basename, overrides=None):
    from .modrinth import safe_jar_name
    overrides = overrides or {}
    resources = {f'overrides/mods/{safe_jar_name(name)}': path for name, path in overrides.items()}
    expected_files = {"manifest.json", "overrides/", *resources}

    def digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    if not re.fullmatch(r"[a-zA-Z0-9_-]+\.zip", basename):
        raise ValueError("Unsafe archive basename")
    final = destination / basename
    raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    def reuse():
        if final.is_symlink():
            raise SourceError("EXPORT_EXISTS: symlink target refused")
        if not final.is_file():
            raise SourceError("EXPORT_EXISTS: expected a regular ZIP file")
        try:
            with zipfile.ZipFile(final) as z:
                entries = z.infolist()
                if (len(entries) != len(expected_files) or {i.filename for i in entries} != expected_files
                        or any(stat.S_ISLNK(i.external_attr >> 16) for i in entries)
                        or z.getinfo("manifest.json").file_size != len(raw)
                        or z.getinfo("overrides/").file_size != 0
                        or z.read("manifest.json") != raw):
                    raise SourceError("EXPORT_EXISTS: existing archive differs; never overwrite")
                for member, path in resources.items():
                    if z.getinfo(member).file_size != path.stat().st_size:
                        raise SourceError("EXPORT_EXISTS: existing override differs; never overwrite")
                    with z.open(member) as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest(path):
                            raise SourceError("EXPORT_EXISTS: existing override differs; never overwrite")
        except (OSError, zipfile.BadZipFile) as exc:
            raise SourceError("EXPORT_EXISTS: invalid ZIP; never overwrite") from exc
        return {"path": str(final), "sha256": digest(final), "reused": True}

    if final.exists() or final.is_symlink():
        return reuse()
    part = destination / (".curator-export-" + uuid.uuid4().hex + ".part")
    try:
        with part.open("xb") as stream:
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("manifest.json", raw)
                z.writestr("overrides/", "")
                for member, path in resources.items():
                    z.write(path, member, compress_type=zipfile.ZIP_STORED)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                os.rename(part, final)  # Windows rename refuses an existing target.
            else:
                os.link(part, final)  # POSIX rename can overwrite an existing target.
        except FileExistsError:
            return reuse()
    finally:
        part.unlink(missing_ok=True)
    return {"path": str(final), "sha256": digest(final), "reused": False}
