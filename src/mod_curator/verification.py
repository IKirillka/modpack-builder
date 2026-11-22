"""Local, read-only audits of saved catalogue pages and an explicitly named Prism instance.

Only reports/state in the application's service folder are written. No game is launched.
"""
from __future__ import annotations

import hashlib
import re
import stat
import tempfile
import json
from pathlib import Path
import time
import tomllib
import uuid
import zipfile

from . import captures
from .jar_validation import validate_pack
from .models import Filters
from .source import parse_catalog


def audit_collection(store, job_id):
    job = store.get(job_id)
    filters = Filters.model_validate(job["filters"])
    pages, extracted, selected, seen, issues = [], [], [], set(), []
    previous = None
    boundary = None
    scope = None
    expected_number = 1
    accepted_set = set()
    active_filters = filters
    for saved in job["pages"]:
        number = saved["number"]
        index = saved.get('search_scope_index')
        if index != scope:
            scope = index
            active_filters = Filters.model_validate({**job['filters'], **job['search_scopes'][index]['filters']})
            expected_number, previous = 1, None
            seen.clear()
        if number != expected_number:
            issues.append("Captured catalogue pages are not consecutive within their scope")
        expected_number = number + 1
        file = captures.catalog_path(store.job_dir(job_id), saved)
        parsed = parse_catalog(captures.read(file), saved["url"], active_filters, number)
        ids = [p["id"] for p in parsed.cards]
        downloads = [p["downloads"] for p in parsed.cards]
        if ids != saved["ids"] or downloads != saved["downloads"]:
            issues.append(f"Page {number}: saved state differs from captured HTML")
        accepted = []
        for card in parsed.cards:
            if card["id"] in seen:
                issues.append(f"Duplicate project {card['id']}")
            seen.add(card["id"])
            if previous is not None and card["downloads"] > previous:
                issues.append("Observed order is not descending")
            previous = card["downloads"]
            extracted.append(card)
            if card["downloads"] > active_filters.minimum_downloads:
                accepted.append(card["id"])
                if card["id"] not in accepted_set:
                    selected.append(card["id"])
                    accepted_set.add(card["id"])
            elif boundary is None:
                boundary = {k: card[k] for k in ("id", "name", "downloads", "page", "position")}
        pages.append({"number": number, "url": saved["url"], "cards": len(parsed.cards),
                      "accepted": len(accepted), "accepted_ids": accepted,
                      "cards_detail": parsed.cards, "evidence": parsed.evidence,
                      "search_scope_index": index, "minimum_downloads": active_filters.minimum_downloads})
    if selected != job["order"]:
        issues.append("An eligible captured project is missing/reordered in the collected list")
    if boundary is None and job.get("collection_reason", job["reason"]) == "threshold":
        issues.append("No observed boundary for threshold stop")
    result = {"job_id": job_id, "audited_at": time.time(), "filters": filters.model_dump(),
              "page_size": filters.page_size, "pages": pages, "extracted_cards": len(extracted),
              "search_scopes": job.get("search_scopes", []),
              "collected_projects": len(selected), "exact_counter_field": "tileProjects[].downloadsCount",
              "first_excluded": boundary, "stop_reason": job.get("collection_reason", job["reason"]),
              "no_omissions_in_captured_pages": not issues, "issues": issues,
              "catalog_total_at_capture": job["pages"][0]["total"] if pages else None,
              "scope": "Verified against captured HTML/DOM and SQLite, not a frozen snapshot of unseen later pages",
              "global_snapshot_guaranteed": False}
    path = store.job_dir(job_id) / "collection-audit.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    store.patch(job_id, collection_audit=result)
    return result


HASHES = frozenset(('md5', 'sha1', 'sha256', 'sha512'))


def regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) &
                                              getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def file_digests(path, algorithms):
    digests = {name: hashlib.new(name) for name in set(algorithms) | {'sha256'} if name in HASHES}
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            for digest in digests.values():
                digest.update(chunk)
    return {name: digest.hexdigest() for name, digest in digests.items()}


def checked_artifact(item, actual):
    expected = item.get('hashes') or {}
    return all(actual.get(name, '').lower() == value.lower() for name, value in expected.items())


