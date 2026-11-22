import hashlib
import io
import json
from pathlib import Path
import zipfile

from mod_curator import instance_sync
from mod_curator.composition import Composition
from mod_curator.jar_validation import compare_maven, validate_pack
from mod_curator.modrinth import ExternalVersion
from mod_curator.preflight import schedule
from mod_curator.verification import audit_prism, compare_inventory
from conftest import FakeSource, file


LOADER = {'id': 'neoforge-21.1.267', 'uid': 'net.neoforged', 'version': '21.1.267'}
CF = file(1, [2])
LIB = file(2)


def make_jar(modid, deps=''):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        z.writestr('META-INF/neoforge.mods.toml', f'[[mods]]\nmodId="{modid}"\nversion="1.0"\n{deps}')
    return stream.getvalue()


async def test_resume_same_checkpoint_and_mixed_zip_with_prism_snapshot(engine, filters, tmp_path, monkeypatch):
    async def loader(*args):
        return LOADER
    monkeypatch.setattr('mod_curator.preflight.loader_version', loader)
    monkeypatch.setattr('mod_curator.prism_export.loader_version', loader)
    jars = {'project-1.jar': make_jar('root'), 'project-2.jar': make_jar('library'), 'mr.jar': make_jar('foreign')}
    cf_files = {1: [dict(CF, hashes=[{'algo': 1, 'value': hashlib.sha1(jars['project-1.jar']).hexdigest()}])],
                2: [dict(LIB, hashes=[{'algo': 1, 'value': hashlib.sha1(jars['project-2.jar']).hexdigest()}])]}
    ext_contents = jars['mr.jar']
    ver = {'id': 'VERSIONA', 'project_id': 'PROJECTA', 'version_number': '1.0', 'loaders': ['neoforge'],
           'game_versions': ['1.21.1'], 'version_type': 'release', 'dependencies': [],
           'files': [{'filename': 'mr.jar', 'url': 'https://cdn.modrinth.com/data/PROJECTA/versions/VERSIONA/mr.jar',
                      'hashes': {'sha512': hashlib.sha512(ext_contents).hexdigest()}, 'size': len(ext_contents), 'primary': True}]}
    async def version(self, vid):
        assert vid == ver['id']
        return ver
    async def download(self, item, path):
        path.write_bytes(ext_contents)
    monkeypatch.setattr('mod_curator.modrinth.Modrinth.version', version)
    monkeypatch.setattr('mod_curator.modrinth.Modrinth.download', download)
    parent = engine.store.create(filters.model_dump(), stage='awaiting_analysis', status='awaiting_analysis', selection=[1])
    rev = Composition(engine).plan_sources(parent, 0, external=[ExternalVersion(
        project_id='PROJECTA', version_id='VERSIONA', reason='Requested extension', required_by=[1])])['revision']
    task = schedule(engine, parent, rev, user_request='Build and verify this exact revision')['job_id']

    class Interrupted(FakeSource):
        async def files(self, pid, f):
            value = await super().files(pid, f)
            engine.cancel(task)
            return value
    await engine.run(task, Interrupted(files=cf_files))
    assert engine.store.get(task)['status'] == 'cancelled'
    engine.resume(task)
    source = FakeSource(files=cf_files)
    await engine.run(task, source)
    plan = engine.store.get(task)['check_plan']
    assert plan['ready'] and source.file_calls == [2] and plan['progress']['cache_hits'] == 1
    assert {(p['source'], p['project_id'], p['role']) for p in plan['items']} == {
        ('curseforge', 1, 'selected'), ('curseforge', 2, 'dependency'), ('modrinth', 'PROJECTA', 'dependency')}
    export_id = Composition(engine).export_revision(parent, rev, 'Fixture')['job_id']
    await engine.run(export_id, FakeSource(files=cf_files))
    exported = engine.store.get(export_id)['pack_export']
    assert exported['ready']
    with zipfile.ZipFile(exported['path']) as z:
        assert z.read('overrides/mods/mr.jar') == ext_contents
        manifest = json.loads(z.read('manifest.json'))
    instance = tmp_path / 'isolated'
    mods = instance / 'minecraft/mods'
    mods.mkdir(parents=True)
    (instance / 'flame').mkdir()
    (instance / 'flame/manifest.json').write_text(json.dumps(manifest))
    (instance / 'mmc-pack.json').write_text(json.dumps({'components': [
        {'uid': 'net.minecraft', 'version': '1.21.1'}, {'uid': LOADER['uid'], 'version': LOADER['version']}]}))
    for name, content in jars.items():
        (mods / name).write_bytes(content)
    observed = audit_prism(engine.store, export_id, instance)
    assert observed['verified']
    receipt = json.loads(Path(observed['receipt_path']).read_text(encoding='utf-8'))
    assert (receipt['user_request'], receipt['revision'], receipt['plan_task_id'], receipt['zip']['sha256']) == (
        'Build and verify this exact revision', rev, task, exported['sha256'])
    assert receipt['installation_snapshot']['inventory']['actual_count'] == 3
    with engine.store.connect() as db:
        stored, = db.execute('SELECT data FROM installation_snapshots WHERE job_id=?', (export_id,)).fetchone()
    assert json.loads(stored)['installation_snapshot']['snapshot_id'] == observed['snapshot_id']
    instance_sync.register(engine.store, export_id, instance)

    # The staged instance is a separate directory. Revert a verified change.
    import shutil
    staged = tmp_path / 'staged'
    shutil.copytree(instance, staged)
    (mods / 'Chunky-NeoForge.jar').write_bytes(b'extra')
    (mods / 'project-1.jar').write_bytes(b'changed')
    (mods / 'project-2.jar').unlink()
    audit = audit_prism(engine.store, export_id, instance)
    assert {key: len(audit['inventory'][key]) for key in ('missing', 'extra', 'different')} == {
        'missing': 1, 'extra': 1, 'different': 1}
    diff = instance_sync.preview(engine.store, export_id, instance, staged)
    assert diff['removed'] == ['Chunky-NeoForge.jar']
    applied = instance_sync.apply(engine.store, export_id, instance, staged, diff['preview_token'])
    assert applied['snapshot']['verified']
    result = instance_sync.rollback(instance, Path(applied['rollback_directory']), diff['preview_token'])
    assert result['snapshot']['jars'] == diff['before']['jars']


