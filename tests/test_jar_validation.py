import json
import zipfile

import pytest

from mod_curator.jar_validation import compare, validate_pack


@pytest.mark.parametrize("version,expression,result", [
    ("1.2.3", ">=1.2.0 <2.0.0", True), ("2.0.0", ">=1.2.0 <2.0.0", False),
    ("1.2.3", "[1.0,2.0)", True), ("2.0", "[1.0,2.0)", False),
    ("1.0", "[1.0]", True), ("1.5", "(,1.0],[2.0,)", False),
    ("2.2", "(,1.0],[2.0,)", True), ("1.2.3", "1.2.x", True),
    ("1.2.3", "^1.1.0", True), ("0.3.0", "^0.2.1", False),
    ("1.2.3", "~1.2", True), ("1.3", "~1.2", False),
    ("1.2", "${missing}", None), ("unknown", ">=1", None),
])
def test_ranges(version, expression, result):
    assert compare(version, expression) is result


def write_jar(path, name, obj):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, obj)
    return path


def test_fabric_conflict_and_missing_dependency(tmp_path, filters):
    filters.loader = "Fabric"
    a = write_jar(tmp_path / "a.jar", "fabric.mod.json", json.dumps({"id": "a", "version": "1.0", "depends": {"b": ">=2.0"}}))
    b = write_jar(tmp_path / "b.jar", "fabric.mod.json", json.dumps({"id": "b", "version": "1.0"}))
    assert "JAR_VERSION_CONFLICT" in validate_pack([a, b], filters)["errors"][0]
    assert "MISSING_JAR_DEPENDENCY" in validate_pack([a], filters)["errors"][0]


def test_neoforge_metadata_and_game_version(tmp_path, filters):
    content = '''[[mods]]
modId="example"
version="1.0"
[[dependencies.example]]
modId="minecraft"
type="required"
versionRange="[1.21.1,1.22)"
[[dependencies.example]]
modId="neoforge"
type="required"
versionRange="[21.1,)"
'''
    path = write_jar(tmp_path / "a.jar", "META-INF/neoforge.mods.toml", content)
    report = validate_pack([path], filters)
    assert not report["errors"] and not report["unresolved"]
    assert len(report["platform_requirements"]) == 1
    filters.minecraft = "1.20.1"
    assert report != validate_pack([path], filters)
    assert "JAR_VERSION_CONFLICT" in validate_pack([path], filters)["errors"][0]


def test_checks_actual_installed_loader_instead_of_only_reporting_range(tmp_path, filters):
    content = '''[[mods]]
modId="example"
version="1.0"
[[dependencies.example]]
modId="neoforge"
type="required"
versionRange="[21.1.219,)"
'''
    path = write_jar(tmp_path / 'loader.jar', 'META-INF/neoforge.mods.toml', content)
    assert not validate_pack([path], filters, {'neoforge': '21.1.266'})['errors']
    assert 'JAR_VERSION_CONFLICT' in validate_pack([path], filters, {'neoforge': '21.1.125'})['errors'][0]
