import hashlib
import json

import pytest

from mod_curator.engine import Engine
from mod_curator.models import Filters, SourceError
from mod_curator.source import Page, catalog_url
from mod_curator.store import Store


@pytest.fixture
def filters():
    return Filters(minecraft="1.21.1", loader="NeoForge", minimum_downloads=50,
                   wishes="Rotational factories with useful logistics", destination="mods", page_size=10)


@pytest.fixture
def engine(tmp_path):
    store = Store(tmp_path / "state")
    dest = tmp_path / "mods"
    dest.mkdir()
    (store.root / "config.json").write_text(json.dumps({"destinations": {"mods": str(dest)}, "transport": "http"}))
    e = Engine(store)
    e.launch = lambda job_id: {"job_id": job_id}
    return e


def card(pid, downloads, page=1, position=1):
    return dict(id=pid, downloads=downloads, name=f"Project {pid}", slug=f"project-{pid}",
                url=f"https://www.curseforge.com/minecraft/mc-mods/project-{pid}", page=page, position=position)


class FakeSource:
    def __init__(self, pages=(), files=None, *, order_guaranteed=False):
        self.pages = list(pages)
        self.calls = []
        self.file_calls = []
        self.file_data = files or {}
        self.describe_calls = []
        self.order_guaranteed = order_guaranteed

    async def page(self, filters, number):
        self.calls.append(number)
        data = self.pages[number - 1]
        if isinstance(data, Exception):
            raise data
        return Page([card(i, n, number, p + 1) for p, (i, n) in enumerate(data)],
                    number < len(self.pages), sum(len(p) for p in self.pages if isinstance(p, list)), catalog_url(filters, number),
                    order_guaranteed=self.order_guaranteed)

    async def describe(self, project):
        self.describe_calls.append(project["id"])
        text = f"Full description for {project['id']}: rotational factories; storage and logistics."
        return dict(description=text, description_sha256=hashlib.sha256(text.encode()).hexdigest(), metadata={})

    async def project(self, pid):
        return card(pid, 0)

    async def files(self, pid, filters):
        self.file_calls.append(pid)
        return self.file_data.get(pid, [])

    async def close(self):
        pass


def file(pid, deps=(), fid=None, **kwargs):
    return dict(id=fid or pid * 100, modId=pid, fileName=f"project-{pid}.jar", isAvailable=True,
                gameVersions=["1.21.1", "NeoForge"], releaseType=1, fileDate="2026-01-01",
                dependencies=[{"modId": i, "relationType": 3} if isinstance(i, int) else i for i in deps],
                fileLength=0, hashes=[], downloadUrl=None, **kwargs)


def decision(project, include=True):
    return dict(project_id=project["id"], evidence_sha256=project["description_sha256"], functions="Rotational factory",
                fit="Matches requested mechanics", include=include, explanation="Description describes logistics", uncertainties=[])
