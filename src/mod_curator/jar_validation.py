"""Read-only mandatory dependency/version checks; never executes or extracts a JAR.

The catalogue API cannot express these ranges. Unknown expressions remain unresolved.
This is a validator, not a Minecraft runtime or a complete mod conflict detector.
"""
from __future__ import annotations

import io
import json
import re
import tomllib
import zipfile

from packaging.version import InvalidVersion, Version

METADATA_LIMIT = 2 * 1024 * 1024
NESTED_LIMIT = 128 * 1024 * 1024
TOTAL_NESTED_LIMIT = 256 * 1024 * 1024


def compare(version, expression):
    """Return bool or None (unrecognized version/range), never approximate unknowns."""
    try:
        version = Version(version)
        if isinstance(expression, list):
            values = [compare(str(version), x) for x in expression]
            return True if True in values else (None if None in values else False)
        s = expression.strip()
        if s in ("", "*", "[0,)"):
            return True
        if "||" in s:
            return compare(str(version), [p.strip() for p in s.split("||")])
        # Maven intervals/unions, including exact singleton [1.2].
        if s.startswith(("[", "(")):
            intervals = re.findall(r"[\[(][^\[\]()]*[\])]", s)
            if not intervals or ",".join(intervals) != s:
                return None
            results = []
            for interval in intervals:
                core = interval[1:-1]
                if "," not in core:
                    results.append(interval[0] == "[" and interval[-1] == "]" and version == Version(core))
                    continue
                lower, upper = (p.strip() for p in core.split(",", 1))
                ok = (not lower or (version >= Version(lower) if interval[0] == "[" else version > Version(lower)))
                ok = ok and (not upper or (version <= Version(upper) if interval[-1] == "]" else version < Version(upper)))
                results.append(ok)
            return any(results)
        if s.startswith(("~", "^")):
            low = Version(s[1:])
            release = list(low.release) + [0, 0]
            if s[0] == "~":
                high = Version(f"{release[0] + 1}.0.0" if len(low.release) == 1 else f"{release[0]}.{release[1] + 1}.0")
            elif release[0]:
                high = Version(f"{release[0] + 1}.0.0")
            elif release[1]:
                high = Version(f"0.{release[1] + 1}.0")
            else:
                high = Version(f"0.0.{release[2] + 1}")
            return low <= version < high
        parts = s.split()
        if len(parts) > 1:
            values = [compare(str(version), p) for p in parts]
            return False if False in values else (None if None in values else True)
        m = re.fullmatch(r"(>=|<=|>|<|=)?(.+)", s)
        op, bound = m.groups()
        if any(p.lower() in ("x", "*") for p in bound.split(".")):
            if op not in (None, "="):
                return None
            prefix = []
            wildcard = False
            for p in bound.split("."):
                if p.lower() in ("x", "*"):
                    wildcard = True
                elif wildcard:
                    return None
                else:
                    prefix.append(int(p))
            return tuple(prefix) == version.release[:len(prefix)]
        target = Version(bound)
        return {None: version == target, "=": version == target, ">=": version >= target,
                "<=": version <= target, ">": version > target, "<": version < target}[op]
    except (InvalidVersion, ValueError, TypeError, AttributeError):
        return None


def compare_maven(version, expression):
    from .maven_version import compare as compare_tokens, parse as parse_tokens

    if not isinstance(version, str) or '${' in version or not isinstance(expression, str):
        return None
    expr = expression.strip()
    if expr in ('', '*', '[0,)'):
        return True
    if not expr.startswith(('[', '(')):
        # A bare Maven version is a recommendation, not an exact [version] constraint.
        return True if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]*', expr) else None
    intervals = re.findall(r'[\[(][^\[\]()]*[\])]', expr)
    if not intervals or re.sub(r'\s+', '', expr) != ','.join(re.sub(r'\s+', '', p) for p in intervals):
        return None
    outcomes = []
    value = parse_tokens(version)
    for interval in intervals:
        core = interval[1:-1]
        if ',' not in core:
            target = core.strip()
            key = parse_tokens(target)
            outcomes.append(None if interval[0] != '[' or interval[-1] != ']' else
                            True if target == version else (compare_tokens(value, key) == 0 if value is not None and key is not None else None))
            continue
        lo, hi = (part.strip() for part in core.split(',', 1))
        bounds = []
        for bound, lower, closed in ((lo, True, interval[0] == '['), (hi, False, interval[-1] == ']')):
            if not bound:
                continue
            other = parse_tokens(bound)
            if bound == version:
                bounds.append(closed)
            elif value is None or other is None:
                bounds.append(None)
            else:
                order = compare_tokens(value, other)
                bounds.append(order == 0 and closed or (order > 0 if lower else order < 0))
        outcomes.append(False if False in bounds else None if None in bounds else True)
    return True if True in outcomes else (None if None in outcomes else False)


