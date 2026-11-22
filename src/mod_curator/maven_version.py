"""Compare NeoForge version tokens using Maven's documented ordering.

Reference: https://maven.apache.org/pom.html#version-order-specification
"""
from functools import lru_cache
from itertools import zip_longest
import re

QUALIFIERS = ('alpha', 'beta', 'milestone', 'rc', 'snapshot', '', 'sp')
ALIASES = {'ga': '', 'final': '', 'release': '', 'cr': 'rc'}
SHORT = {'a': 'alpha', 'b': 'beta', 'm': 'milestone'}
RELEASE = (QUALIFIERS.index(''), '')


def _qualifier(value):
    if value in QUALIFIERS:
        return QUALIFIERS.index(value), ''
    return len(QUALIFIERS), value


@lru_cache(maxsize=4096)
def parse(version):
    if not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._+\-]{0,191}', version):
        return None
    root = []
    frame = root
    parts = re.split(r'([.-])', version.lower())
    for index, part in enumerate(parts):
        more = index + 1 < len(parts)
        if part == '.':
            continue
        if part == '-':
            child = []
            frame.append(child)
            frame = child
            continue
        if not part:
            if more:
                frame.append(0)
            continue
        runs = list(re.finditer(r'[0-9]+|[^0-9]+', part))
        for position, run in enumerate(runs):
            value = run[0]
            following = position + 1 < len(runs)
            if value.isdecimal():
                frame.append(int(value))
            else:
                if frame and (following or not more):
                    child = []
                    frame.append(child)
                    frame = child
                label = SHORT.get(value, value) if following else value
                frame.append(ALIASES.get(label, label))
            if following:
                child = []
                frame.append(child)
                frame = child

    def normalize(items):
        items = [normalize(item) if isinstance(item, list) else item for item in items]
        for i in range(len(items) - 1, -1, -1):
            if items[i] in (0, '', ()):
                del items[i]
            elif not isinstance(items[i], tuple):
                break
        return tuple(items)
    return normalize(root)


def _cmp(left, right):
    return (left > right) - (left < right)


def compare(left, right):
    if right is None:
        if isinstance(left, tuple):
            return next((outcome for x in left if (outcome := compare(x, None))), 0)
        if isinstance(left, str):
            return _cmp(_qualifier(left), RELEASE)
        return _cmp(left or 0, 0)
    if left is None:
        return -compare(right, None)
    if type(left) is not type(right):
        order = {str: 0, tuple: 1, int: 2}
        return _cmp(order[type(left)], order[type(right)])
    if isinstance(left, tuple):
        return next((outcome for x, y in zip_longest(left, right) if (outcome := compare(x, y))), 0)
    if isinstance(left, str):
        return _cmp(_qualifier(left), _qualifier(right))
    return _cmp(left, right)
