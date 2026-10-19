"""Reservation protocol shared by the trusted host and GoalHost, not GUI DTOs."""
from __future__ import annotations
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import os

from dual_agent.coordinator import allocate_run_dir
from dual_agent.models import VerificationCheck
from dual_agent.safety import finalize_declared, same_path, is_reparse_point, is_inside
from dual_agent.worktree import planned_worktree_path, _git
from dual_agent.goal_ledger.schemas import canonical, digest, relative_path, string_list
from dual_agent.goal_ledger.model import ConflictError, ValidationError
from dual_agent.goal_ledger.db import exact_local_path


def ids(request_id: str) -> tuple[str, str, str]:
    key = hashlib.sha256(request_id.encode()).hexdigest()
    return key, 'sch-studio-' + key[:24], 'studio-' + key[:24]


def request_hash(value: dict) -> str:
    # Exactly the existing TaskHost.submit digest (spaces intentionally retained).
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save(path: Path, value: dict) -> None:
    exact_local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.pending')
    exact_local_path(temporary)
    with temporary.open('w', encoding='utf8') as stream:
        stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def load(path: Path) -> dict:
    exact_local_path(path)
    if path.stat().st_size > 128 * 1024: raise ConflictError('Host reservation is oversized')
    data = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(data, dict): raise ConflictError('Invalid host reservation')
    return data


def reservation_path(host, request_id):
    return host.policy.runs / 'studio-host/goal-reservations' / (ids(request_id)[0] + '.json')


def constrain(config, scope):
    scope = [relative_path(p) for p in string_list(scope, nonempty=True)]
    # Keep every existing preset and add a conjunctive restriction. Signed CLI
    # write_paths must remain the authorized worktree root; this is an extra
    # trusted acceptance gate, not a grant or OS confinement claim.
    checks = [*config.verification, VerificationCheck(type='allowed_changes', allowed_paths=scope)]
    if not config.has_developer(): raise ValidationError('Goal work must include a developer')
    prepared = replace(config, verification=checks)
    if config.worktree.enabled:
        prepared = replace(prepared, worktree=replace(config.worktree, path=str(planned_worktree_path(config))))
    return prepared


def reserve(host, raw: str, scope: list[str]) -> dict:
    # Imported through the actual desktop policy, not a duplicate task builder.
    from desktop_validation.task_api import task_request, BusinessError
    value = task_request(raw, submit=True)
    request_id = value.pop('requestId')
    key, schedule_id, task_id = ids(request_id)
    with host.guard:
        file = reservation_path(host, request_id)
        if file.exists():
            saved = load(file)
            if saved['request_hash'] != request_hash(value) or saved['scope'] != scope:
                raise ConflictError('Reserved request content or scope changed')
            return saved
        if (host.ledger / (key + '.json')).exists():
            raise ConflictError('Request ID already belongs to an unreserved task')
        report = host.policy.preflight(json.dumps(value, ensure_ascii=False))
        if not report['ready']: raise BusinessError('PREFLIGHT_BLOCKED', 'Goal task preflight denied: ' + str(report['items']))
        config = host.policy.build(value, task_id)
        config = constrain(replace(config, schedule_id=schedule_id), scope)
        workspace = Path(config.worktree.path) if config.worktree.enabled else Path(config.workspace)
        # No state or trusted programs may be placed in an Agent writable root.
        state = host.policy.runs / 'studio-host'
        if is_inside(state, workspace) or is_inside(workspace, state):
            raise ValidationError('Execution workspace overlaps host state')
        ref = config.worktree.base_ref if config.worktree.enabled else 'HEAD'
        code, head, _ = _git(Path(config.workspace), ['rev-parse', '--verify', ref+'^{commit}'], timeout=5)
        if code != 0: raise ValidationError('Goal execution requires an inspectable Git fixture/project')
        if config.worktree.enabled:
            # Resolve a moving branch/HEAD before intent persistence. Narrowing
            # it to that exact commit keeps a queued attempt's base immutable.
            config = replace(config, worktree=replace(config.worktree, base_ref=head.strip()))
        folder = allocate_run_dir(host.policy.runs, task_id)
        exact_local_path(folder)
        result = dict(request_id=request_id, request_hash=request_hash(value), project_id=value['projectId'],
            schedule_id=schedule_id, task_id=task_id, run_id=folder.relative_to(host.policy.runs).as_posix(),
            workspace_ref=str(workspace), worktree_ref=str(workspace), base_head=head.strip(),
            config_hash=digest(config.to_dict()), scope=list(scope), payload=value)
        save(file, result)
        return result


def prepare_submission(host, value, config, reservation, context):
    from desktop_validation.task_api import BusinessError
    from dual_agent.execution_context import ExecutionContext
    request_id = reservation['request_id']
    saved = load(reservation_path(host, request_id))
    if saved != reservation or saved['request_hash'] != request_hash(value):
        raise ConflictError('Host reservation changed or request mismatch')
    key, schedule_id, task_id = ids(request_id)
    if not isinstance(context, ExecutionContext) or (context.run_id, context.task_id, context.schedule_id) != (saved['run_id'], task_id, schedule_id):
        raise BusinessError('RESERVATION_DENIED', 'Missing matching backend execution context')
    config = constrain(replace(config, schedule_id=schedule_id), saved['scope'])
    if config.worktree.enabled:
        config = replace(config, worktree=replace(config.worktree, base_ref=saved['base_head']))
    if digest(config.to_dict()) != saved['config_hash']:
        raise ConflictError('Configuration changed since reservation; do not submit')
    return config
