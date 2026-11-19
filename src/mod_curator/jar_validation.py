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
NESTED_LIMIT = 32 * 1024 * 1024


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


def read_metadata(path, loader, depth=0, allow_library=False):
    if depth > 8:
        raise ValueError("JAR_METADATA_LIMIT: nested JAR depth exceeds 8")
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
            if name not in z.namelist():
                raise ValueError(f"JAR_METADATA_MISSING: {name}")
            obj = tomllib.loads(read(name).decode())
            manifest = read("META-INF/MANIFEST.MF").decode(errors="replace") if "META-INF/MANIFEST.MF" in z.namelist() else ""
            match = re.search(r"^Implementation-Version:\s*(.+)$", manifest, re.M)
            for mod in obj.get("mods", []):
                version = mod["version"]
                if version == "${file.jarVersion}" and match:
                    version = match[1].strip()
                mods.append({"id": mod["modId"], "version": version})
            for owner, values in obj.get("dependencies", {}).items():
                for dep in values:
                    if dep.get("mandatory", dep.get("type") == "required"):
                        deps.append({"owner": owner, "id": dep["modId"], "range": dep.get("versionRange", "*")})
            if "META-INF/jarjar/metadata.json" in z.namelist():
                nested = [j["path"] for j in json.loads(read("META-INF/jarjar/metadata.json")).get("jars", [])]
        for name in nested:
            child = read_metadata(io.BytesIO(read(name, NESTED_LIMIT)), loader, depth + 1, allow_library=True)
            mods.extend(child[0])
            deps.extend(child[1])
    if not mods:
        raise ValueError("JAR_METADATA_MISSING: no declared mods")
    return mods, deps


def validate_pack(paths, filters, platform_versions=None):
    mods, deps, errors, unresolved, notes = {}, [], [], [], []
    for path in paths:
        try:
            entries, requirements = read_metadata(path, filters.loader)
            for entry in entries:
                key, version = entry["id"], entry["version"]
                if key in mods and mods[key] != version:
                    errors.append(f"DUPLICATE_MOD_VERSION: {key}: {mods[key]} versus {version}")
                mods[key] = version
            deps.extend(requirements)
        except Exception as e:
            unresolved.append(f"{path.name}: {e}")
    mods["minecraft"] = filters.minecraft
    mods.update(platform_versions or {})
    platform = {"java", "fabricloader", "quilt_loader", "forge", "neoforge"}
    for dep in deps:
        if dep["id"] in platform and dep["id"] not in mods:
            notes.append(f"Platform prerequisite {dep['id']} {dep['range']} from {dep['owner']}: installed platform version not supplied")
            continue
        if dep["id"] not in mods:
            errors.append(f"MISSING_JAR_DEPENDENCY: {dep['owner']} requires {dep['id']} {dep['range']}")
            continue
        ok = compare(mods[dep["id"]], dep["range"])
        if ok is False:
            errors.append(f"JAR_VERSION_CONFLICT: {dep['owner']} requires {dep['id']} {dep['range']}; selected {mods[dep['id']]}")
        elif ok is None:
            unresolved.append(f"UNKNOWN_VERSION_RANGE: {dep['owner']} -> {dep['id']} {dep['range']}; version {mods[dep['id']]}")
    return {"errors": sorted(set(errors)), "unresolved": sorted(set(unresolved)), "platform_requirements": sorted(set(notes)),
            "mods": mods, "coverage": "Mandatory JAR dependencies and recognized version expressions. No runtime/conflict testing."}