def read_metadata(path, loader, depth=0, allow_library=False, *, budget=None):
    if depth > 8:
        raise ValueError("JAR_METADATA_LIMIT: nested JAR depth exceeds 8")
    if budget is None:
        budget = [TOTAL_NESTED_LIMIT]
    mods, deps, nested = [], [], []
    with zipfile.ZipFile(path) as z:
        def read(name, limit=METADATA_LIMIT):
            if z.getinfo(name).file_size > limit:
                raise ValueError(f"JAR_METADATA_LIMIT: {name} exceeds {limit} bytes")
            return z.read(name)
        known = {"fabric.mod.json", "quilt.mod.json", "META-INF/mods.toml", "META-INF/neoforge.mods.toml"}
        if allow_library and not known.intersection(z.namelist()):
            return [], []  # embedded Java libraries need not declare a Minecraft mod
        if loader in ("Fabric", "Quilt"):
            name = "fabric.mod.json" if loader == "Fabric" else "quilt.mod.json"
            if name not in z.namelist():
                raise ValueError(f"JAR_METADATA_MISSING: {name}")
            obj = json.loads(read(name))
            if loader == "Fabric":
                mods.append({"id": obj["id"], "version": obj["version"]})
                for alias in obj.get("provides", []):
                    mods.append({"id": alias, "version": obj["version"]})
                deps.extend({"owner": obj["id"], "id": key, "range": value} for key, value in obj.get("depends", {}).items())
                nested = [j["file"] for j in obj.get("jars", [])]
            else:
                q = obj["quilt_loader"]
                mods.append({"id": q["id"], "version": q["version"]})
                for alias in q.get("provides", []):
                    mods.append({"id": alias if isinstance(alias, str) else alias["id"], "version": alias.get("version", q["version"]) if isinstance(alias, dict) else q["version"]})
                for dep in q.get("depends", []):
                    if isinstance(dep, list) or not isinstance(dep, dict):
                        raise ValueError("UNSUPPORTED_QUILT_DEPENDENCY_ALTERNATIVES")
                    if not dep.get("optional"):
                        deps.append({"owner": q["id"], "id": dep["id"], "range": dep.get("versions", "*")})
                nested = q.get("jars", [])
        else:
            name = "META-INF/neoforge.mods.toml" if loader == "NeoForge" else "META-INF/mods.toml"
            if loader == "NeoForge" and name not in z.namelist() and "META-INF/mods.toml" in z.namelist():
                name = "META-INF/mods.toml"  # older NeoForge metadata format
            manifest = read("META-INF/MANIFEST.MF").decode(errors="replace") if "META-INF/MANIFEST.MF" in z.namelist() else ""
            if name not in z.namelist():
                if re.search(r"^FMLModType:\s*(?:GAMELIBRARY|LIBRARY|LANGPROVIDER)\s*$", manifest, re.M):
                    return [], []
                raise ValueError(f"JAR_METADATA_MISSING: {name}")
            obj = tomllib.loads(read(name).decode())
            match = re.search(r"^Implementation-Version:\s*(.+)$", manifest, re.M)
            for mod in obj.get("mods", []):
                version = mod["version"]
                if version == "${file.jarVersion}" and match:
                    version = match[1].strip()
                mods.append({"id": mod["modId"], "version": version})
            for owner, values in obj.get("dependencies", {}).items():
                for dep in values:
                    kind = dep.get("type", "required" if dep.get("mandatory", True) else "optional")
                    if kind in ("required", "optional", "incompatible", "discouraged"):
                        deps.append({"owner": owner, "id": dep["modId"], "range": dep.get("versionRange", "*"),
                                     "kind": kind, "side": dep.get("side", "BOTH"),
                                     "metadata_path": name, "dialect": "maven"})
            if "META-INF/jarjar/metadata.json" in z.namelist():
                nested = json.loads(read("META-INF/jarjar/metadata.json")).get("jars", [])
                for entry in nested:
                    if not isinstance(entry.get('path'), str):
                        raise ValueError('JARJAR_PATH_INVALID')
        for entry in nested:
            name = entry['path'] if isinstance(entry, dict) else entry
            size = z.getinfo(name).file_size
            if size > budget[0]:
                raise ValueError("JAR_METADATA_LIMIT: total expanded nested JAR size")
            budget[0] -= size
            child = read_metadata(io.BytesIO(read(name, NESTED_LIMIT)), loader, depth + 1,
                                  allow_library=True, budget=budget)
            mods.extend({**mod, "nested": f"{name}/{mod['nested']}" if mod.get('nested') else name,
                         "jarjar": mod.get("jarjar") or (entry if isinstance(entry, dict) else None)}
                        for mod in child[0])
            deps.extend({**dep, "nested": f"{name}/{dep['nested']}" if dep.get('nested') else name}
                        for dep in child[1])
    if not mods:
        raise ValueError("JAR_METADATA_MISSING: no declared mods")
    return mods, deps


