"""Local, read-only audits of saved catalogue pages and an explicitly named Prism instance.

Only reports/state in the application's service folder are written. No game is launched.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import tomllib
import zipfile

from .jar_validation import validate_pack
from .models import Filters
from .source import parse_catalog


def audit_collection(store, job_id):
    job = store.get(job_id)
    filters = Filters.model_validate(job["filters"])
    pages, extracted, selected, seen, issues = [], [], [], set(), []
    previous = None
    boundary = None
    if [p["number"] for p in job["pages"]] != list(range(1, len(job["pages"]) + 1)):
        issues.append("Captured catalogue pages are not consecutive from page 1")
    for saved in job["pages"]:
        number = saved["number"]
        file = store.job_dir(job_id) / f"catalog-{number}.html"
        parsed = parse_catalog(file.read_text(), saved["url"], filters, number)
        ids = [p["id"] for p in parsed.cards]
        downloads = [p["downloads"] for p in parsed.cards]
        if ids != saved["ids"] or downloads != saved["downloads"]:
            issues.append(f"Page {number}: saved state differs from captured HTML")
        accepted = []
        for card in parsed.cards:
            if card["id"] in seen:
                issues.append(f"Duplicate project {card['id']}")
            seen.add(card["id"])
            if previous is not None and card["downloads"] > previous:
                issues.append("Observed order is not descending")
            previous = card["downloads"]
            extracted.append(card)
            if card["downloads"] > filters.minimum_downloads:
                accepted.append(card["id"])
                selected.append(card["id"])
            elif boundary is None:
                boundary = {k: card[k] for k in ("id", "name", "downloads", "page", "position")}
        pages.append({"number": number, "url": saved["url"], "cards": len(parsed.cards),
                      "accepted": len(accepted), "accepted_ids": accepted,
                      "cards_detail": parsed.cards, "evidence": parsed.evidence})
    if selected != job["order"]:
        issues.append("An eligible captured project is missing/reordered in the collected list")
    if boundary and job.get("collection_reason", job["reason"]) != "threshold":
        issues.append("Stop reason differs from observed boundary")
    result = {"job_id": job_id, "audited_at": time.time(), "filters": filters.model_dump(),
              "page_size": filters.page_size, "pages": pages, "extracted_cards": len(extracted),
              "collected_projects": len(selected), "exact_counter_field": "tileProjects[].downloadsCount",
              "first_excluded": boundary, "stop_reason": job.get("collection_reason", job["reason"]),
              "no_omissions_in_captured_pages": not issues, "issues": issues,
              "catalog_total_at_capture": job["pages"][0]["total"] if pages else None,
              "scope": "Verified against captured HTML/DOM and SQLite, not a frozen snapshot of unseen later pages",
              "global_snapshot_guaranteed": False}
    path = store.job_dir(job_id) / "collection-audit.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    store.patch(job_id, collection_audit=result)
    return result


def audit_prism(store, job_id, instance, log_file=None):
    job = store.get(job_id)
    filters = Filters.model_validate(job["filters"])
    instance = Path(instance).resolve(strict=True)
    archive = Path(job["pack_export"]["path"])
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("manifest.json"))
    imported = json.loads((instance / "flame/manifest.json").read_text())
    if imported != manifest:
        raise ValueError("This instance's manifest is different; refusing to attribute it to the job")
    components = {v["uid"]: v["version"] for v in json.loads((instance / "mmc-pack.json").read_text())["components"] if not v.get("disabled")}
    expected_loader = job["pack_export"]["loader"]
    errors = []
    if components.get("net.minecraft") != filters.minecraft or components.get(expected_loader["uid"]) != expected_loader["version"]:
        errors.append("Minecraft/loader components differ from the exported manifest")
    mods_dir = instance / "minecraft/mods"
    index = {}
    for file in (mods_dir / ".index").glob("*.pw.toml"):
        obj = tomllib.loads(file.read_text())
        cf = obj.get("update", {}).get("curseforge")
        if cf:
            key = (cf["project-id"], cf["file-id"])
            if key in index:
                errors.append(f"Duplicate Prism index entry: {key}")
            index[key] = obj
    verified, paths = [], []
    for entry in manifest["files"]:
        pid, fid = entry["projectID"], entry["fileID"]
        item = index.get((pid, fid))
        if item is None:
            errors.append(f"Missing exact project/file Prism index entry: {pid}/{fid}")
            continue
        name = item["filename"]
        if Path(name).name != name or "\\" in name:
            errors.append("Unsafe index filename")
            continue
        file = mods_dir / name
        if not file.is_file() or file.is_symlink():
            errors.append(f"Missing regular JAR: {name}")
            continue
        lock = job["lock"]["files"][str(pid)]
        algo, expected = item["download"]["hash-format"], item["download"]["hash"]
        if algo not in ("sha1", "sha256", "sha512", "md5"):
            errors.append(f"Unsupported platform hash: {algo}")
            continue
        digest = hashlib.new(algo)
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        correct = actual.lower() == expected.lower() and file.stat().st_size == lock["fileLength"] and name == lock["fileName"]
        if not correct:
            errors.append(f"File content/length/name mismatch: {pid}/{fid}")
        for dependency in item.get("x-prismlauncher-dependencies", []):
            if dependency.get("type") == "REQUIRED" and int(dependency["addonId"]) not in {f["projectID"] for f in manifest["files"]}:
                errors.append("Prism required dependency missing from manifest")
        verified.append({"project_id": pid, "file_id": fid, "file_name": name, "size": file.stat().st_size,
                         "hash_algorithm": algo, "expected_hash": expected, "actual_hash": actual, "verified": correct})
        paths.append(file)
    expected_names = {f["file_name"] for f in verified}
    unexpected = sorted(p.name for p in mods_dir.glob("*.jar") if p.name not in expected_names)
    if unexpected:
        errors.append(f"Unexpected top-level JARs outside manifest: {unexpected}")
    loader_key = {"NeoForge": "neoforge", "Forge": "forge", "Fabric": "fabricloader", "Quilt": "quilt_loader"}[filters.loader]
    platform_versions = {loader_key: components[expected_loader["uid"]]} if expected_loader["uid"] in components else {}
    jar_report = validate_pack(paths, filters, platform_versions)
    errors.extend(jar_report["errors"] + jar_report["unresolved"])
    # A Prism package requires a complete launcher component graph, independently of mod dependencies.
    missing_components = []
    meta_root = instance.parent.parent / "meta"
    for uid, version in components.items():
        meta = meta_root / uid / (version + ".json")
        if not meta.exists():
            missing_components.append(f"Metadata unavailable: {uid}/{version}")
            continue
        for requirement in json.loads(meta.read_text()).get("requires", []):
            found = components.get(requirement["uid"])
            if found is None or (requirement.get("equals") and found != requirement["equals"]):
                missing_components.append(f"{uid} requires {requirement['uid']} {requirement.get('equals') or requirement.get('suggests') or '*'}")
    errors.extend(missing_components)
    log_evidence = []
    if log_file:
        for line in Path(log_file).read_text(errors="replace").splitlines():
            if any(k in line for k in ("Will download", "Request succeeded: \"https://api.curseforge.com", instance.name, "Checksum mismatch", "org.lwjgl3", "Couldn't open")):
                log_evidence.append(line)
    result = {"job_id": job_id, "audited_at": time.time(), "instance": str(instance),
              "manifest_matches_export": True, "components": components, "files": verified,
              "prism_component_issues": missing_components, "jar_dependencies": jar_report,
              "manual_downloads_pending": [] if len(verified) == len(manifest["files"]) and all(v["verified"] for v in verified) else "unresolved",
              "verified": not errors, "errors": errors, "unexpected_jars": unexpected,
              "minecraft_launched_by_audit": False, "runtime_tested": False, "log_evidence": log_evidence}
    (store.job_dir(job_id) / "prism-import-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    store.patch(job_id, prism_import_verification=result)
    store.mutate(job_id, lambda s: s["pack_export"].update(prism_import_tested=True, prism_import_verification=result,
                      manual_downloads=result["manual_downloads_pending"]))
    return result