def write_report(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.part', delete=False) as tmp:
        temporary = Path(tmp.name)
        try:
            tmp.write(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
            tmp.close()
            temporary.replace(path)
        finally:
            tmp.close()
            temporary.unlink(missing_ok=True)
    return str(path)


def receipt(job_id, job, report, snapshot=None):
    plan = job.get('export_plan') or {}
    parent = job.get('exported_from') or {}
    return {
        'format_version': 1, 'user_request': plan.get('request'),
        'job_id': parent.get('job_id') or plan.get('job_id') or job_id,
        'revision': parent.get('revision', plan.get('revision', job['selection_revision'])),
        'plan_task_id': plan.get('task_id'), 'plan_input_key': plan.get('input_key'),
        'export_job_id': job_id, 'zip': {key: report['path' if key == 'path' else 'sha256']
                                         for key in ('path', 'sha256')},
        'stages': report.get('verification_stages', {}),
        'items': plan.get('items', []), 'replacements': plan.get('replacements', []),
        'installation_snapshot': snapshot,
    }


def write_receipt(store, job_id, job, report):
    data = receipt(job_id, job, report, job.get('prism_import_verification'))
    return write_report(store.job_dir(job_id) / 'installation-receipt.json', data)


def prism_mods(instance):
    for base in ('minecraft', '.minecraft'):
        path = instance / base / 'mods'
        if (path.is_dir() and not path.is_symlink() and
                not getattr(path, 'is_junction', lambda: False)() and path.resolve().is_relative_to(instance.resolve())):
            return path
    raise ValueError('Prism instance mods directory was not found or is redirected')


def jar_inventory(mods, algorithms=()):
    inventory = {}
    for file in sorted(mods.iterdir()):
        if file.name.lower().endswith('.jar'):
            inventory[file.name] = {'size': file.stat().st_size, 'hashes': file_digests(file, algorithms)} if regular(file) else {
                'status': 'not_regular', 'size': None, 'hashes': {}}
    return inventory


def compare_inventory(expected, actual):
    result = {'matched': [], 'missing': [], 'extra': [], 'different': [], 'unverified': []}
    names = {item['file_name'] for item in expected}
    for item in expected:
        fact = {'item': item, 'jar': actual.get(item['file_name'])}
        observed = fact['jar']
        if observed is None:
            outcome = 'missing'
        elif (observed.get('status') == 'not_regular' or
              (item.get('size') and observed['size'] != item['size']) or
              not checked_artifact(item, observed['hashes'])):
            outcome = 'different'
        elif not item.get('hashes'):
            outcome = 'unverified'
        else:
            outcome = 'matched'
        result[outcome].append(fact)
    result['extra'] = [{'file_name': name, **info} for name, info in actual.items() if name not in names]
    result['expected_count'] = len(expected)
    result['actual_count'] = len(actual)
    return result


def _prism_expected(job, manifest, mods, issues):
    recorded = (job.get('export_plan') or {}).get('items')
    lock = job.get('lock', {}).get('files', {})
    if recorded is None:
        recorded = []
        for entry in manifest['files']:
            pid, fid = entry['projectID'], entry['fileID']
            file = lock.get(str(pid), {})
            if not file.get('fileName'):
                issues.append(f'Missing frozen file info: {pid}/{fid}')
                continue
            recorded.append({'source': 'curseforge', 'project_id': pid, 'file_id': fid,
                             'file_name': file['fileName'], 'size': file.get('fileLength'),
                             'hashes': {{1: 'sha1', 2: 'md5'}[x['algo']]: x['value'] for x in file.get('hashes', ())
                                        if x.get('algo') in (1, 2)},
                             'role': 'selected' if pid in job['selection'] else 'dependency',
                             'reason': 'Included in the saved export lock'})
    index = {}
    for path in (mods / '.index').glob('*.pw.toml'):
        if not regular(path) or not path.resolve().is_relative_to(mods.resolve()):
            issues.append(f'Unsafe Prism index: {path.name}')
            continue
        try:
            obj = tomllib.loads(path.read_text(encoding='utf-8'))
            cf = obj.get('update', {}).get('curseforge')
            if not cf:
                continue
            key = (cf['project-id'], cf['file-id'])
            if key in index:
                issues.append(f'Duplicate Prism index entry: {key}')
            index[key] = obj
        except (ValueError, KeyError, TypeError) as e:
            issues.append(f'Invalid Prism index {path.name}: {e}')
    known_cf = {(item['project_id'], item['file_id']) for item in recorded if item['source'] == 'curseforge'}
    expected = []
    for item in recorded:
        entry = {**item, 'hashes': dict(item.get('hashes') or {})}
        if item['source'] == 'curseforge':
            key = item['project_id'], item['file_id']
            index_file = index.get(key)
            if index_file and index_file.get('filename') == item['file_name']:
                download = index_file.get('download', {})
                algo, digest = download.get('hash-format'), download.get('hash')
                if algo in HASHES and isinstance(digest, str) and digest:
                    if algo in entry['hashes'] and entry['hashes'][algo].lower() != digest.lower():
                        issues.append(f'Prism index hash disagrees with the plan: {key}')
                    else:
                        entry['hashes'][algo] = digest
                        entry['prism_index_hash'] = algo
                for dep in index_file.get('x-prismlauncher-dependencies', ()):
                    if dep.get('type') == 'REQUIRED' and str(dep.get('addonId')) not in {str(x[0]) for x in known_cf}:
                        issues.append(f'Prism index reports missing required dependency: {key} -> {dep.get("addonId")}')
            elif index_file:
                issues.append(f'Prism filename disagrees with the plan: {key}')
        expected.append(entry)
    return expected


def component_graph(components, meta_root):
    issues, unchecked = [], []
    for uid, version in components.items():
        if not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]{0,130}', v)
                   for v in (uid, version)):
            issues.append(f'Unsafe component identity: {uid}/{version}')
            continue
        path = meta_root / uid / (version + '.json')
        if not regular(path) or not path.resolve().is_relative_to(meta_root.resolve()):
            unchecked.append(f'Metadata unavailable: {uid}/{version}')
            continue
        try:
            metadata = json.loads(path.read_text(encoding='utf-8'))
            for req in metadata.get('requires', ()):
                target, precise = req['uid'], req.get('equals')
                if target not in components or (precise and precise != components[target]):
                    issues.append(f'{uid} requires {target} {precise or req.get("suggests") or "*"}')
        except (ValueError, TypeError, KeyError, OSError) as exc:
            unchecked.append(f'Unreadable component metadata {uid}/{version}: {exc}')
    return {'issues': issues, 'unchecked': unchecked}