def embedded_winner(candidates):
    from functools import cmp_to_key
    from .maven_version import compare as cmp, parse

    identity = None
    choices, ranges = [], []
    for candidate in candidates:
        spec = candidate.get('jarjar') or {}
        ident = spec.get('identifier') or {}
        coords = (ident.get('group'), ident.get('artifact'))
        version = spec.get('version') or {}
        label, supported = version.get('artifactVersion'), version.get('range')
        if None in coords or (identity and identity != coords) or not isinstance(label, str) or not isinstance(supported, str):
            return None
        key = parse(label)
        if key is None:
            return None
        identity = coords
        choices.append((key, label, candidate))
        ranges.append(supported)
    compatible = [choice for choice in choices if all(compare_maven(choice[1], r) is True for r in ranges)]
    if compatible:
        winner = max(compatible, key=cmp_to_key(lambda a, b: cmp(a[0], b[0])))
        if sum(cmp(item[0], winner[0]) == 0 and item[2]['version'] != winner[2]['version'] for item in compatible) == 0:
            return winner[2]
    return None

def validate_pack(paths, filters, platform_versions=None):
    from collections import defaultdict
    providers, deps, errors, unresolved, notes, evaluated = defaultdict(list), [], [], [], [], []
    for path in paths:
        try:
            entries, requirements = read_metadata(path, filters.loader)
            providers_from_path = [{**entry, 'jar': path.name} for entry in entries]
            for entry in providers_from_path:
                providers[entry['id']].append(entry)
            deps.extend({**dep, 'jar': path.name} for dep in requirements)
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
            unresolved.append(f"{path.name}: {exc}")
    mods = {"minecraft": filters.minecraft, **(platform_versions or {})}
    for mod_id, candidates in providers.items():
        top = [item for item in candidates if not item.get('nested')]
        selected = top or candidates
        versions = {item['version'] for item in selected}
        if len(top) > 1 and len(versions) > 1:
            errors.append(f"DUPLICATE_MOD_VERSION: {mod_id}: {sorted(versions)}")
        elif len(versions) > 1:
            winner = embedded_winner(selected)
            if winner:
                mods[mod_id] = winner['version']
                notes.append(f"EMBEDDED_MOD_SELECTION: {mod_id}: {winner['version']} from {winner['jar']}")
            else:
                unresolved.append(f"EMBEDDED_MOD_SELECTION_UNKNOWN: {mod_id}: {sorted(versions)}")
        else:
            mods[mod_id] = selected[0]['version']

    platform = {"java", "fabricloader", "quilt_loader", "forge", "neoforge"}
    for dep in deps:
        did, kind = dep['id'], dep.get('kind', 'required')
        record = {**dep, 'found_version': mods.get(did)}
        evaluated.append(record)
        if dep['owner'] in providers and len(providers[dep['owner']]) > 1:
            if not any(p['jar'] == dep['jar'] and p.get('nested') == dep.get('nested') and
                       p['version'] == mods.get(dep['owner']) for p in providers[dep['owner']]):
                record['status'] = 'provider_not_selected'
                continue
        if did in platform and did not in mods:
            record['status'] = 'platform_unverified'
            notes.append(f"Platform prerequisite {did} {dep['range']} from {dep['owner']}: installed platform version not supplied")
        elif did not in mods:
            record['status'] = 'missing' if kind == 'required' else 'not_activated'
            if kind == 'required' and did not in providers:
                errors.append(f"MISSING_JAR_DEPENDENCY: {dep['owner']} requires {did} {dep['range']}")
        else:
            check = compare_maven if dep.get('dialect') == 'maven' else compare
            ok = check(mods[did], dep['range'])
            record['status'] = 'matched' if ok else ('unknown' if ok is None else 'outside_range')
            if kind == 'discouraged' and ok:
                notes.append(f"DISCOURAGED_DEPENDENCY: {dep['owner']} with {did} {mods[did]}")
            elif (ok is False and kind in ('required', 'optional')) or (ok is True and kind == 'incompatible'):
                errors.append(f"JAR_VERSION_CONFLICT: {dep['owner']} {kind} {did} {dep['range']}; selected {mods[did]}")
            elif ok is None:
                unresolved.append(f"UNKNOWN_VERSION_RANGE: {dep['owner']} -> {did} {dep['range']}; version {mods[did]}")
    return {"errors": sorted(set(errors)), "unresolved": sorted(set(unresolved)),
            "platform_requirements": sorted(set(notes)), "providers": dict(providers), "dependencies": evaluated,
            "mods": mods, "coverage": "Static JAR metadata and recognized version expressions"}
