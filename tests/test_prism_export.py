import json
import zipfile

import pytest

from conftest import FakeSource, decision, file
from mod_curator.models import SourceError
from mod_curator.prism_export import build_manifest, write_pack


def test_manifest_requires_actual_dependency_closure(filters):
    lock = {"roots": [1], "files": {"1": file(1, [2])}}
    loader = {"id": "neoforge-21.1.266"}
    with pytest.raises(SourceError, match="required project 2 missing"):
        build_manifest(lock, filters, loader, "test")
    lock["files"]["2"] = file(2)
    m = build_manifest(lock, filters, loader, "test")
    assert m["files"] == [{"projectID": 1, "fileID": 100, "required": True}, {"projectID": 2, "fileID": 200, "required": True}]
    assert m["minecraft"]["version"] == "1.21.1"


def test_zip_is_prism_manifest_not_jars_and_never_clobbers(tmp_path, filters):
    m = build_manifest({"roots": [1], "files": {"1": file(1)}}, filters, {"id": "neoforge-21.1.266"}, "test")
    r = write_pack(m, tmp_path, "test.zip")
    with zipfile.ZipFile(r["path"]) as z:
        assert set(z.namelist()) == {"manifest.json", "overrides/"}
        assert json.loads(z.read("manifest.json")) == m
    assert write_pack(m, tmp_path, "test.zip")["reused"]
    m["name"] = "changed"
    with pytest.raises(SourceError, match="existing archive differs"):
        write_pack(m, tmp_path, "test.zip")


async def test_unresolved_export_is_a_persisted_draft(engine, filters):
    job = engine.store.create(filters.model_dump())
    await engine.run(job, FakeSource([[(1, 100)]]))
    d = engine.store.get(job)
    engine.save_analysis(job, [decision(d["projects"]["1"])], 1)
    selected = engine.propose(job, [1], "review", 0)
    engine.confirm(job, selected["revision"], selected["fingerprint"], target="prism")
    await engine.run(job, FakeSource(files={1: [file(1, [2])]}))
    d = engine.store.get(job)
    assert d["status"] == "draft" and not d["pack_export"]["ready"]
    assert not list(engine.store.destination("mods").glob("*.zip"))


@pytest.mark.parametrize('corrupt', [False, True])
async def test_loader_checks_official_metadata_hash_chain(monkeypatch, filters, corrupt):
    import hashlib
    import httpx
    import mod_curator.prism_export as module

    detail = json.dumps({'uid': 'net.neoforged', 'version': '21.1.266',
                         'requires': [{'uid': 'net.minecraft', 'equals': '1.21.1'}]}).encode()
    index = json.dumps({'versions': [{'version': '21.1.266', 'type': 'release',
                       'requires': [{'uid': 'net.minecraft', 'equals': '1.21.1'}],
                       'sha256': hashlib.sha256(detail).hexdigest()}]}).encode()
    root = {'packages': [{'uid': 'net.neoforged',
            'sha256': 'invalid' if corrupt else hashlib.sha256(index).hexdigest()}]}
    requests = []
    def respond(request):
        requests.append(request.url.path)
        if request.url.path == '/v1/index.json':
            return httpx.Response(200, json=root)
        return httpx.Response(200, content=index if request.url.path.endswith('/index.json') else detail)
    client_class = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    async def no_sleep(_):
        pass
    monkeypatch.setattr(module.asyncio, 'sleep', no_sleep)
    if corrupt:
        with pytest.raises(SourceError, match='PRISM_METADATA_INCONSISTENT'):
            await module.loader_version(filters, '21.1.266')
        assert requests.count('/v1/net.neoforged/index.json') == 3
    else:
        result = await module.loader_version(filters, '21.1.266')
        assert result['metadata_chain_verified'] and result['id'] == 'neoforge-21.1.266'


@pytest.mark.parametrize("unexpected_member", ["../outside.jar", "manifest.json", "overrides/extra.txt"])
def test_repeated_export_rejects_archive_with_an_extra_member(tmp_path, unexpected_member):
    import warnings
    manifest = {"files": []}
    result = write_pack(manifest, tmp_path, "repeated.zip")
    with zipfile.ZipFile(result["path"], "a") as archive, warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        archive.writestr(unexpected_member, "")
    with pytest.raises(SourceError, match="EXPORT_EXISTS"):
        write_pack(manifest, tmp_path, "repeated.zip")
