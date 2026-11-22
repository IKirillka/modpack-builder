"""Explicit, reversible mod-directory updates in managed Prism test instances."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

from filelock import FileLock

from .verification import audit_prism, file_digests, jar_inventory, prism_mods, regular, write_report

MARKER = '.modpack-builder-managed.json'


def _state(instance, mods):
    manifest = instance / 'flame/manifest.json'
    return {'components': file_digests(instance / 'mmc-pack.json', ())['sha256'],
            'manifest': file_digests(manifest, ())['sha256'] if regular(manifest) else None,
            'jars': jar_inventory(mods)}


def _marker(instance):
    path = instance / MARKER
    if not regular(path):
        raise ValueError('This instance is not registered as a managed test instance')
    return json.loads(path.read_text(encoding='utf-8'))


def register(store, export_id, instance):
    instance = instance.resolve()
    with FileLock(instance / '.modpack-builder.lock', timeout=0):
        if (instance / MARKER).exists():
            raise ValueError('This instance is already registered')
        result = audit_prism(store, export_id, instance, persist=False)
        if not result['verified']:
            raise ValueError(f"Only a verified test instance can be registered: {result['errors']}")
        write_report(instance / MARKER, {'registration': uuid.uuid4().hex, 'export_job_id': export_id})
    return {'registered': str(instance), 'export_job_id': export_id}


def preview(store, export_id, instance, staged):
    instance, staged = instance.resolve(), staged.resolve()
    if instance == staged or staged.is_relative_to(instance) or instance.is_relative_to(staged):
        raise ValueError('Provide a separate imported instance as the update source')
    managed = _marker(instance)
    target = audit_prism(store, export_id, staged, persist=False)
    if not target['verified']:
        raise ValueError(f"Staged instance does not match the export: {target['errors']}")
    before, after = _state(instance, prism_mods(instance)), _state(staged, prism_mods(staged))
    if before['components'] != after['components']:
        raise ValueError('Different Prism components: import a separate instance for a loader or game version change')
    old, new = before['jars'], after['jars']
    token = hashlib.sha256(json.dumps([str(instance), str(staged), export_id, managed, before, after],
                                     sort_keys=True).encode('utf-8')).hexdigest()
    return {'instance': str(instance), 'staged': str(staged), 'export_job_id': export_id, 'preview_token': token,
            'added': sorted(new.keys() - old.keys()), 'removed': sorted(old.keys() - new.keys()),
            'changed': sorted(name for name in old.keys() & new.keys() if old[name] != new[name]),
            'other_files_moved_to_backup': sorted(file.name for file in prism_mods(instance).iterdir()
                                                  if not file.name.lower().endswith('.jar') and file.name != '.index'),
            'before': before, 'after': after}


def _copy_files(staged_mods, staged_dir):
    staged_dir.mkdir()
    for file in staged_mods.iterdir():
        if file.name.lower().endswith('.jar'):
            if not regular(file):
                raise ValueError(f'Unsafe staged JAR: {file.name}')
            shutil.copyfile(file, staged_dir / file.name)
    index = staged_mods / '.index'
    if index.is_dir() and not index.is_symlink() and index.resolve().parent == staged_mods.resolve():
        (staged_dir / '.index').mkdir()
        for file in index.glob('*.pw.toml'):
            if regular(file):
                shutil.copyfile(file, staged_dir / '.index' / file.name)


def apply(store, export_id, instance, staged, confirmation):
    instance, staged = instance.resolve(), staged.resolve()
    with FileLock(instance / '.modpack-builder.lock', timeout=0):
        check = preview(store, export_id, instance, staged)
        if confirmation != check['preview_token']:
            raise ValueError('Instance or staged files changed. Preview again.')
        mods = prism_mods(instance)
        change = mods.parent / f'.builder-change-{uuid.uuid4().hex}'
        change.mkdir(mode=0o700)
        write_report(change / 'change.json', {**check, 'previous_marker': _marker(instance)})
        prepared = change / 'new-mods'
        _copy_files(prism_mods(staged), prepared)
        if jar_inventory(prepared) != check['after']['jars']:
            raise ValueError('Staged files changed while copying')
        manifest = instance / 'flame/manifest.json'
        if manifest.parent.exists() and manifest.parent.resolve() != manifest.parent:
            raise ValueError('The target manifest directory is redirected')
        staged_manifest = staged / 'flame/manifest.json'
        if regular(manifest):
            shutil.copyfile(manifest, change / 'before-manifest.json')
        if regular(staged_manifest):
            shutil.copyfile(staged_manifest, change / 'new-manifest.json')
            if file_digests(change / 'new-manifest.json', ())['sha256'] != check['after']['manifest']:
                raise ValueError('Staged manifest changed while copying')
        if _state(instance, mods) != check['before']:
            raise ValueError('Target changed during preparation. Preview again.')
        mods.rename(change / 'before-mods')
        try:
            prepared.rename(mods)
            if (change / 'new-manifest.json').exists():
                manifest.parent.mkdir(exist_ok=True)
                (change / 'new-manifest.json').replace(manifest)
            elif regular(manifest):
                manifest.unlink()
            after = audit_prism(store, export_id, instance)
            if not after['verified']:
                raise ValueError(f"Post-update verification failed: {after['errors']}")
            write_report(instance / MARKER, {**_marker(instance), 'export_job_id': export_id,
                                              'rollback_directory': str(change)})
        except BaseException:
            _restore(instance, mods, change)
            raise
    return {'status': 'verified', 'snapshot': after, 'rollback_directory': str(change)}


def _restore(instance, mods, change):
    if mods.exists():
        mods.rename(change / 'replaced-mods')
    (change / 'before-mods').rename(mods)
    manifest = instance / 'flame/manifest.json'
    if regular(change / 'before-manifest.json'):
        (change / 'before-manifest.json').replace(manifest)
    elif regular(manifest):
        manifest.unlink()
    before = json.loads((change / 'change.json').read_text(encoding='utf-8'))
    write_report(instance / MARKER, before['previous_marker'])


def rollback(instance, directory, confirmation):
    instance = instance.resolve()
    change = directory.resolve()
    if change.parent.name not in {'minecraft', '.minecraft'} or not change.parent.parent == instance or not re.fullmatch(r'\.builder-change-[a-f0-9]{32}', change.name):
        raise ValueError('Rollback directory does not belong to this instance')
    with FileLock(instance / '.modpack-builder.lock', timeout=0):
        _marker(instance)
        saved = json.loads((change / 'change.json').read_text(encoding='utf-8'))
        if confirmation != saved['preview_token']:
            raise ValueError('Pass the preview token associated with this recorded update')
        mods = change.parent / 'mods'
        if not (change / 'before-mods').is_dir():
            raise ValueError('No pending rollback backup')
        if mods.exists() and _state(instance, mods)['jars'] != saved['after']['jars']:
            raise ValueError('The active instance was edited after the update. Leave it intact for inspection.')
        _restore(instance, mods, change)
        restored = _state(instance, mods)
        if restored != saved['before']:
            raise ValueError(f'Rollback restored a snapshot with discrepancies. See {change}')
    return {'status': 'rolled_back', 'instance': str(instance), 'snapshot': restored}