def audit_prism(store, job_id, instance, log_file=None, *, persist=True):
    from .modrinth import safe_jar_name

    job = store.get(job_id)
    report = job['pack_export']
    archive = Path(report['path'])
    if not regular(archive) or file_digests(archive, ())['sha256'] != report['sha256']:
        raise ValueError('The recorded export is unavailable or its SHA-256 changed')
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read('manifest.json'))
    manifest_path = instance / 'flame/manifest.json'
    imported = json.loads(manifest_path.read_text(encoding='utf-8')) if regular(manifest_path) else None
    components = {row['uid']: row['version'] for row in json.loads(
        (instance / 'mmc-pack.json').read_text(encoding='utf-8'))['components'] if not row.get('disabled')}
    filters = Filters.model_validate(job['filters'])
    loader = report['loader']
    issues = []
    if imported is not None and imported != manifest:
        issues.append('Prism manifest differs from the recorded ZIP')
    if components.get('net.minecraft') != filters.minecraft or components.get(loader['uid']) != loader['version']:
        issues.append('Minecraft/loader differs from the recorded ZIP')
    graph = component_graph(components, instance.parent.parent / 'meta')
    mods = prism_mods(instance)
    expected = _prism_expected(job, manifest, mods, issues)
    for item in expected:
        safe_jar_name(item['file_name'])
    algorithms = {algo for item in expected for algo in item['hashes']}
    differences = compare_inventory(expected, jar_inventory(mods, algorithms))
    paths = [mods / match['item']['file_name'] for match in differences['matched'] + differences['unverified']]
    jar_report = validate_pack(paths, filters, {filters.loader.lower(): loader['version']})
    errors = [*issues, *jar_report['errors'], *jar_report['unresolved']]
    for group in ('missing', 'extra', 'different', 'unverified'):
        if differences[group]:
            errors.append(f"{group.upper()}_JARS: {len(differences[group])}")
    stages = {**report.get('verification_stages', {}),
              'import': 'inventory_matches' if not errors else 'discrepancies',
              'launcher_components': 'discrepancies' if graph['issues'] else 'unverified' if graph['unchecked'] else 'resolved',
              'main_menu': 'untested', 'world': 'untested', 'mechanics': 'untested'}
    logs = []
    if log_file:
        with Path(log_file).open(encoding='utf-8', errors='replace') as stream:
            logs = [line.strip()[:600] for line in stream if any(signal in line for signal in (
                    'Starting integrated minecraft server', 'Game took', 'Checksum mismatch', 'Caused by:'))][-40:]
        if any('Starting integrated minecraft server' in line for line in logs):
            stages['world'] = 'server_started_in_supplied_log'
    errors.extend(graph['issues'])
    snapshot = {'job_id': job_id, 'export_sha256': report['sha256'], 'plan_task_id': report.get('installation_plan_task'),
                'audited_at': time.time(), 'instance': str(instance), 'manifest_matches_export': imported == manifest,
                'components': components, 'component_graph': graph, 'inventory': differences, 'jar_dependencies': jar_report,
                'verified': not errors, 'errors': errors, 'log_evidence': logs, 'verification_stages': stages}
    if persist:
        snapshot['snapshot_id'] = uuid.uuid4().hex
        snapshot['report_path'] = str(store.job_dir(job_id) / 'prism-import-verification.json')
        export_report = {**report, 'prism_import_tested': True, 'verification_stages': stages}
        snapshot['receipt_path'] = report.get('receipt_path') or str(store.job_dir(job_id) / 'installation-receipt.json')
        data = receipt(job_id, job, export_report, snapshot)
        write_report(Path(snapshot['report_path']), snapshot)
        write_report(Path(snapshot['receipt_path']), data)
        def save_snapshot(db, _):
            db.execute('INSERT INTO installation_snapshots VALUES(?,?,?,?)',
                       (job_id, snapshot['snapshot_id'], snapshot['audited_at'], json.dumps(data, ensure_ascii=False)))

        store.mutate(job_id, lambda row: row.update(prism_import_verification=snapshot,
                     pack_export=export_report, installation_receipt=data), after=save_snapshot)
    return snapshot
