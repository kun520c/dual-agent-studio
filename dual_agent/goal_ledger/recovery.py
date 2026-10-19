"""Read-only, deterministic recovery inspection. Never launches or replays work."""
from .model import (RecoveryCheck, RecoveryKind as R, WorkStatus as W, AttemptStatus as A,
                    GoalStatus as G)


def inspect_goal(service, c, goal) -> list[RecoveryCheck]:
    checks = []
    events = c.execute('SELECT COUNT(*),MIN(goal_revision),MAX(goal_revision) FROM events WHERE goal_id=?', (goal.goal_id,)).fetchone()
    if tuple(events) != (goal.revision + 1, 0, goal.revision):
        checks.append(RecoveryCheck('', None, R.BLOCKED, 'Event/projection revision mismatch; manual inspection required'))
    rows = c.execute('SELECT work_item_id FROM work_items WHERE goal_id=? ORDER BY plan_revision,ordinal', (goal.goal_id,)).fetchall()
    for row in rows:
        work = service._work(c, row[0])
        aid = work.current_attempt_id
        kind, reason = R.SAFE_TO_RESUME, 'Saved work has no dispatch intent; scheduling still requires policy and locks'
        if aid:
            attempt = service._attempt(c, aid)
            compatible = {
                W.DISPATCHED: {A.DISPATCH_INTENT}, W.RUNNING: {A.RUNNING},
                W.AWAITING_CONFIRMATION: {A.RESULT_PENDING}, W.COMPLETED: {A.SUCCEEDED},
                W.FAILED: {A.FAILED}, W.BLOCKED: {A.NEEDS_RECONCILIATION, A.INTERRUPTED},
                W.CANCELLED: {A.CANCELLED, A.NEEDS_RECONCILIATION, A.FAILED, A.INTERRUPTED},
                W.SUPERSEDED: {A.FAILED, A.CANCELLED, A.INTERRUPTED},
            }
            if attempt.work_item_id != work.work_item_id or attempt.generation != work.generation:
                kind, reason = R.BLOCKED, 'Current attempt/generation does not match work projection'
            elif attempt.status not in compatible.get(work.status, set()):
                kind, reason = R.BLOCKED, 'Work and attempt states disagree; do not infer process state'
            elif attempt.status == A.DISPATCH_INTENT:
                kind, reason = R.UNKNOWN_SIDE_EFFECT, 'Dispatch intent committed; external startup is unknown; do not replay'
            elif attempt.status in {A.RUNNING, A.NEEDS_RECONCILIATION}:
                kind, reason = R.NEEDS_RECONCILIATION, 'Execution may have changed the workspace; no verified process/lock state'
            elif attempt.status == A.RESULT_PENDING:
                kind, reason = R.NEEDS_RECONCILIATION, 'Result recorded but not accepted; revalidate evidence and workspace before confirming'
            elif attempt.status == A.INTERRUPTED:
                kind, reason = R.UNKNOWN_SIDE_EFFECT, 'Execution interrupted; explicit reconciliation is required before retry'
            elif work.status == W.COMPLETED and not service._completed(c, work):
                kind, reason = R.BLOCKED, 'Historical completion has no currently usable trusted evidence'
            else:
                kind, reason = R.TERMINAL, 'Attempt finished; no automatic retry'
        elif work.status in {W.DISPATCHED, W.RUNNING, W.AWAITING_CONFIRMATION, W.COMPLETED}:
            kind, reason = R.BLOCKED, 'Work state requires a current attempt, but none is recorded'
        elif work.status in {W.CANCELLED, W.SUPERSEDED, W.FAILED}:
            kind, reason = R.TERMINAL, 'Work is terminal; no replay'
        if work.status == W.BLOCKED and kind == R.SAFE_TO_RESUME:
            kind, reason = R.BLOCKED, 'Explicitly blocked work requires a reconciliation decision'
        if goal.status == G.BLOCKED and kind == R.SAFE_TO_RESUME:
            kind, reason = R.BLOCKED, 'Goal is blocked'
        if kind == R.SAFE_TO_RESUME and work.status == W.READY and not service._deps_met(c, work):
            kind, reason = R.BLOCKED, 'Saved readiness is stale because dependency evidence is no longer usable'
        checks.append(RecoveryCheck(work.work_item_id, aid, kind, reason))
    return checks
