"""Process-local attestations observed from the original locked Coordinator.

No signing keys, saved-JSON trust switch or durable trust restoration. On host
restart only traceability survives; unattested outcomes need reconciliation.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading

from dual_agent.coordinator import _snapshot_workspace, default_lock_dir
from dual_agent.execution_gate import require_authority
from dual_agent.goal_ledger import EvidenceAssessment, EvidenceError, TrustLevel as T, VerificationState as V, AttemptStatus as A
from dual_agent.goal_ledger.evidence import file_hash
from dual_agent.goal_ledger.schemas import canonical, digest
from dual_agent.locks import WriteLockManager, ResourceLockManager, default_resource_lock_dir
from dual_agent.safety import ensure_workspace, same_path, assert_no_escaping_links
from dual_agent.worktree import _git
from .identity import ids, load


def serialized_hash(value) -> str:
    # Exact bytes written by the existing models.dump_json.
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf8')
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Observation:
    mapping: dict
    config: object
    snapshot: dict
    head: str
    verification_hash: str
    verification_digest: str
    workspace: str
    resources: tuple[str, ...]
    host_request_digest: str
    reservation_digest: str


class CoordinatorEvidenceSource:
    def __init__(self, runs: Path):
        self.runs = Path(runs)
        self._observations = {}
        self._receipts = {}
        self._prerequisites = {}
        self._mutex = threading.RLock()
        self._local = threading.local()

    def observe(self, mapping, config, state, run_dir, write_lock, resource_lock):
        aid = mapping['attempt_id']
        with self._mutex:
            self._receipts.pop(aid, None)
            self._observations.pop(aid, None)
        if state.stop_reason != 'verified' or not state.processes_cleared:
            return
        if write_lock is None or write_lock.released or not write_lock.path.is_file():
            raise EvidenceError('Coordinator observation requires its live write lock')
        if config.resource_locks and (resource_lock is None or resource_lock.released):
            raise EvidenceError('Coordinator resource lock missing at observation')
        if not same_path(run_dir, self.runs / mapping['run_id']) or config.task_id != mapping['task_id'] or config.schedule_id != mapping['schedule_id']:
            raise EvidenceError('Coordinator observation identity mismatch')
        workspace = ensure_workspace(config.workspace, config.allowed_roots)
        if not same_path(workspace, Path(mapping['workspace_ref'])):
            raise EvidenceError('Coordinator ran in an unexpected workspace')
        require_authority(config, prepared=bool(state.worktree_path))
        records = [v for v in state.verification if not v.get('superseded')]
        commands = [v for v in records if v.get('type') == 'command']
        if not commands or not all(v.get('ok') is True and not v.get('evidence_invalidated') for v in records):
            raise EvidenceError('Coordinator has no current successful verification set')
        if not all(v.get('exit_code') == v.get('expect_exit_code', 0) for v in commands):
            raise EvidenceError('Trusted command exit mismatch')
        if not any(m.role == 'developer' and m.invoked and m.exit_code == 0 and m.error_kind == 'ok' for m in state.messages):
            raise EvidenceError('No successful observed developer execution')
        acceptors = [m for m in state.messages if m.role == 'acceptor']
        if not acceptors or acceptors[-1].decision != 'accept':
            raise EvidenceError('Original Coordinator acceptor did not accept')
        assert_no_escaping_links(workspace, workspace)
        snapshot = _snapshot_workspace(workspace)
        if not snapshot or snapshot != state.evidence_snapshot:
            raise EvidenceError('Workspace differs from the Coordinator verification snapshot')
        code, head, error = _git(workspace, ['rev-parse', 'HEAD'], timeout=5)
        if code != 0: raise EvidenceError('Cannot inspect observed HEAD: ' + error)
        if head.strip() != mapping['base_head']:
            raise EvidenceError('Observed HEAD differs from the reserved base commit')
        key = ids(mapping['request_id'])[0]
        request = load(self.runs/'studio-host/requests'/(key+'.json'))
        reservation = load(self.runs/'studio-host/goal-reservations'/(key+'.json'))
        observation = Observation(dict(mapping), config, snapshot, head.strip(), serialized_hash(state.verification),
            digest(state.verification), str(workspace), tuple(config.resource_locks), digest(request), digest(reservation))
        with self._mutex:
            self._observations[aid] = observation

    def finalize(self, mapping, result):
        """Only receives a direct result object from this host's ExecutionContext."""
        aid = mapping['attempt_id']
        with self._mutex: observed = self._observations.get(aid)
        if observed is None: return False
        if (not result.success or result.stop_reason != 'verified' or result.status != 'verified'
            or not result.processes_cleared or not result.lock_released
            or result.task_id != mapping['task_id'] or not same_path(result.run_dir, self.runs / mapping['run_id'])
            or result.project_id != mapping['project_id'] or result.execution_mode != observed.config.execution_mode
            or not same_path(Path(result.workspace), Path(observed.workspace))
            or digest(result.verification) != observed.verification_digest):
            with self._mutex: self._receipts.pop(aid, None)
            return False
        receipt = (observed, serialized_hash(result.to_dict()))
        with self._mutex: self._receipts[aid] = receipt
        return True

    def forget(self, attempt_id):
        with self._mutex:
            self._receipts.pop(attempt_id, None)
            self._observations.pop(attempt_id, None)

    def require_dependencies(self, attempt_id, dependencies):
        with self._mutex:
            self._prerequisites[attempt_id] = tuple(dependencies)

    @contextmanager
    def guard(self, goal_id=None):
        if getattr(self._local, 'lease', None) is not None:
            yield
            return
        with self._mutex:
            receipts = {aid:r for aid,r in self._receipts.items() if goal_id is None or r[0].mapping['goal_id'] == goal_id}
        lease, handles = {}, []
        self._local.lease = lease
        try:
            if receipts:
                # One registry acquisition covers overlapping receipt workspaces.
                paths = sorted({r[0].workspace for r in receipts.values()}, key=str.casefold)
                lock, _ = WriteLockManager(default_lock_dir()).try_acquire('goal-evidence-lease', [Path(p) for p in paths])
                if lock is not None:
                    handles.append(lock)
                    resources = sorted({v for r in receipts.values() for v in r[0].resources}, key=str.casefold)
                    resource_lock = None
                    if resources:
                        resource_lock, _ = ResourceLockManager(default_resource_lock_dir()).try_acquire_all('goal-evidence-lease', resources)
                        if resource_lock is not None: handles.append(resource_lock)
                    if not resources or resource_lock is not None:
                        for aid, (observed, result_hash) in receipts.items():
                            try:
                                workspace = ensure_workspace(observed.workspace, [observed.workspace])
                                require_authority(observed.config, prepared=bool(observed.config.worktree.path))
                                code, head, _ = _git(workspace, ['rev-parse', 'HEAD'], timeout=5)
                                if code != 0 or head.strip() != observed.head: continue
                                before = _snapshot_workspace(workspace)
                                if before != observed.snapshot: continue
                                mapping = observed.mapping
                                key = ids(mapping['request_id'])[0]
                                request = load(self.runs/'studio-host/requests'/(key+'.json'))
                                reservation = load(self.runs/'studio-host/goal-reservations'/(key+'.json'))
                                if digest(request) != observed.host_request_digest or digest(reservation) != observed.reservation_digest: continue
                                run_id = mapping['run_id']
                                hashes = {run_id+'/'+name:file_hash(self.runs, run_id, run_id+'/'+name) for name in ('result.json','verification.json')}
                                if hashes[run_id+'/result.json'] != result_hash or hashes[run_id+'/verification.json'] != observed.verification_hash: continue
                                if (self.runs/'schedule'/(mapping['schedule_id']+'.cancel')).exists(): continue
                                # Detect edits during inspection, then hold the original
                                # cooperative locks through the caller's database commit.
                                if _snapshot_workspace(workspace) != before: continue
                                lease[aid] = (observed, hashes)
                            except (ValueError, OSError):
                                continue
            # A derived result cannot outlive the source used to construct it.
            for _ in range(len(lease)+1):
                invalid=[aid for aid in lease if any(parent not in lease for parent in self._prerequisites.get(aid,()))]
                if not invalid: break
                for aid in invalid: lease.pop(aid,None)
            yield
        finally:
            self._local.lease = None
            for handle in reversed(handles): handle.release()

    def artifact_hash(self, attempt, relative):
        item = (getattr(self._local, 'lease', None) or {}).get(attempt.attempt_id)
        if item is None or relative not in item[1]:
            raise EvidenceError('No fresh observed artifact lease; historical JSON is not an authority')
        return item[1][relative]

    def assess(self, attempt, relative, sha256):
        item = (getattr(self._local, 'lease', None) or {}).get(attempt.attempt_id)
        if item is None: return None
        observed, hashes = item
        m = observed.mapping
        with self._mutex:
            current = self._receipts.get(attempt.attempt_id)
            if current is None or current[0] is not observed: return None
        if (self.runs/'schedule'/(m['schedule_id']+'.cancel')).exists(): return None
        if (attempt.attempt_id,attempt.work_item_id,attempt.generation,attempt.run_id,attempt.workspace_ref,attempt.worktree_ref,attempt.base_head) != (
            m['attempt_id'],m['work_item_id'],m['generation'],m['run_id'],m['workspace_ref'],m['worktree_ref'],m['base_head']):
            raise EvidenceError('Evidence observation belongs to another attempt/generation')
        if hashes.get(relative) != sha256: return None
        if attempt.status not in {A.RESULT_PENDING,A.SUCCEEDED}: return None
        snapshot_ref = observed.head + ':' + digest(observed.snapshot)
        return EvidenceAssessment(attempt.attempt_id, attempt.run_id, sha256, snapshot_ref, V.VALID, T.COORDINATOR)
