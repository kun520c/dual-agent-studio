"""CLI-reported usage, never an account bill. No text-length token estimates.

Codex turn.completed.input_tokens includes cached input. Cursor 2026.10.01
JSON result.inputTokens excludes cache reads/writes (7671.index.js turnEnded).
Unknown schemas stay unknown; terminal snapshots replace, never add to, deltas.
"""
from __future__ import annotations

import hashlib
import json

FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens',
          'cache_write_input_tokens', 'reasoning_output_tokens')


def number(value):
    return value if type(value) is int and value >= 0 else None


def unknown():
    return dict(schema_version=1, **{key: None for key in FIELDS},
                uncached_input_tokens=None, internal_model_requests=None,
                actual_cost=None, actual_cost_status='UNKNOWN', model=None,
                input_semantics='UNKNOWN', source='UNKNOWN', observations=[], warnings=[])


def parse_usage(stdout: str, executor: str) -> dict:
    result = unknown()
    snapshots = {}; deltas = {}; seen = set(); terminal = None
    for line_no, line in enumerate(stdout.splitlines(), 1):
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get('usage'), dict):
            continue
        usage = data['usage']; kind = data.get('type'); row = None
        if executor == 'codex' and kind == 'turn.completed':
            row = {k: number(usage.get(k)) for k in FIELDS}
            row['uncached_input_tokens'] = (row['input_tokens'] - row['cached_input_tokens']
                if row['input_tokens'] is not None and row['cached_input_tokens'] is not None
                and row['cached_input_tokens'] <= row['input_tokens'] else None)
            semantics = 'includes_cache'
        elif executor == 'cursor' and kind == 'result':
            incoming = number(usage.get('inputTokens'))
            cached = number(usage.get('cacheReadTokens'))
            written = number(usage.get('cacheWriteTokens'))
            row = dict(input_tokens=sum((incoming, cached, written)) if all(v is not None for v in (incoming, cached, written)) else None,
                       output_tokens=number(usage.get('outputTokens')), cached_input_tokens=cached,
                       cache_write_input_tokens=written, reasoning_output_tokens=None,
                       uncached_input_tokens=incoming)
            semantics = 'cursor_json_excludes_cache'
        # Explicit counter modes are accepted for offline adapters; unlabelled
        # usage events are deliberately ignored instead of guessed.
        elif data.get('usage_mode') in {'delta', 'cumulative', 'total'}:
            row = {k: number(usage.get(k)) for k in FIELDS}
            row['uncached_input_tokens'] = None; semantics = 'UNKNOWN'
        if row is None:
            continue
        mode = data.get('usage_mode') or ('total' if executor == 'cursor' else 'cumulative')
        identity = data.get('event_id')
        if not isinstance(identity,(str,int)):identity=None
        if identity is not None and identity in seen:
            continue
        if identity is not None: seen.add(identity)
        source = f'stdout.txt:L{line_no}'
        result['observations'].append(dict(source=source, event=kind, mode=mode))
        result['source'] = source; result['input_semantics'] = semantics
        model = data.get('model')
        if isinstance(model, str): result['model'] = model
        if mode == 'total': terminal = row
        elif mode == 'delta':
            if identity is None:
                result['warnings'].append('Delta without event_id ignored'); continue
            deltas[identity] = row
        else:
            # Per-turn terminal totals in Codex exec are distinct turns; a
            # repeated identified turn replaces its previous snapshot.
            key = data.get('turn_id', '__session__')
            if not isinstance(key,(str,int)):key='__session__'
            snapshots[key] = row
    rows = [terminal] if terminal is not None else list(snapshots.values()) or list(deltas.values())
    if rows:
        for key in (*FIELDS, 'uncached_input_tokens'):
            values = [row.get(key) for row in rows]
            result[key] = sum(values) if all(v is not None for v in values) else None
    result['stdout_sha256'] = hashlib.sha256(stdout.encode('utf8')).hexdigest()
    return result


def summarize_usage(rounds: list[dict], budget=None) -> dict:
    totals = {}
    for key in (*FIELDS, 'uncached_input_tokens'):
        values = [r.get(key) for r in rounds if r.get('invoked') is True]
        known = [v for v in values if type(v) is int]
        totals[key] = dict(known=sum(known) if known else None,
                           complete=bool(values) and len(known) == len(values))
    return dict(schema_version=1, rounds=rounds, totals=totals,
                cli_launches=sum(r.get('invoked') is True for r in rounds) if rounds and all(type(r.get('invoked')) is bool for r in rounds) else None,
                internal_model_requests=None, actual_cost=None,
                actual_cost_status='UNKNOWN', cli_launch_limit=budget,
                budget_kind='CLI_LAUNCHES_ONLY', source='rounds/*/stdout.txt + meta.json')
