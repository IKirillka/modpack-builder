"""Source-independent finite dependency solver, with backtracking and cycles."""
from __future__ import annotations

from .models import SourceError
from .jar_validation import compare, compare_maven


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
        if not isinstance(version, str):
            return None
        comparator = compare_maven if isinstance(expression, str) and expression.startswith(('[', '(')) else compare
        return comparator(version, expression)
    return True


def relation_evidence(pid, file, dep, path):
    return {"chain": [*path, dep["modId"]], "project_id": pid, "file_id": file["id"],
            "dependency_id": dep["modId"], "required_file": dep.get("fileId"),
            "version_range": version_range(dep),
            "source": dep.get('evidence', {}).get('url') or file.get("_dependencies_source") or
                      f"https://api.curseforge.com/v1/mods/{pid}/files/{file['id']}",
            "evidence": dep.get('evidence')}


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


async def resolve(source, roots, filters, check_cancel=lambda: None, pins=None, excluded=(), *,
                  max_states=100_000, progress=None, evidence=None):
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

    root_ids = set(roots)
    excluded = set(excluded)
    if excluded & root_ids:
        raise SourceError("Selected roots are manually excluded")

    async def candidates(pid):
        if pid in unavailable:
            raise unavailable[pid]
        if pid not in cache:
            check_cancel()
            try:
                if pid in pins and callable(getattr(source, 'exact_file', None)):
                    item = await source.exact_file(pid, pins[pid], filters)
                    files = [item] if item else []
                else:
                    files = await source.files(pid, filters)
            except SourceError as exc:
                unavailable[pid] = exc
                raise
            cache[pid] = sorted((f for f in files if compatible(f, pid, filters)),
                                key=lambda f: (f.get("fileDate", ""), f["id"]), reverse=True)
        return cache[pid]

    # Explicit DFS stack avoids Python's recursion limit for large mod sets.
    stack = [({}, root_ids, {}, {pid: [pid] for pid in roots}, 0)]
    chosen_result = None
    states = 0
    rejected_files = set()
    evidence = evidence or {}

    def reject_parent(pid, chosen, paths):
        if len(paths[pid]) < 2:
            return
        owner = paths[pid][-2]
        parent = chosen.get(owner)
        if not parent:
            return
        if not any(dep['modId'] == pid and dep['relationType'] == 3 and not dep.get('evidence', {}).get('when_project_id')
                   for dep in parent.get('dependencies', ())):
            return
        rejected_files.add((owner, parent['id']))
        if owner in root_ids and all((owner, item['id']) in rejected_files for item in cache[owner]
                                     if owner not in pins or item['id'] == pins[owner]):
            raise ResolutionError(failures)
    while stack:
        check_cancel()
        chosen, required, constraints, paths, cursor = stack.pop()
        if any((pid, file['id']) in rejected_files for pid, file in chosen.items()):
            continue
        pending = required - chosen.keys()
        if not pending:
            # A condition can become true after the owning file was chosen.
            # Extend the candidate graph before accepting this branch.
            expanded = False
            invalid = False
            for owner, file in list(chosen.items()):
                rules = evidence.get((owner, file['id']), ())
                new_rules = [r for r in rules if r.get('when_project_id') in required and
                             r not in file.get('_dependency_evidence', ())]
                if not new_rules:
                    continue
                from .plan_evidence import apply_evidence
                effective, conflicts = apply_evidence(file, new_rules, proposed_ids=required)
                effective['_dependency_evidence'] = [*file.get('_dependency_evidence', ()), *new_rules]
                effective['_platform_dependencies'] = file.get('_platform_dependencies', file['dependencies'])
                chosen = {**chosen, owner: effective}
                for conflict in conflicts:
                    fail('metadata_conflict', conflict['reason'], project_id=owner, file_id=file['id'],
                         chain=paths[owner], evidence=conflict)
                    invalid = True
                for dep in effective['dependencies'][len(file['dependencies']):]:
                    did = dep['modId']
                    edge = relation_evidence(owner, file, dep, paths[owner])
                    if did in excluded or (did in chosen and match_relation(chosen[did], dep) is not True):
                        fail('conditional_dependency_conflict', requirement_label(edge), **edge)
                        invalid = True
                    elif did not in chosen:
                        required, paths, constraints = required | {did}, {**paths, did: edge['chain']}, {
                            **constraints, did: [*constraints.get(did, ()), (dep, edge)]}
                        expanded = True
            if invalid:
                continue
            if expanded:
                stack.append((chosen, required, constraints, paths, 0))
                continue
            chosen_result = chosen
            break
        # Keep the same project for an alternate cursor. Candidate dependence is
        # checked before starting the next unrelated root.
        pid = min(pending, key=lambda item: (item in root_ids, item))
        states += 1
        if progress:
            progress(pid, paths[pid], states, phase='resolving', file_id=None)
        if states > max_states:
            raise ResolutionError([{"code": "search_budget_exhausted", "message":
                                   f"Search budget of {max_states} states exhausted near project {pid}",
                                   "project_id": pid, "chain": paths[pid], "states": states}])
        try:
            files = await candidates(pid)
        except SourceError as exc:
            fail("source_unavailable", str(exc), project_id=pid, chain=paths[pid])
            if pid in root_ids:
                raise ResolutionError(failures) from exc
            reject_parent(pid, chosen, paths)
            continue
        for index, f in enumerate(files[cursor:], cursor):
            if (pid in pins and f["id"] != pins[pid]) or (pid, f["id"]) in rejected_files:
                continue
            if hasattr(source, 'note_file'):
                source.note_file(pid, f['id'])
            if not f.get("_details_complete", True):
                try:
                    f = await source.complete_file(f)
                except SourceError as exc:
                    fail("source_unavailable", str(exc), project_id=pid, file_id=f["id"], chain=paths[pid])
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
            if rules := evidence.get((pid, f['id'])):
                from .plan_evidence import apply_evidence
                f, conflicts = apply_evidence(f, rules, proposed_ids=required)
                if conflicts:
                    for conflict in conflicts:
                        fail("metadata_conflict", conflict['reason'], project_id=pid,
                             file_id=f["id"], chain=paths[pid], evidence=conflict)
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
            missing_pin = pid in pins and not any(f["id"] == pins[pid] for f in files)
            fail("exact_file_unavailable" if missing_pin else "no_compatible_file",
                 f"No consistent compatible file for project {pid}"
                 + (f" with fixed file ID {pins[pid]}" if pid in pins else ""),
                 project_id=pid, chain=paths[pid], required_file=pins.get(pid),
                 compatible_files=[{"file_id": f["id"], "version": declared_version(f)} for f in files[:10]],
                 requirements=[edge for _, edge in constraints.get(pid, [])])
            # Other file choices cannot make an absent root file appear.
            # A transitive dependency may disappear when its parent changes.
            impossible = not files or missing_pin or all((pid, f['id']) in rejected_files for f in files
                                                           if pid not in pins or f['id'] == pins[pid])
            if impossible:
                if pid in root_ids:
                    raise ResolutionError(failures)
                reject_parent(pid, chosen, paths)

    chosen = chosen_result
    if chosen is None:
        raise ResolutionError(failures)
    optional = [{"from": pid, **d} for pid, f in chosen.items() for d in f.get("dependencies", []) if d["relationType"] == 2]
    alternatives = []
    for pid, selected in chosen.items():
        first = cache[pid][0]
        if selected['id'] != first['id']:
            related = [x for x in failures if x.get('project_id') == pid or pid in x.get('chain', ())]
            alternatives.append({'project_id': pid, 'preferred_file': first['id'], 'selected_file': selected['id'],
                                 'basis': 'pinned' if pid in pins else 'compatible_dependency_solution',
                                 'issues': related[:8]})
    return {"files": {str(k): v for k, v in chosen.items()}, "roots": roots, "alternatives": alternatives,
            "search_states": states,
            "optional_not_installed": optional,
            "constraint_coverage": "CurseForge exact-file project relations; JAR version ranges are not part of this metadata",
            "compatibility_extension": "Future providers/JAR metadata validators must run before marking a pack compatible"}
