"""GoalService: the sole domain write boundary for Phase A.

No executor imports, commands, GUI SQL, execution grants or history migration.
Every fresh write checks a goal revision and atomically commits its event,
projection and idempotent response. A key always replays its original response.
"""
from __future__ import annotations
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
import uuid
from typing import Callable, Any

from .db import Database, now
from .evidence import EvidenceSource, file_hash
from .model import (Goal, PlanRevision, WorkItem, Attempt, EvidenceRef, ManagerDecision, DomainEvent,
    GoalStatus as G, WorkStatus as W, AttemptStatus as A, VerificationState as V, TrustLevel as T,
    DecisionAction, ConflictError, NotFoundError, ValidationError, TransitionError, EvidenceError,
    EvidenceAssessment, RecoveryCheck, restore)
from .schemas import canonical, digest, identifier, integer, text, string_list, relative_path, plan_proposal, object_fields
from .state_machine import require_transition, ACTIVE_ATTEMPTS, TERMINAL_GOALS
from .manager_state import ManagerStateAPI
from .manager_calls import ManagerCallAPI


def new_id(prefix: str) -> str:
    return prefix + '-' + uuid.uuid4().hex


class GoalService(ManagerStateAPI, ManagerCallAPI):
    def __init__(self, runs_root: Path, *, evidence_source: EvidenceSource | None = None, busy_timeout_ms: int = 3000):
        self._db = Database(runs_root, busy_timeout_ms=busy_timeout_ms)
        self._source = evidence_source
        self._closed = False

    @classmethod
    def for_task_policy(cls, policy, **kwargs) -> GoalService:
        """Trusted Python adapter. Does not change TaskHost requestId or dispatch."""
        return cls(policy.runs, **kwargs)

    @property
    def database_path(self) -> Path:
        return self._db.path

    def close(self) -> None:
        # Each operation closes its own connection, including exceptional paths.
        self._closed = True

    def __enter__(self) -> GoalService:
        self._open()
        return self

    def __exit__(self, *_):
        self.close()

    def _open(self):
        if self._closed:
            raise TransitionError('GoalService is closed')

    def _row(self, c, table: str, key: str, value: str):
        row = c.execute(f'SELECT * FROM {table} WHERE {key}=?', (value,)).fetchone()
        if row is None:
            raise NotFoundError(f'{table}: {value}')
        return row

    def _goal(self, c, gid: str) -> Goal:
        r = dict(self._row(c, 'goals', 'goal_id', gid))
        r['constraints'] = json.loads(r.pop('constraints_json'))
        return restore(Goal, r)

    def _plan(self, c, gid: str, revision: int) -> PlanRevision:
        r = c.execute('SELECT data FROM plans WHERE goal_id=? AND revision=?', (gid, revision)).fetchone()
        if r is None:
            raise NotFoundError('Unknown plan revision')
        return restore(PlanRevision, json.loads(r[0]))

    def _work(self, c, wid: str) -> WorkItem:
        r = dict(self._row(c, 'work_items', 'work_item_id', wid))
        body = json.loads(r.pop('data'))
        r.pop('ordinal')
        return restore(WorkItem, {**body, **r})

    def _attempt(self, c, aid: str) -> Attempt:
        r = dict(self._row(c, 'attempts', 'attempt_id', aid))
        return restore(Attempt, {**json.loads(r.pop('data')), **r})

    def _evidence(self, c, eid: str) -> EvidenceRef:
        r = dict(self._row(c, 'evidence', 'evidence_id', eid))
        return restore(EvidenceRef, {**json.loads(r.pop('data')), **r})

    def _decision(self, c, did: str) -> ManagerDecision:
        return restore(ManagerDecision, json.loads(self._row(c, 'decisions', 'decision_id', did)['data']))

    def _resolve(self, c, ref: dict):
        kind = ref['entity']
        if kind == 'goal': return self._goal(c, ref['goal_id'])
        if kind == 'plan': return self._plan(c, ref['goal_id'], ref['plan_revision'])
        if kind == 'work': return self._work(c, ref['id'])
        if kind == 'attempt': return self._attempt(c, ref['id'])
        if kind == 'evidence': return self._evidence(c, ref['id'])
        if kind == 'decision': return self._decision(c, ref['id'])
        raise AssertionError(kind)

    def _event(self, c, goal: Goal, operation: str, ref: dict, request_hash: str, details: dict):
        c.execute('INSERT INTO events(event_id,goal_id,goal_revision,kind,payload,created_at) VALUES(?,?,?,?,?,?)',
            (new_id('event'), goal.goal_id, goal.revision, operation, canonical({**ref, 'request_hash': request_hash, **details}), now()))

    def _write(self, operation: str, key: str, payload: dict, action: Callable) -> dict:
        # Backend evidence sources prepare fresh snapshots and take cooperative
        # locks BEFORE BEGIN IMMEDIATE, keeping Git/large workspace reads out of
        # the SQLite write transaction. The lease lasts through its commit.
        with self._evidence_guard(payload.get('goal_id')):
            return self._write_locked(operation, key, payload, action)

    def _evidence_guard(self, goal_id=None):
        guard = getattr(self._source, 'guard', None)
        return guard(goal_id) if guard is not None else nullcontext()

    def _write_locked(self, operation: str, key: str, payload: dict, action: Callable) -> dict:
        self._open()
        text(key, 200)
        # Host fact identity excludes the delivery-time optimistic revision.
        # The actual fact (goal/attempt/generation/event/reason/id) stays pinned.
        # Ordinary Phase A requests retain their original full-content semantics.
        identity_payload = {k:v for k,v in payload.items() if k != 'expected_revision'} if operation.startswith('attempt.') and 'fact_id' in payload else payload
        hashed = digest({'operation': operation, 'payload': identity_payload})
        with self._db.transaction() as c:
            saved = c.execute('SELECT * FROM requests WHERE idempotency_key=?', (key,)).fetchone()
            if saved:
                if saved['request_hash'] != hashed:
                    raise ConflictError('Idempotency key reused with different content')
                return json.loads(saved['response'])
            gid = payload.get('goal_id')
            self._manager_effect_check(c,operation,gid)
            if gid:
                goal = self._goal(c, identifier(gid))
                if goal.revision != integer(payload['expected_revision']):
                    raise ConflictError(f'Expected goal revision {payload["expected_revision"]}, current {goal.revision}')
            ref = action(c)
            gid = ref['goal_id']
            if operation != 'goal.created':
                c.execute('UPDATE goals SET revision=revision+1,updated_at=? WHERE goal_id=?', (now(), gid))
            details = {k: payload[k] for k in ('reason', 'reconciliation_note', 'event', 'generation', 'blocked') if k in payload}
            self._event(c, self._goal(c, gid), operation, ref, hashed, details)
            response = self._resolve(c, ref).to_dict()
            c.execute('INSERT INTO requests VALUES(?,?,?)', (key, hashed, canonical(response)))
            return response

    def _editable(self, goal: Goal) -> None:
        if goal.status in TERMINAL_GOALS:
            raise TransitionError('Goal is terminal')

    def _set_goal(self, c, goal: Goal, status: G):
        if goal.status != status:
            require_transition(goal.status, status)
            c.execute('UPDATE goals SET status=? WHERE goal_id=?', (status.value, goal.goal_id))

    def _set_work(self, c, work: WorkItem, status: W):
        require_transition(work.status, status)
        c.execute('UPDATE work_items SET status=?,updated_at=? WHERE work_item_id=?', (status.value, now(), work.work_item_id))

    def _set_attempt(self, c, attempt: Attempt, status: A, reason: str = ''):
        require_transition(attempt.status, status)
        started = now() if status == A.RUNNING else attempt.started_at
        finished = now() if status not in ACTIVE_ATTEMPTS else attempt.finished_at
        c.execute('UPDATE attempts SET status=?,started_at=?,finished_at=?,failure_reason=? WHERE attempt_id=?',
            (status.value, started, finished, reason, attempt.attempt_id))

    def _items(self, c, goal: Goal) -> list[WorkItem]:
        return [self._work(c, r[0]) for r in c.execute('SELECT work_item_id FROM work_items WHERE goal_id=? AND plan_revision=? ORDER BY ordinal',
            (goal.goal_id, goal.active_plan_revision))]

    def _active_work(self, c, gid: str, wid: str) -> tuple[Goal, WorkItem]:
        goal, work = self._goal(c, gid), self._work(c, wid)
        self._editable(goal)
        if work.goal_id != gid or work.plan_revision != goal.active_plan_revision:
            raise ConflictError('Work item does not belong to the active plan')
        return goal, work

    def _current(self, c, gid: str, aid: str, generation: int) -> tuple[Goal, WorkItem, Attempt]:
        a = self._attempt(c, aid)
        goal, work = self._active_work(c, gid, a.work_item_id)
        if integer(generation, 1) != a.generation or work.generation != a.generation or work.current_attempt_id != aid:
            raise ConflictError('Stale attempt or fencing generation')
        if work.status in {W.COMPLETED, W.CANCELLED, W.SUPERSEDED, W.BLOCKED}:
            raise TransitionError('Work item cannot receive executor events')
        if goal.status == G.BLOCKED:
            raise TransitionError('Goal is blocked')
        return goal, work, a

    def create_goal(self, *, title: str, original_goal: str, constraints=(), idempotency_key: str) -> Goal:
        payload = dict(title=text(title, 500), original_goal=text(original_goal), constraints=string_list(constraints))
        def action(c):
            gid, stamp = new_id('goal'), now()
            c.execute('INSERT INTO goals VALUES(?,?,?,?,?,?,?,?,?)',
                (gid, payload['title'], payload['original_goal'], canonical(payload['constraints']), G.UNPLANNED.value, 0, None, stamp, stamp))
            return dict(entity='goal', goal_id=gid)
        return restore(Goal, self._write('goal.created', idempotency_key, payload, action))

    def get_goal(self, goal_id: str) -> Goal:
        self._open()
        with self._db.read() as c: return self._goal(c, identifier(goal_id))

    def create_plan(self, goal_id: str, proposal: dict, *, parent_revision: int | None, expected_revision: int,
                    idempotency_key: str, source: str = 'structured_proposal') -> PlanRevision:
        proposal = plan_proposal(proposal)
        if parent_revision is not None: integer(parent_revision, 1)
        payload = dict(goal_id=goal_id, proposal=proposal, parent_revision=parent_revision,
                       expected_revision=expected_revision, source=text(source, 100))
        def action(c):
            self._editable(self._goal(c, goal_id))
            latest = c.execute('SELECT MAX(revision) FROM plans WHERE goal_id=?', (goal_id,)).fetchone()[0]
            if latest != parent_revision:
                raise ConflictError('Plan parent must be the latest saved revision')
            revision, stamp = (latest or 0) + 1, now()
            for spec in proposal['work_items']:
                if c.execute('SELECT 1 FROM work_items WHERE work_item_id=?', (spec['work_item_id'],)).fetchone():
                    raise ConflictError('Work item IDs cannot be reused across goals or plan revisions')
            p = PlanRevision(new_id('plan'), goal_id, revision, parent_revision, proposal, digest(proposal), stamp, source)
            c.execute('INSERT INTO plans VALUES(?,?,?,?,?)', (p.plan_id, goal_id, revision, parent_revision, canonical(p.to_dict())))
            for ordinal, spec in enumerate(proposal['work_items']):
                c.execute('INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (spec['work_item_id'], goal_id, revision, ordinal, canonical(spec), W.PENDING.value, 0, None, stamp, stamp))
            for spec in proposal['work_items']:
                for dependency in spec['dependencies']:
                    c.execute('INSERT INTO dependencies VALUES(?,?,?,?)', (goal_id, revision, spec['work_item_id'], dependency))
            return dict(entity='plan', goal_id=goal_id, plan_revision=revision)
        return restore(PlanRevision, self._write('plan.created', idempotency_key, payload, action))

    def get_plan(self, goal_id: str, revision: int) -> PlanRevision:
        self._open()
        with self._db.read() as c: return self._plan(c, identifier(goal_id), integer(revision, 1))

    def activate_plan(self, goal_id: str, plan_revision: int, *, expected_revision: int, idempotency_key: str) -> Goal:
        integer(plan_revision, 1)
        payload = dict(goal_id=goal_id, plan_revision=plan_revision, expected_revision=expected_revision)
        def action(c):
            goal = self._goal(c, goal_id)
            self._editable(goal)
            if goal.status == G.BLOCKED:
                raise TransitionError('Unblock goal explicitly before activating a plan')
            plan = self._plan(c, goal_id, plan_revision)
            if plan.content_hash != digest(plan_proposal(plan.plan_content)):
                raise ValidationError('Stored plan content hash is invalid')
            latest = c.execute('SELECT MAX(revision) FROM plans WHERE goal_id=?', (goal_id,)).fetchone()[0]
            if latest != plan_revision or goal.active_plan_revision == plan_revision:
                raise ConflictError('Only the latest inactive plan can be activated')
            for work in self._items(c, goal):
                if work.current_attempt_id and self._attempt(c, work.current_attempt_id).status in ACTIVE_ATTEMPTS:
                    raise ConflictError('Reconcile/cancel active attempts before replanning')
                if work.status not in {W.COMPLETED, W.CANCELLED, W.SUPERSEDED}:
                    self._set_work(c, work, W.SUPERSEDED)
            c.execute('UPDATE goals SET active_plan_revision=? WHERE goal_id=?', (plan_revision, goal_id))
            self._set_goal(c, goal, G.PLANNED)
            self._refresh(c, self._goal(c, goal_id))
            return dict(entity='goal', goal_id=goal_id)
        return restore(Goal, self._write('plan.activated', idempotency_key, payload, action))

    def get_work_item(self, work_item_id: str) -> WorkItem:
        self._open()
        with self._db.read() as c: return self._work(c, identifier(work_item_id))

    def list_work_items(self, goal_id: str, plan_revision: int | None = None) -> list[WorkItem]:
        self._open()
        with self._db.read() as c:
            goal = self._goal(c, identifier(goal_id))
            revision = plan_revision if plan_revision is not None else goal.active_plan_revision
            if revision is None: return []
            self._plan(c, goal_id, integer(revision, 1))
            return [self._work(c, r[0]) for r in c.execute('SELECT work_item_id FROM work_items WHERE goal_id=? AND plan_revision=? ORDER BY ordinal', (goal_id, revision))]

    def _assessment(self, attempt: Attempt, ref: EvidenceRef) -> EvidenceAssessment | None:
        if not self._source: return None
        try:
            value = self._source.assess(attempt, ref.evidence_path, ref.sha256)
        except Exception as exc:
            raise EvidenceError(f'Trusted evidence source unavailable: {exc}') from exc
        if value is None: return None
        if not isinstance(value, EvidenceAssessment) or not isinstance(value.verification_state, V) or not isinstance(value.trust_level, T):
            raise EvidenceError('Evidence source must return a typed backend assessment')
        if (value.attempt_id, value.run_id, value.sha256) != (attempt.attempt_id, attempt.run_id, ref.sha256):
            raise EvidenceError('Assessment is not bound to this attempt/run/content')
        text(value.head_or_snapshot_ref, 1000)
        return value

    def _usable(self, c, ref: EvidenceRef) -> bool:
        if ref.verification_state != V.VALID or ref.trust_level not in {T.COORDINATOR, T.OFFLINE_FIXTURE}:
            return False
        try:
            a = self._attempt(c, ref.attempt_id)
            if self._reference_hash(a, ref.evidence_path) != ref.sha256: return False
            assessment = self._assessment(a, ref)
            return assessment is not None and assessment.verification_state == V.VALID and assessment.trust_level == ref.trust_level and assessment.head_or_snapshot_ref == ref.head_or_snapshot_ref
        except (EvidenceError, ValidationError, OSError):
            return False

    def _reference_hash(self, attempt: Attempt, path: str) -> str:
        prepared = getattr(self._source, 'artifact_hash', None)
        if prepared is not None:
            return prepared(attempt, path)
        return file_hash(self._db.root, attempt.run_id, path)

    def _completed(self, c, work: WorkItem) -> bool:
        if work.status != W.COMPLETED or not work.current_attempt_id: return False
        a = self._attempt(c, work.current_attempt_id)
        return a.status == A.SUCCEEDED and a.generation == work.generation and any(
            self._usable(c, self._evidence(c, r[0])) for r in c.execute('SELECT evidence_id FROM evidence WHERE attempt_id=?', (a.attempt_id,)))

    def _deps_met(self, c, work: WorkItem) -> bool:
        # Recheck the transitive closure: a previously accepted intermediate task
        # cannot hide an ancestor whose workspace evidence has since expired.
        todo, checked = list(work.dependencies), set()
        while todo:
            wid = todo.pop()
            if wid in checked: continue
            checked.add(wid)
            dependency = self._work(c, wid)
            if not self._completed(c, dependency): return False
            todo.extend(dependency.dependencies)
        return True

    def _refresh(self, c, goal: Goal):
        for work in self._items(c, goal):
            if work.status in {W.PENDING, W.READY}:
                status = W.READY if self._deps_met(c, work) else W.PENDING
                if work.status != status: self._set_work(c, work, status)

    def ready_work_items(self, goal_id: str, *, available_capacity: int = 1, conflicts=()) -> list[WorkItem]:
        """Candidates only; caller MUST still use TaskScheduler and resource locks.

        Capacity is bounded by the existing scheduler's maximum of two. Known
        conflict IDs are exclusions, not evidence that other workspaces are safe.
        """
        self._open()
        integer(available_capacity)
        if available_capacity > 2: raise ValidationError('Scheduler capacity cannot exceed two')
        excluded = {identifier(i) for i in string_list(conflicts)}
        with self._evidence_guard(goal_id), self._db.read() as c:
            goal = self._goal(c, identifier(goal_id))
            if goal.status not in {G.PLANNED, G.RUNNING}: return []
            return [w for w in self._items(c, goal) if w.status == W.READY and w.work_item_id not in excluded and self._deps_met(c, w)][:available_capacity]

    def create_attempt(self, goal_id: str, work_item_id: str, *, executor: str, run_id: str, workspace_ref: str,
                       worktree_ref: str, base_head: str, expected_revision: int, idempotency_key: str,
                       dispatch: dict | None = None) -> Attempt:
        run_id = relative_path(run_id)
        if len(run_id.split('/')) != 2: raise ValidationError('run_id must be task-directory/run-directory')
        for part in run_id.split('/'): identifier(part)
        payload = dict(goal_id=goal_id, work_item_id=identifier(work_item_id), executor=identifier(executor), run_id=run_id,
            workspace_ref=text(workspace_ref, 1000), worktree_ref=text(worktree_ref, 1000), base_head=text(base_head, 1000), expected_revision=expected_revision)
        if dispatch is not None:
            dispatch = dict(object_fields(dispatch, {'request_id','project_id','schedule_id','task_id','request_hash','config_hash','scope','request_key'}))
            text(dispatch['request_key'], 150)
            for name in ('request_id','project_id','schedule_id','task_id'): identifier(dispatch[name])
            import hashlib
            identity = hashlib.sha256(dispatch['request_id'].encode()).hexdigest()[:24]
            if dispatch['task_id'] != 'studio-' + identity or dispatch['schedule_id'] != 'sch-studio-' + identity or run_id.split('/')[0] != dispatch['task_id']:
                raise ValidationError('Dispatch identity does not match the legacy request mapping')
            for name in ('request_hash', 'config_hash'):
                if not isinstance(dispatch[name], str) or not __import__('re').fullmatch('[0-9a-f]{64}', dispatch[name]):
                    raise ValidationError('Dispatch digest must be SHA-256')
            dispatch['scope'] = [relative_path(p) for p in string_list(dispatch['scope'], nonempty=True)]
            payload['dispatch'] = dispatch
        def action(c):
            goal, work = self._active_work(c, goal_id, work_item_id)
            if goal.status not in {G.PLANNED, G.RUNNING} or work.status != W.READY or not self._deps_met(c, work):
                raise TransitionError('Only a ready item with currently valid dependencies can be claimed')
            if c.execute('SELECT 1 FROM attempts WHERE run_id=?', (run_id,)).fetchone():
                raise ConflictError('Run is already bound to an attempt')
            number = c.execute('SELECT COALESCE(MAX(attempt_number),0)+1 FROM attempts WHERE work_item_id=?', (work_item_id,)).fetchone()[0]
            aid, generation, stamp = new_id('attempt'), work.generation + 1, now()
            a = Attempt(aid, work_item_id, number, executor, run_id, workspace_ref, worktree_ref, base_head,
                A.DISPATCH_INTENT, generation, None, None, '', stamp)
            c.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)', (aid, work_item_id, number, generation,
                a.status.value, run_id, canonical(a.to_dict()), None, None, ''))
            c.execute('UPDATE work_items SET current_attempt_id=?,generation=? WHERE work_item_id=?', (aid, generation, work_item_id))
            if dispatch is not None:
                mapping = dict(dispatch, goal_id=goal_id, plan_revision=work.plan_revision, work_item_id=work_item_id,
                    attempt_id=aid, generation=generation, run_id=run_id, workspace_ref=workspace_ref,
                    worktree_ref=worktree_ref, base_head=base_head, created_at=stamp)
                c.execute('INSERT INTO dispatches VALUES(?,?,?,?,?,?,?,?)', (aid, dispatch['request_id'], dispatch['schedule_id'],
                    dispatch['task_id'], dispatch['request_key'], canonical(mapping), 'INTENT', stamp))
            self._set_work(c, work, W.DISPATCHED)
            self._set_goal(c, goal, G.RUNNING)
            return dict(entity='attempt', id=aid, goal_id=goal_id)
        return restore(Attempt, self._write('attempt.intent_recorded', idempotency_key, payload, action))

    def get_dispatch(self, attempt_id: str) -> dict:
        self._open()
        with self._db.read() as c:
            r = self._row(c, 'dispatches', 'attempt_id', identifier(attempt_id))
            return {**json.loads(r['data']), 'stage': r['stage'], 'updated_at': r['updated_at']}

    def dispatch_for_request(self, request_key: str) -> dict | None:
        self._open()
        text(request_key,150)
        with self._db.read() as c:
            r = c.execute('SELECT * FROM dispatches WHERE request_key=?', (request_key,)).fetchone()
            return None if r is None else {**json.loads(r['data']), 'stage': r['stage'], 'updated_at': r['updated_at']}

    def list_dispatches(self, goal_id: str) -> list[dict]:
        self._open()
        with self._db.read() as c:
            self._goal(c, identifier(goal_id))
            rows = c.execute('SELECT d.* FROM dispatches d JOIN attempts a USING(attempt_id) JOIN work_items w USING(work_item_id) WHERE w.goal_id=? ORDER BY w.plan_revision,w.ordinal,a.attempt_number', (goal_id,)).fetchall()
            return [{**json.loads(r['data']), 'stage':r['stage'], 'updated_at':r['updated_at']} for r in rows]

    def record_dispatch_stage(self, goal_id: str, attempt_id: str, *, stage: str, reason: str,
                              expected_revision: int, idempotency_key: str) -> Attempt:
        if stage not in {'ACKNOWLEDGED','UNCERTAIN','FINISHED','CANCEL_REQUESTED'}:
            raise ValidationError('Unknown dispatch stage')
        payload = dict(goal_id=goal_id, attempt_id=identifier(attempt_id), stage=stage, reason=text(reason,10000), expected_revision=expected_revision)
        def action(c):
            a = self._attempt(c, attempt_id)
            if self._work(c, a.work_item_id).goal_id != goal_id: raise ConflictError('Dispatch belongs to another goal')
            row = self._row(c, 'dispatches', 'attempt_id', attempt_id)
            allowed = {'INTENT':{'ACKNOWLEDGED','UNCERTAIN','FINISHED','CANCEL_REQUESTED'},
                'ACKNOWLEDGED':{'UNCERTAIN','FINISHED','CANCEL_REQUESTED'}, 'UNCERTAIN':{'ACKNOWLEDGED','FINISHED','CANCEL_REQUESTED'},
                'CANCEL_REQUESTED':{'FINISHED','UNCERTAIN'}, 'FINISHED':set()}
            if stage not in allowed[row['stage']]: raise TransitionError('Dispatch stage is stale or terminal')
            c.execute('UPDATE dispatches SET stage=?,updated_at=? WHERE attempt_id=?', (stage, now(), attempt_id))
            return dict(entity='attempt',id=attempt_id,goal_id=goal_id)
        return restore(Attempt, self._write('dispatch.'+stage.lower(), idempotency_key, payload, action))

    def get_attempt(self, attempt_id: str) -> Attempt:
        self._open()
        with self._db.read() as c: return self._attempt(c, identifier(attempt_id))

    def list_attempts(self, work_item_id: str) -> list[Attempt]:
        self._open()
        with self._db.read() as c:
            self._work(c, identifier(work_item_id))
            return [self._attempt(c, r[0]) for r in c.execute('SELECT attempt_id FROM attempts WHERE work_item_id=? ORDER BY attempt_number', (work_item_id,))]

    def record_attempt_event(self, goal_id: str, attempt_id: str, *, generation: int, event: str, reason: str = '',
                             expected_revision: int, idempotency_key: str, fact_id: str | None = None) -> Attempt:
        if not isinstance(reason, str) or len(reason) > 10000 or '\x00' in reason:
            raise ValidationError('reason must be bounded text')
        if event not in {'STARTED', 'RESULT_PRODUCED', 'FAILED', 'CANCELLED', 'INTERRUPTED'}:
            raise ValidationError('Unknown executor event; success requires trusted acceptance')
        if event in {'FAILED', 'CANCELLED', 'INTERRUPTED'}: text(reason, 10000)
        payload = dict(goal_id=goal_id, attempt_id=identifier(attempt_id), generation=integer(generation, 1), event=event,
                       reason=reason, expected_revision=expected_revision)
        if fact_id is not None: payload['fact_id'] = text(fact_id,200)
        def action(c):
            _, work, a = self._current(c, goal_id, attempt_id, generation)
            states = {'STARTED': (A.RUNNING, W.RUNNING), 'RESULT_PRODUCED': (A.RESULT_PENDING, W.AWAITING_CONFIRMATION),
                      'FAILED': (A.FAILED, W.FAILED), 'CANCELLED': (A.CANCELLED, W.CANCELLED), 'INTERRUPTED': (A.INTERRUPTED, W.BLOCKED)}
            at, wt = states[event]
            self._set_attempt(c, a, at, reason)
            self._set_work(c, work, wt)
            return dict(entity='attempt', id=attempt_id, goal_id=goal_id)
        return restore(Attempt, self._write('attempt.' + event.lower(), idempotency_key, payload, action))

    def record_evidence(self, goal_id: str, attempt_id: str, *, generation: int, evidence_path: str,
                        expected_revision: int, idempotency_key: str) -> EvidenceRef:
        path = relative_path(evidence_path)
        payload = dict(goal_id=goal_id, attempt_id=identifier(attempt_id), generation=integer(generation, 1), evidence_path=path, expected_revision=expected_revision)
        def action(c):
            _, _, a = self._current(c, goal_id, attempt_id, generation)
            if a.status != A.RESULT_PENDING: raise TransitionError('Evidence can only be linked to a pending result')
            sha = self._reference_hash(a, path)
            ref = EvidenceRef(new_id('evidence'), attempt_id, a.run_id, path, sha, '', V.RECORDED, T.UNTRUSTED, now(), '')
            assessment = self._assessment(a, ref)
            if assessment:
                if assessment.verification_state == V.VALID and assessment.trust_level not in {T.COORDINATOR, T.OFFLINE_FIXTURE}:
                    raise EvidenceError('An untrusted source cannot assert valid evidence')
                ref = EvidenceRef(ref.evidence_id, attempt_id, a.run_id, path, sha, assessment.head_or_snapshot_ref,
                    assessment.verification_state, assessment.trust_level, ref.recorded_at, '')
            c.execute('INSERT INTO evidence VALUES(?,?,?,?,?)', (ref.evidence_id, attempt_id, canonical(ref.to_dict()), ref.verification_state.value, ''))
            return dict(entity='evidence', id=ref.evidence_id, goal_id=goal_id)
        return restore(EvidenceRef, self._write('evidence.recorded', idempotency_key, payload, action))

    def get_evidence(self, evidence_id: str) -> EvidenceRef:
        self._open()
        with self._db.read() as c: return self._evidence(c, identifier(evidence_id))

    def evidence_usable(self, evidence_id: str) -> bool:
        self._open()
        with self._evidence_guard(), self._db.read() as c: return self._usable(c, self._evidence(c, identifier(evidence_id)))

    def invalidate_evidence(self, goal_id: str, evidence_id: str, *, reason: str, expected_revision: int, idempotency_key: str) -> EvidenceRef:
        payload = dict(goal_id=goal_id, evidence_id=identifier(evidence_id), reason=text(reason, 10000), expected_revision=expected_revision)
        def action(c):
            ref = self._evidence(c, evidence_id)
            work = self._work(c, self._attempt(c, ref.attempt_id).work_item_id)
            if work.goal_id != goal_id: raise ConflictError('Evidence belongs to another goal')
            if ref.verification_state == V.INVALIDATED: raise TransitionError('Evidence is already invalidated')
            c.execute('UPDATE evidence SET verification_state=?,invalidation_reason=? WHERE evidence_id=?', (V.INVALIDATED.value, reason, evidence_id))
            self._refresh(c, self._goal(c, goal_id))
            return dict(entity='evidence', id=evidence_id, goal_id=goal_id)
        return restore(EvidenceRef, self._write('evidence.invalidated', idempotency_key, payload, action))

    def record_decision(self, goal_id: str, proposal: dict, *, expected_revision: int, idempotency_key: str) -> ManagerDecision:
        proposal = object_fields(proposal, {'plan_revision', 'action', 'reason', 'evidence_ids'}, {'work_item_id', 'attempt_id'})
        revision = integer(proposal['plan_revision'], 1)
        try: decision_action = DecisionAction(proposal['action'])
        except (ValueError, TypeError) as exc: raise ValidationError('Unknown decision action') from exc
        reason = text(proposal['reason'], 10000)
        eids = [identifier(e) for e in string_list(proposal['evidence_ids'])]
        wid, aid = proposal.get('work_item_id'), proposal.get('attempt_id')
        if wid is not None: identifier(wid)
        if aid is not None: identifier(aid)
        proposal = dict(plan_revision=revision, action=decision_action.value, reason=reason, evidence_ids=eids,
                        work_item_id=wid, attempt_id=aid)
        payload = dict(goal_id=goal_id, proposal=proposal, expected_revision=expected_revision)
        def action(c):
            goal = self._goal(c, goal_id)
            self._editable(goal)
            if revision != goal.active_plan_revision: raise ConflictError('Decision references a stale plan')
            if aid and not wid: raise ValidationError('Attempt decisions require work_item_id')
            if wid:
                _, work = self._active_work(c, goal_id, wid)
                if work.status in {W.CANCELLED, W.SUPERSEDED, W.COMPLETED, W.BLOCKED}: raise TransitionError('Decision target is not active')
                if aid and work.current_attempt_id != aid: raise ConflictError('Decision references a stale attempt')
                if aid and self._attempt(c, aid).generation != work.generation: raise ConflictError('Decision generation is stale')
            for eid in eids:
                ref = self._evidence(c, eid)
                if not aid or ref.attempt_id != aid: raise EvidenceError('Decision evidence must belong to the specified attempt')
            d = ManagerDecision(new_id('decision'), goal_id, wid, aid, revision, decision_action, reason, tuple(eids), now(), goal.revision)
            c.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?)', (d.decision_id, goal_id, revision, wid, aid, canonical(d.to_dict())))
            return dict(entity='decision', id=d.decision_id, goal_id=goal_id)
        return restore(ManagerDecision, self._write('decision.proposed', idempotency_key, payload, action))

    def get_decision(self, decision_id: str) -> ManagerDecision:
        self._open()
        with self._db.read() as c: return self._decision(c, identifier(decision_id))

    def list_decisions(self, goal_id: str) -> list[ManagerDecision]:
        self._open()
        with self._db.read() as c:
            self._goal(c, identifier(goal_id))
            return [self._decision(c,r[0]) for r in c.execute('SELECT decision_id FROM decisions WHERE goal_id=? ORDER BY rowid',(goal_id,))]

    def accept_attempt(self, goal_id: str, attempt_id: str, *, generation: int, decision_id: str,
                       expected_revision: int, idempotency_key: str) -> WorkItem:
        payload = dict(goal_id=goal_id, attempt_id=identifier(attempt_id), generation=integer(generation, 1), decision_id=identifier(decision_id), expected_revision=expected_revision)
        def action(c):
            goal, work, a = self._current(c, goal_id, attempt_id, generation)
            d = self._decision(c, decision_id)
            if goal.revision != d.goal_revision + 1:
                raise ConflictError('Manager decision is stale; obtain a fresh decision against current state')
            if (d.goal_id, d.work_item_id, d.attempt_id, d.plan_revision, d.action) != (goal_id, work.work_item_id, attempt_id, goal.active_plan_revision, DecisionAction.ACCEPT):
                raise ConflictError('ACCEPT proposal is not bound to the current attempt and plan')
            if a.status != A.RESULT_PENDING or not self._deps_met(c, work): raise TransitionError('Result or dependencies are not ready for acceptance')
            if not d.evidence_ids or not all(self._usable(c, self._evidence(c, e)) for e in d.evidence_ids):
                raise EvidenceError('ACCEPT requires current trusted evidence; a proposal alone cannot complete work')
            self._set_attempt(c, a, A.SUCCEEDED)
            self._set_work(c, work, W.COMPLETED)
            self._refresh(c, goal)
            if all(self._completed(c, item) for item in self._items(c, goal)):
                self._set_goal(c, goal, G.COMPLETED)
            return dict(entity='work', id=work.work_item_id, goal_id=goal_id)
        return restore(WorkItem, self._write('attempt.accepted', idempotency_key, payload, action))

    def retry_work_item(self, goal_id: str, work_item_id: str, *, reconciliation_note: str, expected_revision: int, idempotency_key: str) -> WorkItem:
        """Explicit backend reconciliation, never an automatic recovery replay.

        Caller must first establish that no external process can still write.
        This records that conclusion; it grants no scheduling/execution rights.
        """
        payload = dict(goal_id=goal_id, work_item_id=identifier(work_item_id), reconciliation_note=text(reconciliation_note, 10000), expected_revision=expected_revision)
        def action(c):
            goal, work = self._active_work(c, goal_id, work_item_id)
            if goal.status == G.BLOCKED or work.status not in {W.FAILED, W.BLOCKED}: raise TransitionError('Explicitly unblock the goal or reconcile the failed work first')
            if work.current_attempt_id:
                a = self._attempt(c, work.current_attempt_id)
                if a.status in ACTIVE_ATTEMPTS:
                    self._set_attempt(c, a, A.INTERRUPTED, reconciliation_note)
            self._set_work(c, work, W.READY if self._deps_met(c, work) else W.PENDING)
            c.execute('UPDATE work_items SET current_attempt_id=NULL WHERE work_item_id=?', (work_item_id,))
            return dict(entity='work', id=work_item_id, goal_id=goal_id)
        return restore(WorkItem, self._write('work.retry_authorized', idempotency_key, payload, action))

    def block_work_item(self, goal_id: str, work_item_id: str, *, reason: str, expected_revision: int, idempotency_key: str) -> WorkItem:
        payload = dict(goal_id=goal_id, work_item_id=identifier(work_item_id), reason=text(reason, 10000), expected_revision=expected_revision)
        def action(c):
            _, work = self._active_work(c, goal_id, work_item_id)
            self._set_work(c, work, W.BLOCKED)
            if work.current_attempt_id:
                a = self._attempt(c, work.current_attempt_id)
                if a.status in ACTIVE_ATTEMPTS and a.status != A.NEEDS_RECONCILIATION:
                    self._set_attempt(c, a, A.NEEDS_RECONCILIATION, reason)
            return dict(entity='work', id=work_item_id, goal_id=goal_id)
        return restore(WorkItem, self._write('work.blocked', idempotency_key, payload, action))

    def cancel_work_item(self, goal_id: str, work_item_id: str, *, reason: str, expected_revision: int, idempotency_key: str) -> WorkItem:
        """Record cancellation intent. A live writer still needs reconciliation."""
        payload = dict(goal_id=goal_id, work_item_id=identifier(work_item_id), reason=text(reason, 10000), expected_revision=expected_revision)
        def action(c):
            _, work = self._active_work(c, goal_id, work_item_id)
            self._set_work(c, work, W.CANCELLED)
            if work.current_attempt_id:
                a = self._attempt(c, work.current_attempt_id)
                if a.status in ACTIVE_ATTEMPTS and a.status != A.NEEDS_RECONCILIATION:
                    self._set_attempt(c, a, A.NEEDS_RECONCILIATION, reason)
            return dict(entity='work', id=work_item_id, goal_id=goal_id)
        return restore(WorkItem, self._write('work.cancelled', idempotency_key, payload, action))

    def cancel_goal(self, goal_id: str, *, reason: str, expected_revision: int, idempotency_key: str) -> Goal:
        payload = dict(goal_id=goal_id, reason=text(reason, 10000), expected_revision=expected_revision)
        def action(c):
            goal = self._goal(c, goal_id)
            self._editable(goal)
            self._set_goal(c, goal, G.CANCELLED)
            # Pending proposals as well as the active plan must not retain runnable states.
            for row in c.execute('SELECT work_item_id FROM work_items WHERE goal_id=?', (goal_id,)).fetchall():
                work = self._work(c, row[0])
                if work.status not in {W.COMPLETED, W.CANCELLED, W.SUPERSEDED}:
                    self._set_work(c, work, W.CANCELLED)
                if work.current_attempt_id:
                    a = self._attempt(c, work.current_attempt_id)
                    if a.status in ACTIVE_ATTEMPTS:
                        # Cancellation records intent, not proof that a process stopped.
                        if a.status != A.NEEDS_RECONCILIATION:
                            self._set_attempt(c, a, A.NEEDS_RECONCILIATION, reason)
            return dict(entity='goal', goal_id=goal_id)
        return restore(Goal, self._write('goal.cancelled', idempotency_key, payload, action))

    def set_goal_blocked(self, goal_id: str, *, blocked: bool, reason: str, expected_revision: int, idempotency_key: str) -> Goal:
        if type(blocked) is not bool: raise ValidationError('blocked must be boolean')
        payload = dict(goal_id=goal_id, blocked=blocked, reason=text(reason, 10000), expected_revision=expected_revision)
        def action(c):
            goal = self._goal(c, goal_id)
            self._editable(goal)
            target = G.BLOCKED if blocked else (G.PLANNED if goal.active_plan_revision else G.UNPLANNED)
            if not blocked and goal.status != G.BLOCKED: raise TransitionError('Goal is not blocked')
            require_transition(goal.status, target)
            self._set_goal(c, goal, target)
            return dict(entity='goal', goal_id=goal_id)
        return restore(Goal, self._write('goal.block_changed', idempotency_key, payload, action))

    def events(self, goal_id: str, *, after_revision: int = -1) -> list[DomainEvent]:
        self._open()
        if type(after_revision) is not int or after_revision < -1: raise ValidationError('Invalid event cursor')
        with self._db.read() as c:
            self._goal(c, identifier(goal_id))
            return [DomainEvent(r['event_id'], r['goal_id'], r['goal_revision'], r['kind'], json.loads(r['payload']), r['created_at'])
                for r in c.execute('SELECT * FROM events WHERE goal_id=? AND goal_revision>? ORDER BY sequence', (goal_id, after_revision))]

    def check_integrity(self) -> list[str]:
        self._open()
        return self._db.integrity_check()

    def recovery_checks(self, goal_id: str) -> list[RecoveryCheck]:
        from .recovery import inspect_goal
        self._open()
        with self._evidence_guard(goal_id), self._db.read() as c: return inspect_goal(self, c, self._goal(c, identifier(goal_id)))

    def current_state(self, goal_id: str) -> dict[str, Any]:
        """Consistent read projection, including dynamically checked evidence usability."""
        from .recovery import inspect_goal
        self._open()
        with self._evidence_guard(goal_id), self._db.read() as c:
            goal = self._goal(c, identifier(goal_id))
            items = self._items(c, goal)
            checks = inspect_goal(self, c, goal)
            return {'goal': goal.to_dict(), 'plan': self._plan(c, goal_id, goal.active_plan_revision).to_dict() if goal.active_plan_revision else None,
                'work_items': [w.to_dict() for w in items], 'recovery': [r.to_dict() for r in checks],
                'completion_currently_usable': bool(items) and all(self._completed(c, w) for w in items),
                'evidence_trust_levels': sorted({self._evidence(c, r[0]).trust_level.value for w in items if w.current_attempt_id
                    for r in c.execute('SELECT evidence_id FROM evidence WHERE attempt_id=?', (w.current_attempt_id,))})}

    def work_completion_usable(self, work_item_id: str) -> bool:
        self._open()
        work = self.get_work_item(work_item_id)
        with self._evidence_guard(work.goal_id), self._db.read() as c:
            current=self._work(c, work_item_id)
            return self._completed(c, current) and self._deps_met(c, current)
