"""Closed structured proposal schemas. No status, permissions or SQL inputs."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import PureWindowsPath
from typing import Any
from .model import ValidationError

ID = re.compile(r'^[A-Za-z0-9_\-]{1,100}$')


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError('Input must be finite JSON') from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf8')).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValidationError('Invalid identifier (1-100 ASCII letters, digits, _ or -)')
    return value


def integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationError('Expected nonnegative integer revision/capacity')
    return value


def text(value: Any, limit: int = 100000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValidationError('Expected nonempty bounded text')
    return value  # Preserve the user's original text, including whitespace.


def string_list(value: Any, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 1000:
        raise ValidationError('Expected bounded string list')
    result = [text(v, 10000) for v in value]
    if nonempty and not result:
        raise ValidationError('List must not be empty')
    if len(set(result)) != len(result):
        raise ValidationError('Duplicate list entry')
    return result


def relative_path(value: Any) -> str:
    raw = text(value, 1000)
    path = PureWindowsPath(raw)
    parts = raw.replace('\\', '/').split('/')
    if path.drive or path.root or any(p in {'', '.', '..'} or ':' in p or p.endswith((' ', '.')) for p in parts):
        raise ValidationError('Only normalized relative paths without traversal/ADS are allowed')
    for part in parts:
        if any(c in part for c in '<>"|?*') or any(ord(c) < 32 for c in part) or path.__class__(part).is_reserved():
            raise ValidationError('Invalid Windows path component')
    return '/'.join(parts)


def object_fields(raw: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(raw, dict) or not all(isinstance(k, str) for k in raw):
        raise ValidationError('Expected structured object')
    if required - raw.keys() or raw.keys() - required - (optional or set()):
        raise ValidationError('Missing or unknown proposal fields')
    return raw


def plan_proposal(raw: Any) -> dict:
    raw = object_fields(raw, {'summary', 'work_items'})
    text(raw['summary'])
    items = raw['work_items']
    if not isinstance(items, list) or not 1 <= len(items) <= 500:
        raise ValidationError('Plan requires 1-500 work items')
    keys = {'work_item_id', 'title', 'description', 'dependencies', 'acceptance_criteria', 'allowed_write_scope'}
    normalized = []
    ids = set()
    for item in items:
        item = object_fields(item, keys)
        wid = identifier(item['work_item_id'])
        if wid in ids:
            raise ValidationError('Duplicate work item ID')
        ids.add(wid)
        normalized.append(dict(work_item_id=wid, title=text(item['title'], 500), description=text(item['description']),
            dependencies=[identifier(d) for d in string_list(item['dependencies'])],
            acceptance_criteria=string_list(item['acceptance_criteria'], nonempty=True),
            allowed_write_scope=[relative_path(p) for p in string_list(item['allowed_write_scope'])]))
    graph = {i['work_item_id']: i['dependencies'] for i in normalized}
    for deps in graph.values():
        if set(deps) - ids:
            raise ValidationError('Unknown, cross-goal or cross-version dependency')
    # Kahn's algorithm avoids recursion limits for long plans.
    remaining = dict(graph)
    done = set()
    while remaining:
        ready = {i for i, deps in remaining.items() if set(deps) <= done}
        if not ready:
            raise ValidationError('Dependency cycle')
        done.update(ready)
        remaining = {i: deps for i, deps in remaining.items() if i not in ready}
    return {'summary': raw['summary'], 'work_items': normalized}
