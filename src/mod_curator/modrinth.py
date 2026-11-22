"""Exact Modrinth versions and source-verified artifacts for a local mixed pack."""
from __future__ import annotations

import hashlib
import time
import re
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import SourceError

API = 'https://api.modrinth.com/v2'
TYPES = {'release': 1, 'beta': 2, 'alpha': 3}
MODRINTH_ID = r'^[A-Za-z0-9-]{1,80}$'
MAX_JAR_BYTES = 256 * 1024 * 1024


def safe_jar_name(name):
    if (not isinstance(name, str) or not name.lower().endswith('.jar') or len(name) > 240
            or any(char in name for char in '/\\:<>|"*?') or any(ord(c) < 32 for c in name)
            or name != name.strip() or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])',
                                                   name.split('.')[0].rstrip().upper())):
        raise ValueError('Unsafe JAR filename')
    return name


class ExternalVersion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str = Field(pattern=MODRINTH_ID)
    version_id: str = Field(pattern=MODRINTH_ID)
    reason: str = Field(min_length=1, max_length=600)
    required_by: list[int] = Field(default_factory=list, max_length=100)
    manual_download: bool = False


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(MODRINTH_ID, value):
        raise SourceError('MODRINTH_BAD_ID')
    return value


def cdn_url(url, project, version):
    parsed = urlparse(url or '')
    prefix = f'/data/{project}/versions/{version}/'
    if (parsed.scheme != 'https' or parsed.netloc != 'cdn.modrinth.com' or not parsed.path.startswith(prefix)
            or parsed.fragment or parsed.query):
        raise SourceError('MODRINTH_UNVERIFIED_DOWNLOAD_URL')
    return url


