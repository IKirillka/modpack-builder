"""Source-independent finite dependency solver, with backtracking and cycles."""
from __future__ import annotations

from .models import SourceError
from .jar_validation import compare


class ResolutionError(SourceError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("DEPENDENCIES_UNRESOLVED: " + "; ".join(dict.fromkeys(i["message"] for i in issues[:8])))


def version_range(dep):
    return dep.get("versionRange") or dep.get("versionConstraint")


def declared_version(file):
    return file.get("versionNumber") or file.get("version")


def match_relation(file, dep):
    """Check only explicit version metadata, without guessing from display names."""
    if dep.get("fileId") and file["id"] != dep["fileId"]:
        return False
    if expression := version_range(dep):
        version = declared_version(file)
        if isinstance(expression, str):
            expression = expression.replace('≥', '>=').replace('≤', '<=')
        return compare(version, expression) if isinstance(version, str) else None
    return True


def relation_evidence(pid, file, dep, path):
    return {"chain": [*path, dep["modId"]], "project_id": pid, "file_id": file["id"],
            "dependency_id": dep["modId"], "required_file": dep.get("fileId"),
            "version_range": version_range(dep),
            "source": file.get("_dependencies_source") or f"https://api.curseforge.com/v1/mods/{pid}/files/{file['id']}"}


def requirement_label(edge):
    return (f"requires project {edge['dependency_id']}"
            + (f" file {edge['required_file']}" if edge["required_file"] else "")
            + (f" version {edge['version_range']}" if edge["version_range"] else ""))


def compatible(file, project_id, filters):
    tags = file.get("gameVersions", [])
    return (file.get("modId") == project_id and file.get("isAvailable") is True
            and filters.minecraft in tags and filters.loader in tags
            and not file.get("isServerPack", False)
            and not file.get("fileName", "").lower().endswith(("-dev.jar", "-sources.jar", "-javadoc.jar"))
            and (file.get("releaseType") in filters.release_types if filters.release_types is not None
                 else filters.allow_prerelease or file.get("releaseType") == 1)
            and file.get("fileName", "").lower().endswith(".jar"))


async def resolve(source, roots, filters, check_cancel=lambda: None, pins=None, excluded=()):
    cache = {}
    unavailable = {}
    pins = {int(k): int(v) for k, v in (pins or {}).items()}
    failures = []
    seen = set()

    def fail(code, message, **details):
        key = (code, message, tuple(details.get("chain", [])))
        if key not in seen:
            seen.add(key)
            failures.append({"code": code, "message": message, **details})

    excluded = set(excluded)
    if excluded & set(roots):
        raise SourceError("Selected roots are manually excluded")

    async def candidates(pid):
        if pid in unavailable:
            raise unavailable[pid]
        if pid not in cache:
            check_cancel()
            try:
                files = await source.files(pid, filters)
            except SourceError as exc:
                unavailable[pid] = exc
                raise
            cache[pid] = sorted((f for f in files if compatible(f, pid, filters)),
                                key=lambda f: (f.get("fileDate", ""), f["id"]), reverse=True)
        return cache[pid]

    # Explicit DFS stack avoids Python's recursion limit for large mod sets.
    stack = [({}, set(roots), {}, {pid: [pid] for pid in roots}, 0)]
    chosen_result = None
    while stack:
        check_cancel()
        chosen, required, constraints, paths, cursor = stack.pop()
        pending = sorted(required - chosen.keys())
        if not pending:
            chosen_result = chosen
            break
        pid = pending[0]
        try:
            files = await candidates(pid)
        except SourceError as exc:
            fail("file_metadata_unavailable", str(exc), project_id=pid, chain=paths[pid])
            continue
        for index, f in enumerate(files[cursor:], cursor):
            if pid in pins and f["id"] != pins[pid]:
                continue
            if not f.get("_details_complete", True):
                try:
                    f = await source.complete_file(f)
                except SourceError as exc:
                    fail("file_metadata_unavailable", str(exc), project_id=pid, file_id=f["id"], chain=paths[pid])
                    continue
                if not compatible(f, pid, filters):
                    raise SourceError("FILE_CHANGED_DURING_RESOLUTION")
            new_required, new_constraints, new_paths = set(required), dict(constraints), dict(paths)
            invalid = False
            if not isinstance(f.get("dependencies"), list):
                fail("file_relations_unknown", f"File {f['id']} lacks explicit dependency metadata",
                     project_id=pid, file_id=f["id"], chain=paths[pid])
                continue
            if f.get("versionConstraints"):
                fail("unparsed_constraints", f"File {f['id']} has unsupported dependency constraints",
                     project_id=pid, file_id=f["id"], chain=paths[pid])
                continue
            for dep, edge in constraints.get(pid, []):
                outcome = match_relation(f, dep)
                if outcome is not True:
                    fail("version_unverified" if outcome is None else "version_conflict",
                         f"Project {edge['project_id']} {requirement_label(edge)}; candidate file {f['id']} "
                         f"has version {declared_version(f) or 'unavailable in metadata'}", **edge,
                         candidate_file=f["id"], candidate_version=declared_version(f))
                    invalid = True
            proposed = {**chosen, pid: f}
            for dep in f.get("dependencies", []):
                did, rel = dep["modId"], dep["relationType"]
                if rel == 3:
                    edge = relation_evidence(pid, f, dep, paths[pid])
                    if did in excluded:
                        fail("manual_exclusion", f'Project {pid}, file {f["id"]}, requires manually excluded project {did}', **edge)
                        invalid = True
                    new_required.add(did)
                    new_paths.setdefault(did, edge["chain"])
                    new_constraints[did] = [*new_constraints.get(did, []), (dep, edge)]
                    outcome = match_relation(proposed[did], dep) if did in proposed else True
                    if outcome is not True:
                        candidate = proposed[did]
                        fail("version_unverified" if outcome is None else "version_conflict",
                             f"Project {pid} {requirement_label(edge)}; candidate file "
                             f"{candidate['id']} has version {declared_version(candidate) or 'unavailable in metadata'}",
                             **edge, candidate_file=candidate["id"], candidate_version=declared_version(candidate))
                        invalid = True
            pairs = [(pid, f, dep) for dep in f.get("dependencies", []) if dep["relationType"] == 5]
            pairs += [(other_id, other, dep) for other_id, other in chosen.items()
                      for dep in other.get("dependencies", []) if dep["relationType"] == 5 and dep["modId"] == pid]
            for owner, file, dep in pairs:
                target = proposed.get(dep["modId"])
                if target and match_relation(target, dep) is not False:
                    edge = relation_evidence(owner, file, dep, new_paths[owner])
                    fail("declared_incompatibility", f"File {file['id']} declares an incompatibility with "
                         f"project {dep['modId']} (candidate file {target['id']})", **edge, candidate_file=target["id"])
                    invalid = True
            if invalid:
                continue
            if index + 1 < len(files) and pid not in pins:
                stack.append((chosen, required, constraints, paths, index + 1))
            stack.append((proposed, new_required, new_constraints, new_paths, 0))
            break
        else:
            fail("exact_file_unavailable" if pid in pins and not any(f["id"] == pins[pid] for f in files)
                 else "no_compatible_file", f"No consistent compatible file for project {pid}"
                 + (f" with fixed file ID {pins[pid]}" if pid in pins else ""),
                 project_id=pid, chain=paths[pid], required_file=pins.get(pid),
                 compatible_files=[{"file_id": f["id"], "version": declared_version(f)} for f in files[:10]],
                 requirements=[edge for _, edge in constraints.get(pid, [])])

    chosen = chosen_result
    if chosen is None:
        raise ResolutionError(failures)
    optional = [{"from": pid, **d} for pid, f in chosen.items() for d in f.get("dependencies", []) if d["relationType"] == 2]
    return {"files": {str(k): v for k, v in chosen.items()}, "roots": roots,
            "optional_not_installed": optional,
            "constraint_coverage": "CurseForge exact-file project relations; JAR version ranges are not part of this metadata",
            "compatibility_extension": "Future providers/JAR metadata validators must run before marking a pack compatible"}