def test_chunky_232_vs_231_is_an_unattributed_extra():
    expected = [{'file_name': f'{i}.jar', 'hashes': {'sha256': f'{i}'}} for i in range(231)]
    actual = {item['file_name']: {'hashes': item['hashes']} for item in expected}
    actual['Chunky-NeoForge-1.4.23.jar'] = {'hashes': {'sha256': 'archive snapshot hash'}}
    diff = compare_inventory(expected, actual)
    assert len(diff['matched']) == 231 and diff['actual_count'] == 232
    assert [x['file_name'] for x in diff['extra']] == ['Chunky-NeoForge-1.4.23.jar']


def test_neoforge_ranges_and_conditional_optional_integration(filters, tmp_path):
    assert compare_maven('21.1.267', '21.1.228') is True
    assert compare_maven('0.4.9.4-1.21.1', '[0.4.9.4-1.21.1,0.5.0)') is True
    assert compare_maven('5.1.1-1.21.1', '[0.4.9.4-1.21.1,0.5.0)') is False
    assert compare_maven('2101.2.8-build.91', '[2101.2.7-build.81,)') is True
    vintage = tmp_path / 'vintage.jar'
    bridge = tmp_path / 'bridge.jar'
    vintage.write_bytes(make_jar('vintage', '''[[dependencies.vintage]]
modId="kubejs_create"
type="optional"
versionRange="[1.1,)"
'''))
    bridge.write_bytes(make_jar('kubejs_create'))
    assert validate_pack([vintage], filters)['dependencies'][0]['status'] == 'not_activated'
    assert validate_pack([vintage, bridge], filters)['dependencies'][0]['status'] == 'outside_range'


async def test_documented_exact_relation_applies_only_to_intended_file(filters):
    from mod_curator.plan_evidence import grouped
    from mod_curator.resolver import ResolutionError, resolve
    import pytest
    rules = grouped([{'project_id': 10, 'file_id': 1000, 'dependency_id': 90, 'kind': 'author',
                      'url': 'https://example.com/exact-version', 'evidence_version': '1.3.2',
                      'disposition': 'accept_platform_override', 'reason': 'Exact source declares a different loader dependency'}])
    src = FakeSource(files={10: [file(10, [90], fid=1000), file(10, [90], fid=1001)]})
    with pytest.raises(ResolutionError):
        await resolve(src, [10], filters, pins={10: 1001}, evidence=rules)
    result = await resolve(src, [10], filters, evidence=rules)
    assert result['files']['10']['id'] == 1000
    assert result['files']['10']['_platform_dependencies'][0]['modId'] == 90
    assert not result['files']['10']['dependencies']
    assert result['alternatives'][0]['preferred_file'] == 1001


def test_prism_required_lwjgl_component_is_a_separate_failure(tmp_path):
    from mod_curator.verification import component_graph
    meta = tmp_path / 'meta/net.minecraft'
    meta.mkdir(parents=True)
    (meta / '1.21.1.json').write_text(json.dumps({'requires': [{'uid': 'org.lwjgl3', 'suggests': '3.3.3'}]}))
    assert component_graph({'net.minecraft': '1.21.1'}, tmp_path / 'meta')['issues'] == [
        'net.minecraft requires org.lwjgl3 3.3.3']
