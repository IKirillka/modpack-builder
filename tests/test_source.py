import json

import httpx
import pytest

from mod_curator.models import SourceError
from mod_curator.source import CurseForge, catalog_url, parse_catalog


def html(projects, total=None, has_next=False):
    data = {"tileProjects": projects}
    payload = "1:" + json.dumps(data, separators=(",", ":"))
    flight = "self.__next_f.push(" + json.dumps([1, payload]) + ")"
    cards = "".join(f'<div class="project-card"><a class="name" href="/minecraft/mc-mods/{p["slug"]}">{p["name"]}</a></div>' for p in projects)
    return f'''<title>Minecraft Mods - CurseForge</title><p aria-label="Sort by">Total Downloads</p>
    <input checked aria-label="Mods"><input checked aria-label="1.21.1"><input checked aria-label="NeoForge filter">
    <div class="results-count">{len(projects) if total is None else total:,} Projects</div>
    <a aria-label="Next Page" class="{'btn-next' if has_next else 'btn-next disabled'}"></a>
    {cards}<script>{flight}</script>'''


def project(n=62451353):
    return dict(id=688768, name="Create: Enchantment Industry", slug="create-enchantment-industry", downloadsCount=n)


def test_actual_flight_shape_exact_counter(filters):
    p = parse_catalog(html([project()]), catalog_url(filters, 1), filters, 1)
    assert p.cards[0]["downloads"] == 62451353
    assert p.cards[0]["id"] == 688768


@pytest.mark.parametrize("change,match", [
    (lambda x: html([project("62.4M")]), "UNKNOWN_DOWNLOADS"),
    (lambda x: x.replace('Total Downloads', 'Relevancy'), "SORT_UNVERIFIED"),
    (lambda x: x.replace('checked aria-label="NeoForge filter"', 'aria-label="NeoForge filter"'), "FILTER_UNVERIFIED"),
    (lambda x: x.replace('class="name"', 'class="missing"'), "INCOMPLETE_PAGE"),
    (lambda x: x.replace('1 Projects', '25 Projects'), "INCOMPLETE_PAGE"),
    (lambda x: '<title>Just a moment...</title>', "SITE_BLOCKED"),
])
def test_parse_fails_closed(filters, change, match):
    with pytest.raises(SourceError, match=match):
        parse_catalog(change(html([project()])), catalog_url(filters, 1), filters, 1)


def test_filters_url_and_ignored_filter(filters):
    url = catalog_url(filters, 2)
    assert "sortBy=total+downloads" in url and "page=2" in url and "gameVersionTypeId=6" in url
    with pytest.raises(SourceError, match="FILTER_MISMATCH"):
        parse_catalog(html([project()]), url.replace("version=1.21.1", "version=1.20.1"), filters, 2)
    filters.categories = ["nonexistent-category"]
    with pytest.raises(SourceError, match="CATEGORY_UNVERIFIED"):
        parse_catalog(html([project()]), catalog_url(filters, 1), filters, 1)


def test_empty_catalog_must_have_explicit_data(filters):
    assert not parse_catalog(html([]), catalog_url(filters, 1), filters, 1).cards
    with pytest.raises(SourceError, match="SCHEMA_CHANGED"):
        parse_catalog(html([]).replace("tileProjects", "unknown"), catalog_url(filters, 1), filters, 1)


async def test_bounded_network_retries(monkeypatch):
    calls = []
    async def fast_sleep(seconds):
        pass
    monkeypatch.setattr("mod_curator.source.asyncio.sleep", fast_sleep)
    def handler(request):
        calls.append(request)
        return httpx.Response(500, request=request)
    s = CurseForge({})
    await s.client.aclose()
    s.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError, match="SOURCE_HTTP_500"):
        await s.request("https://www.curseforge.com/minecraft/search")
    assert len(calls) == 3
    await s.close()


async def test_no_key_uses_website_metadata_and_explicit_api_still_requires_key(monkeypatch, filters):
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    s = CurseForge({})
    async def website(pid, f):
        return [{"id": 42, "modId": pid, "_metadata_source": "website"}]
    monkeypatch.setattr(s, "website_files", website)
    assert (await s.files(688768, filters))[0]["id"] == 42
    with pytest.raises(SourceError, match="API_KEY_REQUIRED"):
        await s.api("mods/688768/files")
    await s.close()


async def test_image_only_description_is_not_reported_as_blank(tmp_path, monkeypatch):
    from mod_curator.source import CurseForge
    from mod_curator.models import SourceError
    import pytest
    source = CurseForge({'transport':'http'}, tmp_path)
    async def html(_):
        return '<div class="project-description"><img src="page.webp"></div>', 'https://www.curseforge.com/minecraft/mc-mods/example'
    source.html=html
    source.website_project=lambda *_: {'id':1}
    try:
        with pytest.raises(SourceError,match='DESCRIPTION_IMAGE_ONLY'):
            await source._describe_once({'id':1,'url':'https://www.curseforge.com/minecraft/mc-mods/example'})
        assert (tmp_path/'description-1.html').is_file()
    finally:
        await source.close()


def test_file_table_ignores_unfiltered_latest_files_sidebar():
    from mod_curator.source import files_and_pagination
    payload = '1:{"latestFiles":[{"files":[{"id":11}]}]}\n2:{"files":[{"id":22}],"pagination":{"index":0,"totalCount":1}}'
    files, pagination = files_and_pagination(payload)
    assert files == [{'id':22}] and pagination['totalCount'] == 1


def test_exclusion_filter_requires_the_requested_label(filters):
    filters.excluded_categories = ["magic"]
    body = html([project()])
    payload = json.dumps([1, '{"slug":"magic","name":"Magic"}'])
    body += f'<script>self.__next_f.push({payload})</script><input checked aria-label="Exclude Adventure">'
    with pytest.raises(SourceError, match="EXCLUSION_UNVERIFIED"):
        parse_catalog(body, catalog_url(filters, 1), filters, 1)
    page = parse_catalog(body.replace("Exclude Adventure", "Exclude Magic"), catalog_url(filters, 1), filters, 1)
    assert not page.order_guaranteed


async def test_exact_file_checks_prefix_and_always_refreshes_relations(filters, monkeypatch):
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    source = CurseForge({})
    pid, fid = 21, 100
    project = {"id": pid, "name": "Test", "slug": "test"}
    source.remember_project(project)
    item = {"id": fid, "fileName": "test.jar", "flavors": [{"name": "NeoForge"}], "gameVersions": ["1.21.1"],
            "releaseType": 1, "isAvailable": True}
    visits = []
    def flight(data):
        return "<script>self.__next_f.push(" + json.dumps([1, json.dumps(data)]) + ")</script>"
    async def html(url):
        visits.append(url)
        if "/files/all?" in url:
            assert "page=1&" in url
            files = [item] + [{**item, 'id': 200+i} for i in range(19)]
            return flight({"project": project, "files": files, "pagination": {"index": 0, "totalCount": 60}}), url
        related = {"id": 31, "name": "Dep", "slug": "dep", "type": "RequiredDependency"}
        return flight({"project": project, "file": item, "relations": {
            "data": [related], "pagination": {"index": 0, "pageSize": 20, "totalCount": 1}}}), url
    source.html = html
    try:
        for _ in range(2):
            result = await source.exact_file(pid, fid, filters)
            assert result["dependencies"] == [{"modId": 31, "relationType": 3}]
        assert len(visits) == 4
        # The ordinary resolver reuses its dependency evidence. Export requests fresh verification.
        await source.complete_file(source.normalize_file(item, pid, "test"))
        assert len(visits) == 4
    finally:
        await source.close()
