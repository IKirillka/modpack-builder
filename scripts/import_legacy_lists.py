#!/usr/bin/env python3
"""Import explicitly identified old report rows without touching selection or decisions."""
import argparse
import csv
import json
from pathlib import Path

from mod_curator.composition import Composition
from mod_curator.engine import Engine
from mod_curator.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job_id')
    parser.add_argument('revision', type=int)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    path = args.report.resolve(strict=True)
    with path.open(encoding='utf-8-sig', newline='') as report:
        rows = list(csv.DictReader(report))
    store = Store()
    engine = Engine(store)

    def import_lists(job):
        engine.idle(job)
        if job['selection_revision'] != args.revision or 'composition' in job or 'legacy_reports' in job:
            raise ValueError('Import requires the exact unconverted legacy revision')
        main_ids = {int(row['project_id']) for row in rows if row['status'] == 'основной отбор'}
        all_ids = [row['project_id'] for row in rows]
        if main_ids != set(job['selection']) or len(set(all_ids)) != len(all_ids) or set(all_ids) != job['projects'].keys():
            raise ValueError('The report does not exactly match this job and its main list')
        reserve, excluded, manual = [], {}, {}
        for row in rows:
            pid = row['project_id']
            if row['url'] != job['projects'][pid]['url']:
                raise ValueError(f'Report URL differs for {pid}')
            label = row['status']
            if label == 'дополнительный вариант':
                reserve.append(int(pid))
            if label.startswith('исключён'):
                excluded[pid] = {'from': 'reserve' if label == 'исключённый резервный вариант по скачиваниям' else None,
                                 'reason': row['explanation'] or label}
            if label == 'исключён по запросу':
                manual[pid] = {'list': 'excluded', 'reason': row['explanation'] or label}
        job.update(legacy_reserve=reserve, legacy_excluded=excluded, legacy_manual=manual,
                   legacy_reports={'source_csv': str(path), 'source_revision': args.revision})
        return {'reserve': len(reserve), 'excluded': len(excluded), 'explicit_exclusions': len(manual)}

    result = store.mutate(args.job_id, import_lists)
    result['table'] = Composition(engine).write_table(args.job_id, args.revision)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
