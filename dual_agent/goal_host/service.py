"""Trusted GoalHost API over the existing TaskHost/Policy/Scheduler chain."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import threading
import uuid

from dual_agent.execution_context import ExecutionContext
from dual_agent.goal_ledger import (GoalService, ConflictError, TransitionError, EvidenceError,
    ValidationError, AttemptStatus as A, WorkStatus as W, GoalStatus as G)
from dual_agent.goal_ledger.schemas import identifier, integer, text, canonical
from dual_agent.status import read_status
from .identity import reserve, request_hash, ids, load, reservation_path
from .trusted_evidence import CoordinatorEvidenceSource


class GoalHost:
    def __init__(self, host):
        self.host = host
        self.manager_epoch = 'epoch-' + uuid.uuid4().hex
        self.source = CoordinatorEvidenceSource(host.policy.runs)
        self.goals = GoalService.for_task_policy(host.policy, evidence_source=self.source)
        self._contexts: dict[str, ExecutionContext] = {}
        self._guard = threading.RLock()

    def _request(self, raw):
        from desktop_validation.task_api import dto, task_request
        value = dto(raw, {'goalId','workItemId','projectId','templateId','executionMode','budget','expectedRevision','idempotencyKey'},
                    {'goalId','workItemId','projectId','templateId','expectedRevision','idempotencyKey'})
        identifier(value['goalId']); identifier(value['workItemId']); integer(value['expectedRevision']); text(value['idempotencyKey'],150)
        work = self.goals.get_work_item(value['workItemId'])
        if work.goal_id != value['goalId']: raise ConflictError('Work item belongs to another goal')
        goal_text = work.description + '\nAcceptance:\n' + '\n'.join(work.acceptance_criteria)
        generation = work.generation if work.current_attempt_id else work.generation+1
        if generation>1:
            goal_text += '\nGoalHost retry generation: '+str(generation)
            advice=[d.reason for d in self.goals.list_decisions(work.goal_id)
                    if d.work_item_id==work.work_item_id and d.action.value=='REQUEST_REPAIR']
            if advice: goal_text += '\nUntrusted Manager repair suggestion (no authority):\n'+advice[-1][:4000]
        payload = dict(projectId=value['projectId'], title=work.title, goal=goal_text, templateId=value['templateId'],
                       executionMode=value.get('executionMode','mock'))
        if 'budget' in value: payload['budget'] = value['budget']
        payload = task_request(json.dumps(payload, ensure_ascii=False))
        return value, work, payload

    def submit_work_item(self, raw: str, *, inherit_dependencies: bool = False) -> dict:
        with self._guard:
            value, work, payload = self._request(raw)
            old = self.goals.dispatch_for_request(value['idempotencyKey'])
            if old is None and work.current_attempt_id:
                try: old = self.goals.get_dispatch(work.current_attempt_id)
                except ValueError: raise ConflictError('Work is already claimed outside GoalHost')
            if old is not None:
                if (old['goal_id'], old['work_item_id'], old['request_hash'], old['scope']) != (
                    value['goalId'],work.work_item_id,request_hash(payload),list(work.allowed_write_scope)):
                    raise ConflictError('Repeated dispatch request changed identity/content/scope')
                # A duplicate is a query/reconciliation, NEVER another submit.
                return self.reconcile_attempt(old['attempt_id'])
            goal = self.goals.get_goal(value['goalId'])
            if goal.revision != value['expectedRevision']: raise ConflictError('Stale GoalHost expectedRevision')
            if work.status != W.READY or work.plan_revision != goal.active_plan_revision:
                raise TransitionError('GoalHost can only submit current READY work')
            if work.work_item_id not in {w.work_item_id for w in self.goals.ready_work_items(goal.goal_id, available_capacity=2)}:
                # Capacity filtering must not prevent a later valid READY item;
                # the attempt transaction itself checks the dependency closure.
                if goal.status not in {G.PLANNED,G.RUNNING}: raise TransitionError('Goal is not dispatchable')
            transfer = None
            if inherit_dependencies and work.dependencies:
                from .inheritance import DependencyPatch
                transfer = DependencyPatch(self, work, payload['executionMode'])
                transfer.inspect()  # Fail before creating any dispatch intent.
            rid = 'goal-' + hashlib.sha256(canonical([goal.goal_id,work.plan_revision,work.work_item_id,work.generation+1]).encode()).hexdigest()[:48]
            reservation = reserve(self.host, json.dumps({**payload,'requestId':rid},ensure_ascii=False), list(work.allowed_write_scope))
            dispatch = {name:reservation[name] for name in ('request_id','project_id','schedule_id','task_id','request_hash','config_hash','scope')}
            dispatch['request_key'] = value['idempotencyKey']
            a = self.goals.create_attempt(goal.goal_id, work.work_item_id, executor=payload['executionMode'], run_id=reservation['run_id'],
                workspace_ref=reservation['workspace_ref'], worktree_ref=reservation['worktree_ref'], base_head=reservation['base_head'],
                dispatch=dispatch, expected_revision=value['expectedRevision'], idempotency_key='goal-submit:'+value['idempotencyKey'])
            mapping = self.goals.get_dispatch(a.attempt_id)
            context = ExecutionContext(self.host.policy.runs, mapping['task_id'], mapping['schedule_id'], mapping['run_id'],
                lambda config,state,run_dir,write_lock,resource_lock:self.source.observe(mapping,config,state,run_dir,write_lock,resource_lock),
                preparer=(lambda config,run_dir:transfer.prepare(mapping,config,run_dir)) if transfer else None)
            self._contexts[a.attempt_id] = context
            try:
                self.host.submit(json.dumps({**payload,'requestId':rid},ensure_ascii=False), goal_reservation=reservation, execution_context=context)
            except Exception:
                self._stage(mapping, 'UNCERTAIN', 'TaskHost acknowledgement absent; query original identity, never replay')
                raise
            self._stage(mapping, 'ACKNOWLEDGED', 'TaskHost accepted the original stable request')
            return self.goals.get_dispatch(a.attempt_id)

    def _call(self, mapping, method, *, event_key, **kwargs):
        gid = mapping['goal_id']
        for _ in range(3):
            revision = self.goals.get_goal(gid).revision
            try:
                return method(gid, mapping['attempt_id'], expected_revision=revision,
                    idempotency_key='host:'+mapping['attempt_id']+':'+event_key, **kwargs)
            except ConflictError:
                current = self.goals.get_goal(gid)
                work = self.goals.get_work_item(mapping['work_item_id'])
                if current.revision == revision or work.current_attempt_id != mapping['attempt_id'] or work.generation != mapping['generation']:
                    raise
        raise ConflictError('Concurrent Goal update; reconcile again with current facts')

    def _stage(self, mapping, stage, reason):
        current = self.goals.get_dispatch(mapping['attempt_id'])
        if current['stage'] == stage or current['stage'] == 'FINISHED': return
        # Unknown recovery does not erase an already persisted cancellation
        # intent or replay the earlier UNCERTAIN event with a new revision.
        if current['stage']=='CANCEL_REQUESTED' and stage=='UNCERTAIN': return
        self._call(mapping, self.goals.record_dispatch_stage, event_key='dispatch-'+stage, stage=stage, reason=reason)

    def _event(self, mapping, event, reason=''):
        return self._call(mapping, self.goals.record_attempt_event, event_key=event,
            generation=mapping['generation'], event=event, reason=reason, fact_id=mapping['attempt_id']+':'+event)

    def _binding_facts(self, mapping):
        # Historical files are used only to identify a possibly accepted request.
        # They NEVER create a trusted execution result or evidence assessment.
        key, sid, tid = ids(mapping['request_id'])
        if (sid,tid) != (mapping['schedule_id'],mapping['task_id']): raise ConflictError('Stored identity mapping is inconsistent')
        reservation = load(reservation_path(self.host, mapping['request_id']))
        for name in ('run_id','request_hash','config_hash','project_id','workspace_ref','worktree_ref','base_head','scope'):
            if reservation[name] != mapping[name]: raise ConflictError('Reservation and ledger mapping disagree: '+name)
        file = self.host.ledger / (key+'.json')
        accepted = False
        if file.exists():
            saved = load(file)
            if (saved.get('requestId'),saved.get('payloadHash'),saved.get('taskId'),saved.get('scheduleId'),saved.get('goalRunId'),saved.get('goalConfigHash')) != (
                mapping['request_id'],mapping['request_hash'],tid,sid,mapping['run_id'],mapping['config_hash']):
                raise ConflictError('TaskHost request does not match dispatch mapping')
            accepted = True
        return accepted

    def reconcile_attempt(self, attempt_id: str) -> dict:
        with self._guard:
            mapping = self.goals.get_dispatch(identifier(attempt_id))
            a = self.goals.get_attempt(attempt_id)
            work = self.goals.get_work_item(mapping['work_item_id'])
            if a.generation != mapping['generation'] or work.current_attempt_id != attempt_id or work.generation != mapping['generation']:
                raise ConflictError('Late callback/reconciliation for an old generation')
            accepted = self._binding_facts(mapping)
            context = self._contexts.get(attempt_id)
            if a.status in {A.SUCCEEDED,A.FAILED,A.CANCELLED,A.INTERRUPTED}:
                return {**self.goals.get_dispatch(attempt_id),'recovery':'TERMINAL','trusted_now':a.status==A.SUCCEEDED and self._currently_usable(mapping)}
            if context is None:
                self._stage(mapping, 'UNCERTAIN', 'No live trusted execution context after restart; no replay')
                if work.status not in {W.BLOCKED,W.CANCELLED}:
                    self.goals.block_work_item(mapping['goal_id'],work.work_item_id, reason='Unattested external state after restart; request found='+str(accepted),
                        expected_revision=self.goals.get_goal(mapping['goal_id']).revision,idempotency_key='host:'+attempt_id+':restart-block')
                return {**self.goals.get_dispatch(attempt_id),'recovery':'NEEDS_RECONCILIATION' if accepted else 'UNKNOWN_SIDE_EFFECT','trusted_now':False}
            if not accepted and (context.started or context.result is not None):
                self._stage(mapping,'UNCERTAIN','Live execution exists but original host request record is missing')
                if work.status not in {W.BLOCKED,W.CANCELLED}:
                    self.goals.block_work_item(mapping['goal_id'],work.work_item_id,reason='Host request record lost; identity reconciliation required',
                        expected_revision=self.goals.get_goal(mapping['goal_id']).revision,idempotency_key='host:'+attempt_id+':missing-request')
                return {**self.goals.get_dispatch(attempt_id),'recovery':'UNKNOWN_SIDE_EFFECT','trusted_now':False}
            if accepted and mapping['stage'] == 'INTENT': self._stage(mapping,'ACKNOWLEDGED','Original request found by stable identity')
            if work.status == W.CANCELLED:
                return {**self.goals.get_dispatch(attempt_id),'recovery':'NEEDS_RECONCILIATION','trusted_now':False}
            result = context.result
            if context.started and a.status == A.DISPATCH_INTENT:
                self._event(mapping,'STARTED')
                a = self.goals.get_attempt(attempt_id)
            if result is not None:
                if not result.processes_cleared or not result.lock_released:
                    if work.status != W.BLOCKED:
                        self.goals.block_work_item(mapping['goal_id'], work.work_item_id, reason='Executor cleanup or lock release unconfirmed',
                            expected_revision=self.goals.get_goal(mapping['goal_id']).revision,idempotency_key='host:'+attempt_id+':cleanup-block')
                    return {**self.goals.get_dispatch(attempt_id),'recovery':'NEEDS_RECONCILIATION','trusted_now':False}
                if (self.host.policy.runs/'schedule'/(mapping['schedule_id']+'.cancel')).exists() and a.status in {A.DISPATCH_INTENT,A.RUNNING,A.RESULT_PENDING}:
                    self.source.forget(attempt_id)
                    self._event(mapping,'CANCELLED',reason='Cancellation intent and direct Coordinator cleanup both confirmed')
                    self._stage(mapping,'FINISHED','Cancellation reconciled after confirmed process cleanup')
                    return {**self.goals.get_dispatch(attempt_id),'recovery':'TERMINAL','trusted_now':False}
                if result.stop_reason == 'verified' and result.success:
                    if a.status == A.RUNNING:
                        self._event(mapping,'RESULT_PRODUCED')
                    self.source.finalize(mapping,result)
                    self._stage(mapping,'FINISHED','Direct Coordinator result persisted; acceptance still requires a fresh evidence lease')
                else:
                    event = 'CANCELLED' if result.stop_reason == 'cancelled' else 'FAILED'
                    if a.status in {A.DISPATCH_INTENT,A.RUNNING}:
                        self._event(mapping,event,reason=result.stop_reason+': '+(result.error_message or 'Coordinator terminal result'))
                    self._stage(mapping,'FINISHED','Direct Coordinator terminal failure/cancellation confirmed')
            else:
                # Queued cancellation can be confirmed by the live original
                # scheduler, without pretending a submitted task had started.
                with self.host.scheduler._cv:
                    record = next((r for r in self.host.scheduler.tasks if r.schedule_id==mapping['schedule_id']),None)
                    cancelled = record is not None and record.state=='cancelled' and not context.started
                    uncertain = record is None or (record.state=='failed' and context.result is None)
                if context.result is not None:
                    return self.reconcile_attempt(attempt_id)
                if cancelled and a.status == A.DISPATCH_INTENT:
                    self._event(mapping,'CANCELLED',reason='Original scheduler confirmed queued cancellation before Coordinator start')
                    self._stage(mapping,'FINISHED','Queued cancellation confirmed')
                elif uncertain:
                    self._stage(mapping,'UNCERTAIN','No live original scheduler record confirms execution outcome')
                    if work.status != W.BLOCKED:
                        self.goals.block_work_item(mapping['goal_id'],work.work_item_id,reason='Dispatch outcome unknown; no automatic retry',
                            expected_revision=self.goals.get_goal(mapping['goal_id']).revision,idempotency_key='host:'+attempt_id+':dispatch-block')
                    return {**self.goals.get_dispatch(attempt_id),'recovery':'UNKNOWN_SIDE_EFFECT','trusted_now':False}
            return {**self.goals.get_dispatch(attempt_id),'recovery':'LIVE_CONTEXT','trusted_now':self._currently_usable(mapping)}

    def _currently_usable(self, mapping):
        a = self.goals.get_attempt(mapping['attempt_id'])
        path = a.run_id+'/verification.json'
        with self.source.guard(mapping['goal_id']):
            try:
                sha = self.source.artifact_hash(a,path)
                return self.source.assess(a,path,sha) is not None
            except EvidenceError: return False

    def reconcile_goal(self, goal_id: str) -> list[dict]:
        return [self.reconcile_attempt(m['attempt_id']) for m in self.goals.list_dispatches(identifier(goal_id))
                if self.goals.get_work_item(m['work_item_id']).current_attempt_id == m['attempt_id']]

    def current_state(self, goal_id: str):
        return {'ledger':self.goals.current_state(identifier(goal_id)), 'dispatches':self.goals.list_dispatches(goal_id)}

    def execution_identity(self, attempt_id: str) -> dict:
        return self.goals.get_dispatch(identifier(attempt_id))

    def verification_status(self, attempt_id: str) -> dict:
        mapping=self.goals.get_dispatch(identifier(attempt_id))
        attempt=self.goals.get_attempt(attempt_id)
        path=mapping['run_id']+'/verification.json'
        with self.source.guard(mapping['goal_id']):
            try:
                sha=self.source.artifact_hash(attempt,path)
                assessment=self.source.assess(attempt,path,sha)
                if assessment is not None:
                    return dict(available=True,attempt_id=attempt_id,run_id=attempt.run_id,evidence_path=path,sha256=sha,
                        head_or_snapshot_ref=assessment.head_or_snapshot_ref,trust_level=assessment.trust_level.value,
                        execution_mode=attempt.executor)
            except EvidenceError: pass
        return dict(available=False,attempt_id=attempt_id,run_id=attempt.run_id,
            reason='No fresh lock-backed Coordinator attestation in this host epoch')

    def accept_result(self, attempt_id: str, *, reason: str, idempotency_key: str) -> object:
        """Deterministic backend driver. Tests label its use OFFLINE/FAKE_CLI.

        It is not a Manager model and cannot bypass the fresh evidence gate.
        """
        with self._guard:
            text(idempotency_key,150)
            mapping = self.goals.get_dispatch(identifier(attempt_id))
            current = self.goals.get_attempt(attempt_id)
            if current.status == A.SUCCEEDED: return self.goals.get_work_item(current.work_item_id)
            self.reconcile_attempt(attempt_id)
            if not self._currently_usable(mapping): raise EvidenceError('No currently usable observed Coordinator result')
            # Hold cooperative locks across evidence, decision, and acceptance.
            # Nested ledger guards reuse this lease; all expensive checks precede SQL.
            with self.source.guard(mapping['goal_id']):
                ref = self._call(mapping,self.goals.record_evidence,event_key='evidence:'+idempotency_key,
                    generation=mapping['generation'],evidence_path=mapping['run_id']+'/verification.json')
                proposal = dict(plan_revision=mapping['plan_revision'],action='ACCEPT',reason=text(reason,10000),
                    work_item_id=mapping['work_item_id'],attempt_id=attempt_id,evidence_ids=[ref.evidence_id])
                decision = self.goals.record_decision(mapping['goal_id'],proposal,
                    expected_revision=self.goals.get_goal(mapping['goal_id']).revision,idempotency_key='host:'+attempt_id+':decision:'+idempotency_key)
                return self._call(mapping,self.goals.accept_attempt,event_key='accept:'+idempotency_key,
                    generation=mapping['generation'],decision_id=decision.decision_id)

    def cancel_attempt(self, attempt_id: str) -> dict:
        with self._guard:
            mapping = self.goals.get_dispatch(identifier(attempt_id))
            a = self.goals.get_attempt(attempt_id)
            if a.status in {A.SUCCEEDED,A.FAILED,A.CANCELLED,A.INTERRUPTED}: return self.reconcile_attempt(attempt_id)
            self.host.cancel(json.dumps({'scheduleId':mapping['schedule_id']}))
            self.source.forget(attempt_id)
            self._stage(mapping,'CANCEL_REQUESTED','Cancellation requested; process exit not implied')
            return self.reconcile_attempt(attempt_id)

    def close(self):
        self.goals.close()

    def retry_safety(self, attempt_id: str) -> dict:
        """Current original-host cleanup, never a historical JSON assertion."""
        a=self.goals.get_attempt(identifier(attempt_id))
        mapping=self.goals.get_dispatch(attempt_id)
        ctx=self._contexts.get(attempt_id)
        if a.status != A.FAILED:
            return dict(safe=False,reason='Attempt is not an actual terminal failure')
        if ctx is None or ctx.result is None:
            return dict(safe=False,reason='UNKNOWN_SIDE_EFFECT: no current direct result')
        r=ctx.result
        if not r.processes_cleared or not r.lock_released or mapping['stage']!='FINISHED':
            return dict(safe=False,reason='Process exit and resource release are unconfirmed')
        # Only deterministic trusted verification failures may be task-retried.
        current_checks=[v for v in r.verification if not v.get('superseded')]
        if (r.stop_reason not in {'max_repair','repeated_failure'} or
            not any(v.get('type')=='command' and v.get('ok') is False for v in current_checks) or
            any(v.get('type')!='command' and v.get('ok') is False for v in current_checks)):
            return dict(safe=False,reason='Failure class requires explicit reconciliation: '+r.stop_reason)
        from dual_agent.locks import WriteLockManager, ResourceLockManager, default_resource_lock_dir
        from dual_agent.coordinator import default_lock_dir
        lock,reason=WriteLockManager(default_lock_dir()).try_acquire('manager-retry-inspection',[Path(a.workspace_ref)])
        if lock is None: return dict(safe=False,reason=reason)
        resources=None
        try:
            # Original scheduler config supplies resource names, never the model.
            with self.host.scheduler._cv:
                record=next((r for r in self.host.scheduler.tasks if r.schedule_id==mapping['schedule_id']),None)
                names=list(record.config.resource_locks) if record else None
            if names is None: return dict(safe=False,reason='Original scheduler resource facts unavailable')
            if names:
                resources,reason=ResourceLockManager(default_resource_lock_dir()).try_acquire_all('manager-retry-inspection',names)
                if resources is None: return dict(safe=False,reason=reason)
            return dict(safe=True,reason='Current Coordinator confirmed trusted verification failure, process cleanup and released locks')
        finally:
            if resources: resources.release()
            lock.release()
