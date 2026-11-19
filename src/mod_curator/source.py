"""Source protocol: catalogue membership/order comes exclusively from website HTML."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from . import __version__
from .models import Filters, LOADERS, SourceError

BASE = "https://www.curseforge.com"
SORT = "total downloads"


@dataclass
class Page:
    cards: list[dict]
    has_next: bool
    total: int
    url: str
    evidence: dict | None = None
    # A sort label or a monotonic page does not attest to a globally ordered snapshot.
    order_guaranteed: bool = False


class Source(Protocol):
    async def page(self, filters: Filters, number: int) -> Page: ...
    async def describe(self, project: dict) -> dict: ...
    async def project(self, project_id: int) -> dict: ...
    async def files(self, project_id: int, filters: Filters) -> list[dict]: ...
    async def close(self): ...


def catalog_url(f: Filters, page: int) -> str:
    params = {"class": "mc-mods", "page": page, "pageSize": f.page_size,
              "sortBy": SORT, "version": f.minecraft, "gameVersionTypeId": LOADERS[f.loader][0]}
    if f.categories:
        params["categories"] = ",".join(f.categories)
    if f.excluded_categories:
        params["excludedCategories"] = ",".join(f.excluded_categories)
    if f.search:
        params["search"] = f.search
    return BASE + "/minecraft/search?" + urlencode(params)


def flight_text(soup):
    """Decode embedded Next Flight JSON strings. Never execute page JavaScript."""
    chunks = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)\s*;?\s*$', text, re.S):
            try:
                data = json.loads(match[1])
                if len(data) > 1 and isinstance(data[1], str):
                    chunks.append(data[1])
            except (ValueError, TypeError):
                pass
    return "".join(chunks)


def embedded_value(text, key):
    found = []
    for m in re.finditer(r'"' + re.escape(key) + r'"\s*:', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[m.end():].lstrip())
            found.append(value)
        except ValueError:
            continue
    return found


def files_and_pagination(payload):
    """The filtered file table owns its pagination; sidebar latestFiles do not."""
    decoder = json.JSONDecoder()
    candidates = []
    for marker in re.finditer(r'"files"\s*:\s*(?=\[)', payload):
        try:
            batch, end = decoder.raw_decode(payload, marker.end())
            paging_field = re.match(r'\s*,\s*"pagination"\s*:\s*', payload[end:])
            if paging_field:
                paging, _ = decoder.raw_decode(payload, end + paging_field.end())
                if isinstance(paging, dict) and {'index', 'totalCount'} <= paging.keys():
                    candidates.append((batch, paging))
        except ValueError:
            continue
    if len(candidates) != 1:
        raise SourceError('FILE_LIST_INCOMPLETE: no unique file table with its pagination')
    return candidates[0]


def parse_catalog(html: str, final_url: str, filters: Filters, number: int) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and any(x in soup.title.get_text().lower() for x in ("just a moment", "access denied")):
        raise SourceError("SITE_BLOCKED: CurseForge browser/security challenge")
    expected = parse_qs(urlparse(catalog_url(filters, number)).query)
    parsed = urlparse(final_url)
    actual = parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.netloc != "www.curseforge.com" or parsed.path != "/minecraft/search"
            or any(actual.get(k) != v for k, v in expected.items())
            or any(k in actual and k not in expected for k in ("categories", "excludedCategories", "search"))):
        raise SourceError("FILTER_MISMATCH: URL changed or filters dropped")
    sort = soup.select_one('[aria-label="Sort by"]')
    if not sort or sort.get_text(" ", strip=True) != "Total Downloads":
        raise SourceError("SORT_UNVERIFIED: expected Total Downloads selected in website")
    # Verify application, not only the URL. Labels and their checked inputs are live site DOM.
    checked = {i.get("aria-label") for i in soup.select("input[checked]")}
    required = {"Mods", filters.minecraft, filters.loader + " filter"}
    if not required <= checked:
        raise SourceError(f"FILTER_UNVERIFIED: required checked controls {sorted(required - checked)}")
    payload = flight_text(soup)
    for slug, prefix in [(s, "") for s in filters.categories] + [(s, "Exclude ") for s in filters.excluded_categories]:
        labels = set()
        for m in re.finditer(r'\{[^{}]*"slug":"' + re.escape(slug) + r'"[^{}]*\}', payload):
            try:
                name = json.loads(m[0]).get("name")
                if isinstance(name, str):
                    labels.add(prefix + name)
            except ValueError:
                pass
        if not labels & checked:
            raise SourceError(f"{'EXCLUSION' if prefix else 'CATEGORY'}_UNVERIFIED: {slug}")
    if filters.search:
        field = soup.select_one('input[placeholder="Search for a project"]')
        if not field or field.get("value", "") != filters.search:
            raise SourceError("SEARCH_UNVERIFIED")
    lists = embedded_value(payload, "tileProjects")
    if not lists or not isinstance(lists[0], list):
        raise SourceError("SCHEMA_CHANGED: no embedded tileProjects; not an empty catalogue")
    projects = lists[0]
    dom = soup.select(".project-card a.name")
    dom_slugs = [urlparse(a.get("href", "")).path.rsplit("/", 1)[-1] for a in dom]
    if dom_slugs != [p.get("slug") for p in projects]:
        raise SourceError("INCOMPLETE_PAGE: rendered cards disagree with embedded catalogue order")
    count = soup.select_one(".results-count")
    if not count or not re.search(r"[\d,]+", count.get_text()):
        raise SourceError("INCOMPLETE_PAGE: missing result count")
    total = int(re.search(r"[\d,]+", count.get_text())[0].replace(",", ""))
    next_link = soup.select_one('a[aria-label="Next Page"]')
    if next_link is None and total > 0:
        raise SourceError("INCOMPLETE_PAGE: pagination missing")
    has_next = bool(next_link and "disabled" not in next_link.get("class", []) and next_link.get("aria-disabled") != "true")
    remaining = max(0, total - (number - 1) * filters.page_size)
    if len(projects) != min(filters.page_size, remaining):
        raise SourceError("INCOMPLETE_PAGE: card count inconsistent with pagination")
    if has_next != (remaining > filters.page_size):
        raise SourceError("INCOMPLETE_PAGE: next-page control disagrees with total")
    cards = []
    for pos, p in enumerate(projects, 1):
        n = p.get("downloadsCount")
        # No float, K/M conversion or assumed zero at the threshold.
        if type(n) is not int or n < 0:
            raise SourceError(f"UNKNOWN_DOWNLOADS: project {p.get('id')}")
        if type(p.get("id")) is not int or p["id"] <= 0 or not re.fullmatch(r"[a-z0-9-]+", p.get("slug", "")):
            raise SourceError("SCHEMA_CHANGED: invalid project identity")
        cards.append(dict(id=p["id"], name=p["name"], slug=p["slug"],
                          url=f"{BASE}/minecraft/mc-mods/{p['slug']}", downloads=n,
                          page=number, position=pos, summary=p.get("summary", "")))
    return Page(cards, has_next, total, final_url, {
        "html_sha256": hashlib.sha256(html.encode()).hexdigest(), "dom_cards": len(dom),
        "payload_cards": len(projects), "exact_counter_field": "tileProjects[].downloadsCount",
        "selected_sort": sort.get_text(" ", strip=True), "checked_controls": sorted(v for v in checked if v),
        "filters_verified": True})


class CurseForge:
    def __init__(self, config: dict, evidence_dir=None):
        self.config = config
        self.client = httpx.AsyncClient(timeout=40, headers={"User-Agent": f"ModpackBuilder/{__version__} (+local user tool)"})
        self.api_key = os.environ.get("CURSEFORGE_API_KEY")
        self.browser = None
        self.remote_browser = None
        self.playwright = None
        self.browser_gate = asyncio.Semaphore(1)
        self.evidence_dir = evidence_dir
        self.gate = asyncio.Semaphore(3)
        self.known_projects = {}
        self.file_cache = {}

    def seed_projects(self, projects):
        self.known_projects.update({int(k): v for k, v in projects.items()})

    def remember_project(self, obj):
        if obj.get("gameId", 432) != 432 or obj.get("classCategory", {}).get("id", 6) != 6:
            raise SourceError("NOT_A_MINECRAFT_MOD")
        pid, slug = obj["id"], obj["slug"]
        if type(pid) is not int or not re.fullmatch(r"[a-z0-9-]+", slug):
            raise SourceError("INVALID_PROJECT_IDENTITY")
        p = dict(id=pid, name=obj["name"], slug=slug, url=f"{BASE}/minecraft/mc-mods/{slug}",
                 downloads=obj.get("downloads"), metadata_source="website")
        if type(obj.get("allowModDistribution")) is bool:
            p["allow_mod_distribution"] = obj["allowModDistribution"]
        self.known_projects[pid] = p
        return p

    def website_project(self, html, expected_id=None, *, payload=None):
        values = embedded_value(payload if payload is not None else flight_text(BeautifulSoup(html, "html.parser")), "project")
        obj = next((p for p in values if isinstance(p, dict) and (expected_id is None or p.get("id") == expected_id)), None)
        if obj is None:
            raise SourceError("PROJECT_METADATA_UNAVAILABLE")
        self.remember_project(obj)
        return obj

    async def request(self, url, *, api=False, params=None):
        headers = {"x-api-key": self.api_key} if api and self.api_key else {}
        if api and not self.api_key:
            raise SourceError("API_KEY_REQUIRED: set CURSEFORGE_API_KEY locally; catalogue never uses API search")
        last = None
        for attempt in range(3):
            try:
                r = await self.client.get(url, headers=headers, params=params)
                if r.status_code == 403:
                    raise SourceError("SITE_BLOCKED: HTTP 403 from CurseForge" if not api else "API_DENIED: invalid key or permissions")
                if r.status_code == 429 or r.status_code >= 500:
                    delay = min(30, max(1, float(r.headers.get("retry-after", 2 ** attempt))))
                    await asyncio.sleep(delay)
                    last = SourceError(f"SOURCE_HTTP_{r.status_code}")
                    continue
                r.raise_for_status()
                return r
            except SourceError:
                raise
            except (httpx.HTTPError, ValueError) as e:
                last = SourceError(f"NETWORK_ERROR: {type(e).__name__}")
                await asyncio.sleep(2 ** attempt)
        raise last or SourceError("RETRY_EXHAUSTED")

    async def html(self, url):
        if self.config.get("transport") not in ("browser", "cdp"):
            r = await self.request(url)
            return r.text, str(r.url)
        async with self.browser_gate:
            if not self.browser:
                from playwright.async_api import async_playwright
                self.playwright = await async_playwright().start()
                if self.config.get("transport") == "cdp":
                    address = self.config.get("cdp_url", "http://127.0.0.1:9222")
                    if urlparse(address).hostname not in ("127.0.0.1", "localhost", "::1"):
                        raise SourceError("CDP_MUST_BE_LOCAL: configure a loopback browser endpoint locally")
                    self.remote_browser = await self.playwright.chromium.connect_over_cdp(address)
                    if not self.remote_browser.contexts:
                        raise SourceError("CDP_NO_BROWSER_CONTEXT")
                    self.browser = self.remote_browser.contexts[0]
                else:
                    self.browser = await self.playwright.chromium.launch_persistent_context(
                        str(self.config["browser_profile"]), headless=self.config.get("headless", True))
            page = await self.browser.new_page()
            leave_open = False
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # A browser may finish a noninteractive Cloudflare check naturally after the
                # initial 403. Wait for actual website data; never click/solve a challenge.
                try:
                    await page.wait_for_function("""() => Array.from(document.scripts).some(
                        s => s.textContent.includes('self.__next_f.push([1,'))""",
                        timeout=self.config.get("browser_wait_seconds", 60) * 1000)
                except Exception as e:
                    leave_open = bool(self.remote_browser)
                    raise SourceError("SITE_BLOCKED_OR_INCOMPLETE: browser did not load site data; "
                                      "inspect the open local tab, complete any check yourself, then resume") from e
                return await page.content(), page.url
            finally:
                if not leave_open:
                    await page.close()

    async def page(self, filters, number):
        last = None
        for attempt in range(3):
            try:
                html, url = await self.html(catalog_url(filters, number))
                if self.evidence_dir:
                    (self.evidence_dir / f"catalog-{number}.html").write_text(html)
                page = parse_catalog(html, url, filters, number)
                self.seed_projects({p["id"]: p for p in page.cards})
                return page
            except SourceError as e:
                last = e
                if "BLOCKED" in str(e) or "UNVERIFIED" in str(e) or "MISMATCH" in str(e):
                    raise
                await asyncio.sleep(2 ** attempt)
        raise last

    async def api(self, endpoint, params=None):
        return (await self.request("https://api.curseforge.com/v1/" + endpoint, api=True, params=params)).json()

    async def project(self, project_id):
        if not self.api_key:
            if project_id in self.known_projects:
                return self.known_projects[project_id]
            html, _ = await self.html(f"{BASE}/projects/{project_id}")
            self.website_project(html, project_id)
            return self.known_projects[project_id]
        p = (await self.api(f"mods/{project_id}"))["data"]
        if p["id"] != project_id or p.get("gameId") != 432 or p.get("classId") != 6:
            raise SourceError("NOT_A_MINECRAFT_MOD")
        return dict(id=p["id"], name=p["name"], slug=p["slug"],
                    url=f"{BASE}/minecraft/mc-mods/{p['slug']}", downloads=p["downloadCount"], provenance="user_added",
                    allow_mod_distribution=p.get("allowModDistribution"))

    async def describe(self, project):
        for attempt in range(2):
            try:
                return await self._describe_once(project)
            except SourceError as e:
                if "BLOCKED" in str(e) or attempt == 1:
                    raise
                await asyncio.sleep(1)

    async def _describe_once(self, project):
        async with self.gate:
            if self.api_key:
                raw = (await self.api(f"mods/{project['id']}/description"))["data"]
                metadata = (await self.api(f"mods/{project['id']}"))["data"]
            else:
                html, _ = await self.html(project["url"])
                if self.evidence_dir:
                    (self.evidence_dir / f"description-{project['id']}.html").write_text(html)
                soup = BeautifulSoup(html, "html.parser")
                desc = soup.select_one(".project-description")
                if not desc:
                    raise SourceError(f"DESCRIPTION_UNAVAILABLE: {project['id']}; API key can enable official metadata")
                raw, metadata = str(desc), self.website_project(html, project["id"])
                metadata["metadata_source"] = "website"
            text = BeautifulSoup(raw, "html.parser").get_text("\n", strip=True)
            if not text:
                kind = "DESCRIPTION_IMAGE_ONLY" if BeautifulSoup(raw, "html.parser").select("img") else "EMPTY_DESCRIPTION"
                raise SourceError(kind)
            return {"description": text, "description_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "metadata": metadata}

    async def files(self, project_id, filters):
        if not self.api_key:
            return await self.website_files(project_id, filters)
        index, files = 0, []
        while True:
            if index >= 10000:
                raise SourceError(f"API_PAGINATION_LIMIT: files of {project_id}; result incomplete")
            obj = await self.api(f"mods/{project_id}/files", {"gameVersion": filters.minecraft,
                                "modLoaderType": LOADERS[filters.loader][1], "index": index, "pageSize": min(50, 10000 - index)})
            batch, paging = obj["data"], obj["pagination"]
            if paging["index"] != index or paging["resultCount"] != len(batch):
                raise SourceError("API_PAGINATION_INVALID")
            files.extend(batch)
            index += len(batch)
            if index >= paging["totalCount"]:
                return files
            if not batch:
                raise SourceError("API_FILES_INCOMPLETE")

    async def exact_file(self, project_id, file_id, filters):
        """Fetch fresh evidence for a pinned file without expanding older file pages."""
        if self.api_key:
            file = (await self.api(f"mods/{project_id}/files/{file_id}"))["data"]
            return file if file.get("id") == file_id else None
        files = await self.website_files(project_id, filters, until_file=file_id)
        if files and files[0]["isAvailable"]:
            return await self.complete_file(files[0], fresh=True)

    def normalize_file(self, item, pid, slug):
        flavors = [f["name"] for f in item.get("flavors", [])]
        if item.get("flavor") and item["flavor"]["name"] not in flavors:
            flavors.append(item["flavor"]["name"])
        if type(item.get("id")) is not int or not flavors or "gameVersions" not in item:
            raise SourceError("FILE_METADATA_INCOMPLETE")
        return {**item, "modId": pid, "gameVersions": list(dict.fromkeys(item["gameVersions"] + flavors)),
                "isAvailable": item.get("isAvailable", True) is True and not item.get("isEarlyAccess", False),
                "fileDate": item.get("dateCreated", ""),
                "downloadUrl": item.get("downloadUrl") or None, "hashes": item.get("hashes", []),
                "_slug": slug, "_details_complete": False, "_metadata_source": "website_public_file_page"}

    async def website_files(self, pid, filters, *, until_file=None):
        project = await self.project(pid)
        page, all_files, seen, expected_total = 1, [], set(), None
        while True:
            params = {"page": page, "pageSize": 20, "version": filters.minecraft,
                      "gameVersionTypeId": LOADERS[filters.loader][0], "showAlphaFiles": "show" if (3 in filters.release_types if filters.release_types is not None else filters.allow_prerelease) else "hide",
                      "sortBy": "dateCreated", "sortOrder": "desc"}
            url = project["url"] + "/files/all?" + urlencode(params)
            html, final = await self.html(url)
            expected = urlparse(url)
            actual = urlparse(final)
            if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path) or any(parse_qs(actual.query).get(key) != value
                                                   for key, value in parse_qs(expected.query).items()):
                raise SourceError("FILE_LIST_FILTER_MISMATCH")
            t = flight_text(BeautifulSoup(html, "html.parser"))
            self.website_project(html, pid, payload=t)
            batch, paging = files_and_pagination(t)
            total = paging["totalCount"]
            if expected_total is not None and total != expected_total:
                raise SourceError("FILE_LIST_CHANGED: resume after source stabilizes")
            expected_total = total
            if paging["index"] != (page - 1) * 20 or len(batch) != min(20, max(0, total - paging["index"])):
                raise SourceError("FILE_LIST_INCOMPLETE: count/index mismatch")
            for item in batch:
                if item["id"] in seen:
                    raise SourceError("FILE_LIST_DUPLICATE: pagination changed")
                seen.add(item["id"])
                if until_file is None or item["id"] == until_file:
                    all_files.append(self.normalize_file(item, pid, project["slug"]))
            if until_file is not None and all_files:
                return all_files
            if len(seen) >= total:
                return all_files
            page += 1

    async def complete_file(self, candidate, *, fresh=False):
        if candidate.get("_details_complete", True):
            return candidate
        pid, fid, slug = candidate["modId"], candidate["id"], candidate["_slug"]
        key = (pid, fid)
        if key in self.file_cache and not fresh:
            return self.file_cache[key]
        deps, page, seen, expected_total, detail = [], 1, set(), None, None
        types = {"EmbeddedLibrary": 1, "OptionalDependency": 2, "RequiredDependency": 3,
                 "Tool": 4, "Incompatible": 5, "Include": 6}
        while True:
            url = f"{BASE}/minecraft/mc-mods/{slug}/files/{fid}/dependencies?page={page}"
            html, final = await self.html(url)
            parsed = urlparse(final)
            if (parsed.scheme != "https" or parsed.netloc != "www.curseforge.com"
                    or parsed.path != f"/minecraft/mc-mods/{slug}/files/{fid}/dependencies"):
                raise SourceError("FILE_RELATIONS_REDIRECTED: cannot verify dependencies for the requested file")
            t = flight_text(BeautifulSoup(html, "html.parser"))
            self.website_project(html, pid, payload=t)
            actual = next((v for v in embedded_value(t, "file") if isinstance(v, dict) and v.get("id") == fid), None)
            relations = next((v for v in embedded_value(t, "relations") if isinstance(v, dict) and "data" in v and "pagination" in v), None)
            if actual is None or relations is None:
                raise SourceError("FILE_DEPENDENCIES_UNKNOWN: explicit file relations missing; do not assume empty")
            detail = self.normalize_file(actual, pid, slug)
            paging, batch = relations["pagination"], relations["data"]
            total = paging["totalCount"]
            if expected_total is not None and expected_total != total:
                raise SourceError("FILE_RELATIONS_CHANGED")
            expected_total = total
            if paging["index"] != (page - 1) * paging["pageSize"] or len(batch) != min(paging["pageSize"], max(0, total - paging["index"])):
                raise SourceError("FILE_RELATIONS_INCOMPLETE")
            for item in batch:
                if item["type"] not in types or (item["id"], item["type"]) in seen:
                    raise SourceError("FILE_RELATIONS_UNKNOWN_OR_DUPLICATE")
                seen.add((item["id"], item["type"]))
                self.remember_project(item)
                dep = {"modId": item["id"], "relationType": types[item["type"]]}
                for field in ("fileId", "versionRange", "versionConstraint"):
                    if item.get(field):
                        dep[field] = item[field]
                deps.append(dep)
            if len(seen) >= total:
                break
            page += 1
        detail.update(dependencies=deps, _details_complete=True, _dependencies_source=url)
        self.file_cache[key] = detail
        if self.evidence_dir:
            (self.evidence_dir / f"file-{pid}-{fid}.json").write_text(json.dumps(detail, indent=2))
        return detail

    async def close(self):
        await self.client.aclose()
        if self.browser and not self.remote_browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
