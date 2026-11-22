"""Model choices for the isolated Codex workers and their task boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

ModelId = Literal['gpt-6-luna', 'gpt-6.1-terra', 'gpt-6.1-sol', 'gpt-6.2-sol', 'gpt-6.2-astra']
Effort = Literal['low', 'medium', 'high']

DEFAULT_MODEL = 'gpt-6-luna'
DEFAULT_EFFORT = 'medium'
MODELS = {
    'gpt-6-luna': ('GPT-6 Luna', 'Обычные порции описаний и экономный первый проход.'),
    'gpt-6.1-terra': ('GPT-6.1 Terra', 'Вариант для небольших сложных порций.'),
    'gpt-6.1-sol': ('GPT-6.1 Sol', 'Точечная перепроверка сложных описаний.'),
    'gpt-6.2-sol': ('GPT-6.2 Sol', 'Более сильная модель для плотных технических описаний.'),
    'gpt-6.2-astra': ('GPT-6.2 Astra', 'Вариант для отдельных самых сложных порций.'),
}
ROLE_KEYS = {'model', 'effort', 'retry_model', 'retry_effort'}
ROLE_LABELS = {
    'extract': 'Первый проход',
    'retry': 'Повторная проверка',
}
ROLE_DESCRIPTIONS = {
    'extract': 'Читает только описания из порученной порции. Извлекает механики, интеграции, требования, ограничения и номера фрагментов.',
    'retry': 'Перечитывает описание проекта, если прошлый ответ не прошёл проверку схемы, ID или доказательств либо порция не была сохранена. Исправляет запись и возвращает факты.',
}


def route(settings, role):
    model = settings.get('model') or DEFAULT_MODEL
    effort = settings.get('effort') or DEFAULT_EFFORT
    if role == 'retry':
        retry_model = settings.get('retry_model')
        retry_effort = settings.get('retry_effort')
        if retry_model and retry_model != 'inherit':
            model = retry_model
        if retry_effort and retry_effort != 'inherit':
            effort = retry_effort
    return {'role': role, 'model': model, 'effort': effort}


def separate_retry(settings):
    first, retry = route(settings, 'extract'), route(settings, 'retry')
    return (first['model'], first['effort']) != (retry['model'], retry['effort'])


def role_options(settings):
    roles = []
    for role in ROLE_LABELS:
        current = route(settings, role)
        roles.append({
            **current, 'label': ROLE_LABELS[role], 'task': ROLE_DESCRIPTIONS[role],
            'model_label': MODELS.get(current['model'], (current['model'], ''))[0],
            'inherits_model': role == 'retry' and settings.get('retry_model', 'inherit') == 'inherit',
            'inherits_effort': role == 'retry' and settings.get('retry_effort', 'inherit') == 'inherit',
        })
    return roles


def model_options():
    """A local catalogue hint. Credentials and catalogue instructions are never returned."""
    location = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'models_cache.json'
    try:
        entries = json.loads(location.read_text(encoding="utf-8"))['models']
        listed = {entry['slug'] for entry in entries if entry.get('visibility') == 'list'}
    except (OSError, ValueError, TypeError, KeyError):
        listed = None
    return [
        {'id': model, 'label': label, 'use': purpose, 'efforts': ['low', 'medium', 'high'],
         'listed_in_local_codex_cache': model in listed if listed is not None else None}
        for model, (label, purpose) in MODELS.items()
    ]
