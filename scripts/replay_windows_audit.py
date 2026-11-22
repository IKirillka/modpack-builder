#!/usr/bin/env python3
"""Replay normalized Windows audit responses in an isolated state directory."""
import argparse
import asyncio
from collections import defaultdict
import json
from pathlib import Path
import re
import time
import zipfile

from mod_curator.engine import Engine
from mod_curator.models import Filters, SourceError
from mod_curator.plan_evidence import grouped
from mod_curator.preflight import schedule
from mod_curator.resolver import ResolutionError, resolve
from mod_curator.source import check_file_page
from mod_curator.store import Store
from mod_curator.verification import compare_inventory, write_report


def read_audit(path):
    saved = defaultdict(dict)
    with zipfile.ZipFile(path) as z:
        paths = z.namelist()

        def document(tail):
            return json.loads(z.read(next(name for name in paths if name.endswith(tail))))

        jobs = []
        for line in z.open('03_BUILDER/snapshot/jobs.jsonl'):
            row = json.loads(line)['data']
            jobs.append(json.loads(row) if isinstance(row, str) else row)
        trial = max((j for j in jobs if j.get('check_options')), key=lambda j: j['timings'].get('pack_check', 0))
        for name in paths:
            if re.search(r'/file-\d+-\d+\.json$', name):
                row = json.loads(z.read(name))
                saved[row['modId']][row['id']] = row
        captured = {}
        for line in z.open('02_TOOLS/tool-events.jsonl'):
            if b'immediatelyfast' not in line.lower() or b'totalCount' not in line:
                continue
            for element in json.loads(line).get('output', ()):
                try:
                    content = json.loads(element['text'])['output']
                except (KeyError, ValueError, TypeError):
                    continue
                for page, index, size, total, count in re.findall(
                        r"(\d+) \{'index': (\d+), 'pageSize': (\d+), 'totalCount': (\d+), 'totalPages': \d+\} (\d+) \[",
                        content):
                    if int(count):
                        captured[int(page)] = {'index': int(index), 'size': int(size), 'total': int(total), 'rows': int(count)}
        old_names = re.findall(r'`([^\n`]+\.jar)`', z.read('05_PRISM/instance/inventory-from-chat.md').decode())
        live = document('/active-mods-with-sha256.json')
        history = document('/exact-file-plan-r13-author-requirements.json')
    return jobs, trial, saved, captured, old_names, live, history


class SavedSource:
    def __init__(self, files):
        self.saved = files
        self.requests = []

    async def files(self, pid, filters):
        self.requests.append(pid)
        if pid not in self.saved:
            raise SourceError(f'AUDIT_RESPONSE_MISSING: {pid}')
        return list(self.saved[pid].values())

    async def close(self):
        pass


