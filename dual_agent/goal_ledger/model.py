"""Typed ledger snapshots. These records grant no execution authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class LedgerError(ValueError):
    """Base class for diagnosable service errors."""


class ValidationError(LedgerError):
    pass


class ConflictError(LedgerError):
    pass


class NotFoundError(LedgerError):
    pass


class TransitionError(LedgerError):
    pass


class EvidenceError(LedgerError):
    pass


class StorageError(LedgerError):
    pass


class StorageBusyError(StorageError):
    pass


class SchemaError(StorageError):
    pass


class GoalStatus(str, Enum):
    UNPLANNED = 'UNPLANNED'
    PLANNED = 'PLANNED'
    RUNNING = 'RUNNING'
    BLOCKED = 'BLOCKED'
    COMPLETED = 'COMPLETED'
    CANCELLED = 'CANCELLED'


class WorkStatus(str, Enum):
    PENDING = 'PENDING'
    READY = 'READY'
    DISPATCHED = 'DISPATCHED'
    RUNNING = 'RUNNING'
    AWAITING_CONFIRMATION = 'AWAITING_CONFIRMATION'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    BLOCKED = 'BLOCKED'
    CANCELLED = 'CANCELLED'
    SUPERSEDED = 'SUPERSEDED'


class AttemptStatus(str, Enum):
    DISPATCH_INTENT = 'DISPATCH_INTENT'
    RUNNING = 'RUNNING'
    RESULT_PENDING = 'RESULT_PENDING'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    INTERRUPTED = 'INTERRUPTED'
    NEEDS_RECONCILIATION = 'NEEDS_RECONCILIATION'


class VerificationState(str, Enum):
    RECORDED = 'RECORDED'
    VALID = 'VALID'
    INVALIDATED = 'INVALIDATED'


class TrustLevel(str, Enum):
    UNTRUSTED = 'UNTRUSTED'
    COORDINATOR = 'COORDINATOR'
    OFFLINE_FIXTURE = 'OFFLINE_FIXTURE'


class DecisionAction(str, Enum):
    ACCEPT = 'ACCEPT'
    REQUEST_REPAIR = 'REQUEST_REPAIR'
    REPLAN = 'REPLAN'
    BLOCK = 'BLOCK'
    ESCALATE = 'ESCALATE'


class RecoveryKind(str, Enum):
    SAFE_TO_RESUME = 'SAFE_TO_RESUME'
    NEEDS_RECONCILIATION = 'NEEDS_RECONCILIATION'
    UNKNOWN_SIDE_EFFECT = 'UNKNOWN_SIDE_EFFECT'
    BLOCKED = 'BLOCKED'
    TERMINAL = 'TERMINAL'


class Snapshot:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Goal(Snapshot):
    goal_id: str
    title: str
    original_goal: str
    constraints: tuple[str, ...]
    status: GoalStatus
    revision: int
    active_plan_revision: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlanRevision(Snapshot):
    plan_id: str
    goal_id: str
    revision: int
    parent_revision: int | None
    plan_content: dict[str, Any]
    content_hash: str
    created_at: str
    source: str


@dataclass(frozen=True)
class WorkItem(Snapshot):
    work_item_id: str
    goal_id: str
    plan_revision: int
    title: str
    description: str
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    allowed_write_scope: tuple[str, ...]
    status: WorkStatus
    generation: int
    current_attempt_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Attempt(Snapshot):
    attempt_id: str
    work_item_id: str
    attempt_number: int
    executor: str
    run_id: str
    workspace_ref: str
    worktree_ref: str
    base_head: str
    status: AttemptStatus
    generation: int
    started_at: str | None
    finished_at: str | None
    failure_reason: str
    created_at: str


@dataclass(frozen=True)
class EvidenceRef(Snapshot):
    evidence_id: str
    attempt_id: str
    run_id: str
    evidence_path: str
    sha256: str
    head_or_snapshot_ref: str
    verification_state: VerificationState
    trust_level: TrustLevel
    recorded_at: str
    invalidation_reason: str


@dataclass(frozen=True)
class ManagerDecision(Snapshot):
    decision_id: str
    goal_id: str
    work_item_id: str | None
    attempt_id: str | None
    plan_revision: int
    action: DecisionAction
    reason: str
    evidence_ids: tuple[str, ...]
    created_at: str
    goal_revision: int


@dataclass(frozen=True)
class DomainEvent(Snapshot):
    event_id: str
    goal_id: str
    goal_revision: int
    kind: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class RecoveryCheck(Snapshot):
    work_item_id: str
    attempt_id: str | None
    kind: RecoveryKind
    reason: str


@dataclass(frozen=True)
class EvidenceAssessment:
    """Returned by a backend-owned trusted source, never parsed from a DTO."""
    attempt_id: str
    run_id: str
    sha256: str
    head_or_snapshot_ref: str
    verification_state: VerificationState
    trust_level: TrustLevel


def restore(cls, data: dict[str, Any]):
    """Decode our own persisted snapshot, not an untrusted proposal."""
    data = dict(data)
    enums = {Goal: {'status': GoalStatus}, WorkItem: {'status': WorkStatus},
             Attempt: {'status': AttemptStatus}, EvidenceRef: {'verification_state': VerificationState, 'trust_level': TrustLevel},
             ManagerDecision: {'action': DecisionAction}}
    for key, enum in enums.get(cls, {}).items():
        data[key] = enum(data[key])
    for key in {'constraints', 'dependencies', 'acceptance_criteria', 'allowed_write_scope', 'evidence_ids'} & data.keys():
        data[key] = tuple(data[key])
    return cls(**data)
