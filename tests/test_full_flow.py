import hashlib
import io
import zipfile

import httpx

from mod_curator.engine import Engine
from mod_curator.store import Store
from conftest import FakeSource, decision, file


def payload(pid):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("META-INF/neoforge.mods.toml", f'[[mods]]\nmodId="project_{pid}"\nversion="1.0.0"\n')
    data = b.getvalue()
    return data


async def test_end_to_end_prepared_data(engine, filters, monkeypatch):
    job = engine.store.create(filters.model_dump())
    source = FakeSource([[(1, 100), (2, 50)]], files={1: [file(1, [3])], 3: [file(3)]})
    await engine.run(job, source)
    engine.save_analysis(job, [decision(engine.store.get(job)["projects"]["1"])], 1)
    selection = engine.propose(job, [1], "One selected factory; project 3 is a low-popularity required library", 0)
    engine.confirm(job, selection["revision"], selection["fingerprint"])
    for pid, fs in source.file_data.items():
        data = payload(pid)
        fs[0].update(fileLength=len(data), hashes=[{"algo": 1, "value": hashlib.sha1(data).hexdigest()}],
                     downloadUrl=f"https://edge.forgecdn.net/{pid}.jar")
    original_client = httpx.AsyncClient
    def handler(req):
        return httpx.Response(200, content=payload(int(req.url.path.split("/")[-1].split(".")[0])))
    monkeypatch.setattr("mod_curator.engine.httpx.AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    await engine.run(job, source)
    d = Engine(Store(engine.store.root)).store.get(job)
    assert d["status"] == "completed" and d["installation_complete"]
    assert set(d["lock"]["files"]) == {"1", "3"}
    assert sorted(p.name for p in engine.store.destination("mods").iterdir()) == ["project-1.jar", "project-3.jar"]
    assert not d["jar_dependency_validation"]["errors"]
    assert (engine.store.job_dir(job) / "report.json").exists()
    await engine.revise(job, [], selection["revision"])
    revised = engine.store.get(job)
    assert not revised["installation_complete"] and "lock" not in revised
    assert "jar_dependency_validation" not in revised
    assert len(list(engine.store.destination("mods").iterdir())) == 2  # no implicit deletion of earlier JARs
