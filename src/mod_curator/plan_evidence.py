"""Exact-file evidence supplied with a preflight. Links are recorded, never executed."""
from __future__ import annotations

from collections import defaultdict
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def https_url(value):
    u = urlparse(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port not in (None, 443):
        raise ValueError('Use a public HTTPS evidence link')
    return value


class DependencyEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: int = Field(gt=0)
    file_id: int = Field(gt=0)
    dependency_id: int | None = Field(default=None, gt=0)
    dependency_file_id: int | None = Field(default=None, gt=0)
    version_range: str | None = Field(default=None, max_length=200)
    when_project_id: int | None = Field(default=None, gt=0)
    kind: Literal['author', 'jar', 'platform']
    url: str = Field(max_length=1000)
    evidence_version: str = Field(min_length=1, max_length=150)
    disposition: Literal['require', 'disputed', 'accept_platform_override', 'note']
    reason: str = Field(min_length=1, max_length=700)
    _url = field_validator('url')(https_url)

    @model_validator(mode='after')
    def check_requirement(self):
        if self.disposition in ('require', 'accept_platform_override') and self.dependency_id is None:
            raise ValueError('This evidence requires an exact dependency project ID')
        if self.disposition == 'accept_platform_override' and (self.kind == 'platform' or self.when_project_id):
            raise ValueError('An override needs unconditional author or JAR evidence')
        return self


def grouped(values):
    rules = defaultdict(list)
    for value in values:
        rule = DependencyEvidence.model_validate(value).model_dump()
        rules[rule['project_id'], rule['file_id']].append(rule)
    return rules


def apply_evidence(file, rules, *, proposed_ids):
    conflicts = []
    active = [rule for rule in rules if not rule.get('when_project_id') or rule['when_project_id'] in proposed_ids]
    deps = file['dependencies'].copy()
    for rule in active:
        did = rule['dependency_id']
        action = rule['disposition']
        if action == 'disputed':
            conflicts.append(rule)
        elif action == 'accept_platform_override':
            deps = [dep for dep in deps if not (dep['modId'] == did and dep['relationType'] == 3)]
        elif action == 'require':
            edge = {'modId': did, 'relationType': 3, 'evidence': rule}
            if fid := rule['dependency_file_id']:
                edge['fileId'] = fid
            if expr := rule['version_range']:
                edge['versionRange'] = expr
            deps.append(edge)
    return {**file, '_platform_dependencies': file['dependencies'], 'dependencies': deps, '_dependency_evidence': active}, conflicts


class Replacement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    previous_project: int = Field(gt=0)
    replacement_project: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=700)