async def replay(args):
    jobs, trial, saved, pages, old_names, live, history = read_audit(args.archive)
    checks = {}
    for mode in ('captured', 'offset_from_captured_counts'):
        formats = {'offset', 'page', 'zero_based_page'}
        counts = []
        for page, row in sorted(pages.items()):
            paging = {'index': row['index'] if mode == 'captured' else (page - 1) * row['size'], 'totalCount': row['total']}
            check_file_page(paging, row['rows'], page, row['size'], row['total'], formats)
            counts.append(row['rows'])
        checks[mode] = {'counts': counts, 'formats': sorted(formats)}
    categories = {}
    for job in jobs:
        group = job['filters']['categories']
        if group in (['create'], ['performance']) and job['pages']:
            threshold = job['filters']['minimum_downloads']
            counts = [n for p in job['pages'] for n in p['downloads']]
            categories[group[0]] = {'threshold_exclusive': threshold, 'pages': len(job['pages']),
                                    'cards': len(counts), 'eligible': sum(n > threshold for n in counts)}
    options = trial['check_options']['inputs']
    cf = Filters.model_validate(trial['filters'])
    engine = Engine(Store(args.state))
    engine.launch = lambda job_id: {'job_id': job_id}
    root = engine.store.create(cf.model_dump(), selection=options['roots'], selection_revision=13,
                               projects=trial['projects'], file_pins=options['pins'],
                               stage='awaiting_analysis', status='awaiting_analysis')
    task = schedule(engine, root, 13, user_request='Replay of the Windows audit with 99 selected mods')['job_id']
    source = SavedSource(saved)
    begin = time.perf_counter()
    await engine.run(task, source)
    elapsed = time.perf_counter() - begin
    result = engine.store.get(task)
    structure = await resolve(SavedSource(saved), [1010066], cf, excluded=(624165, 306612))
    assert structure['files']['1010066']['id'] == 6354819
    exception = history['exceptions'][0]
    row = {'project_id': exception['project_id'], 'file_id': exception['file_id'],
           'dependency_id': exception['dependency'], 'kind': 'author', 'url': exception['source'],
           'evidence_version': '1.3.2', 'disposition': 'disputed', 'reason': 'Author source and file platform differ'}
    try:
        await resolve(SavedSource(saved), [row['project_id']], cf, pins={row['project_id']: row['file_id']},
                      evidence=grouped([row]))
    except ResolutionError as exc:
        escalated = exc.issues[0]
    else:
        raise AssertionError('The unadjudicated metadata conflict must block')
    inventory = {x['file']: {'size': x['size'], 'hashes': {'sha256': x['sha256']}, 'provenance': 'archived snapshot'}
                 for x in live}
    diff = compare_inventory([{'file_name': n, 'role': 'unknown', 'reason': 'Listed in earlier chat inventory'}
                              for n in old_names], inventory)
    assert diff['expected_count'] == 231 and diff['actual_count'] == 232
    assert [x['file_name'] for x in diff['extra']] == ['Chunky-NeoForge-1.4.23.jar']
    diff['extra_first_seen'] = 'unknown'
    diff['comparison_basis'] = 'Earlier list has filenames only; current hashes are from the archived inventory'
    jar_report = write_report(args.state / 'archive-jar-reconciliation.json', diff)
    from mod_curator.jar_validation import compare_maven
    sample = {}
    for key, prefix in {'escalated': 'escalated-', 'vintage': 'vintage-', 'jei': 'jei-1.21.1',
                        'xaero': 'xaeroworldmap-', 'mezzconfig': 'mezz_config-',
                        'kubejs_create': 'kubejs-create-'}.items():
        found = next(row for row in live if row['file'].startswith(prefix))
        sample[key] = {'file': found['file'], 'sha256': found['sha256'], 'mod_ids': [
            x.get('modId') for x in found['mod_metadata']], 'dependencies': found.get('dependencies')}
    sample['jei']['minecraft_checks'] = [
        {'range': d['versionRange'], 'matches_minecraft_1_21_1': compare_maven('1.21.1', d['versionRange'])}
        for d in sample['jei']['dependencies']['jei'] if d['modId'] == 'minecraft']
    report = {'catalogue': categories, 'check_99': {'task': task, 'roots': len(options['roots']),
              'available_exact_responses': sum(map(len, saved.values())), 'status': result['status'],
              'archived_elapsed': trial['timings']['pack_check'], 'seconds': elapsed,
              'source_requests': len(source.requests), 'progress': result.get('activity'),
              'blockers': result.get('check_plan', {}).get('blockers', []),
              'full_live_reproduced': False},
              'immediatelyfast': checks, 'structures_arise': structure['alternatives'], 'escalated': escalated,
              'author_documentation_edges': history['author_documentation_edges'],
              'author_documentation_unresolved': history['author_documentation_unresolved'],
              'saved_jar_metadata': sample, 'jar_report': jar_report, 'inventory_counts': {k: diff[k] for k in ('expected_count', 'actual_count')}}
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('state', type=Path, help='New, separate directory for replay SQLite and reports')
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=False)
    asyncio.run(replay(args))