class Modrinth:
    def __init__(self, client):
        self.client = client
        self.network_seconds = 0.0

    async def get(self, endpoint, params=None):
        started = time.monotonic()
        try:
            r = await self.client.get(f'{API}/{endpoint}', params=params, follow_redirects=False)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f'MODRINTH_METADATA_UNAVAILABLE: {endpoint}: {exc}') from exc
        finally:
            self.network_seconds += time.monotonic() - started

    async def version(self, vid):
        return await self.get(f'version/{_id(vid)}')

    async def latest(self, project, filters):
        # The official endpoint filters game versions and loaders. Verify the returned tags as well.
        import json
        matches = await self.get(f'project/{_id(project)}/version', {
            'game_versions': json.dumps([filters.minecraft]), 'loaders': json.dumps([filters.loader.lower()])
        })
        return next((row for row in matches if self.compatible(row, filters)), None)

    @staticmethod
    def compatible(row, f):
        allowed = f.release_types or ([1, 2, 3] if f.allow_prerelease else [1])
        return (f.minecraft in row.get('game_versions', ()) and f.loader.lower() in row.get('loaders', ())
                and TYPES.get(row.get('version_type')) in allowed)

    def item(self, row, f):
        pid, vid = _id(row.get('project_id')), _id(row.get('id'))
        if not self.compatible(row, f):
            raise SourceError(f'MODRINTH_INCOMPATIBLE_FILE: {pid}/{vid}')
        jar_files = [x for x in row.get('files', ()) if str(x.get('filename', '')).lower().endswith('.jar')]
        primary = [x for x in jar_files if x.get('primary')]
        choice = primary or jar_files
        if len(choice) != 1:
            raise SourceError(f'MODRINTH_AMBIGUOUS_FILE: {pid}/{vid}')
        file = choice[0]
        name = safe_jar_name(file['filename'])
        hashes = {k: v.lower() for k, v in file.get('hashes', {}).items()
                  if k in ('sha1', 'sha512') and isinstance(v, str) and re.fullmatch(
                      '[A-Fa-f0-9]{%s}' % (40 if k == 'sha1' else 128), v)}
        if 'sha512' not in hashes or not isinstance(file.get('size'), int) or not 0 < file['size'] <= MAX_JAR_BYTES:
            raise SourceError(f'MODRINTH_UNVERIFIED_HASH_OR_SIZE: {pid}/{vid}')
        if not isinstance(row.get('dependencies'), list):
            raise SourceError(f'MODRINTH_DEPENDENCIES_UNKNOWN: {pid}/{vid}')
        return {'source': 'modrinth', 'project_id': pid, 'file_id': vid, 'version': row['version_number'],
                'file_name': name, 'size': file['size'], 'hashes': hashes,
                'url': f'https://modrinth.com/mod/{pid}/version/{vid}',
                'download_url': cdn_url(file.get('url'), pid, vid),
                'dependencies': row['dependencies'],
                'evidence': {'source': f'{API}/version/{vid}', 'version': vid,
                             'published': row.get('date_published')}}

    async def resolve(self, specs, f, check_cancel):
        items, aliases, entries = {}, {}, []
        pending = []
        # Resolve pinned roots first so an unpinned edge can use them.
        for raw in specs:
            check_cancel()
            spec = ExternalVersion.model_validate(raw)
            row = await self.version(spec.version_id)
            if row.get('id') != spec.version_id:
                raise SourceError(f'MODRINTH_VERSION_ID_CHANGED: {spec.version_id}')
            pid = _id(row.get('project_id'))
            if pid != spec.project_id:
                canonical = await self.get(f'project/{spec.project_id}')
                if canonical.get('id') != pid:
                    raise SourceError(f'MODRINTH_PROJECT_ID_MISMATCH: {spec.project_id}/{spec.version_id}')
            aliases[spec.project_id] = pid
            pending.append((row, spec))
        for row, spec in pending:
            pid = row['project_id']
            if pid in items:
                raise SourceError(f'MODRINTH_DUPLICATE_ROOT: {pid}')
            item = self.item(row, f)
            item.update(role='dependency' if spec.required_by else 'selected', reason=spec.reason,
                        required_by=[f'curseforge:{p}' for p in spec.required_by],
                        acquisition='manual' if spec.manual_download else 'prism_override_confirmation')
            items[pid] = item
            entries.append(item)
        index = 0
        while index < len(entries):
            item = entries[index]
            index += 1
            for dep in item['dependencies']:
                if dep.get('dependency_type') != 'required':
                    continue
                check_cancel()
                target, pin = dep.get('project_id'), dep.get('version_id')
                target = aliases.get(target, target)
                if not target and not pin:
                    raise SourceError(f'MODRINTH_REQUIRED_FILE_UNRESOLVED: {item["project_id"]}: {dep}')
                if target not in items:
                    row = await self.version(pin) if pin else await self.latest(target, f)
                    if row is None or (target and row.get('project_id') != target) or (pin and row.get('id') != pin):
                        raise SourceError(f'MODRINTH_REQUIRED_VERSION_UNAVAILABLE: {item["project_id"]} -> {target}/{pin}')
                    resolved = self.item(row, f)
                    target = resolved['project_id']
                    if target not in items:
                        resolved.update(role='dependency', reason='Required by Modrinth version metadata',
                                        required_by=[], acquisition='prism_override_confirmation')
                        items[target] = resolved
                        entries.append(resolved)
                if pin and items[target]['file_id'] != pin:
                    raise SourceError(f'MODRINTH_REQUIRED_VERSION_CONFLICT: {item["project_id"]} -> {target}/{pin}; selected {items[target]["file_id"]}')
                owner = f'modrinth:{item["project_id"]}'
                if owner not in items[target]['required_by']:
                    items[target]['required_by'].append(owner)
        for item in entries:
            for dep in item['dependencies']:
                if dep.get('dependency_type') == 'incompatible':
                    other = items.get(dep.get('project_id'))
                    if other is None and dep.get('version_id'):
                        other = next((i for i in entries if i['file_id'] == dep['version_id']), None)
                    if other and (not dep.get('version_id') or dep['version_id'] == other['file_id']):
                        raise SourceError(f'MODRINTH_INCOMPATIBLE_DEPENDENCY: {item["project_id"]} -> {other["project_id"]}')
        return entries

    async def verify_item(self, item, f):
        current = self.item(await self.version(item['file_id']), f)
        if any(current[key] != item[key] for key in ('project_id', 'file_id', 'version', 'file_name',
                                                   'hashes', 'size', 'dependencies', 'download_url')):
            raise SourceError(f'INSTALLATION_PLAN_STALE: Modrinth {item["project_id"]}/{item["file_id"]} changed')

    async def download(self, item, path):
        digest = hashlib.sha512()
        size = 0
        async with self.client.stream('GET', item['download_url'], follow_redirects=False) as response:
            response.raise_for_status()
            with path.open('xb') as stream:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > item['size']:
                        raise SourceError(f'MODRINTH_ARTIFACT_SIZE_CHANGED: {item["file_name"]}')
                    digest.update(chunk)
                    stream.write(chunk)
        if size != item['size'] or digest.hexdigest() != item['hashes']['sha512']:
            raise SourceError(f'MODRINTH_ARTIFACT_HASH_MISMATCH: {item["file_name"]}')
