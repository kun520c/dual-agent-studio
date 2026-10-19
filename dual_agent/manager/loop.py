"""Deterministic bounded controller over GoalService and the original GoalHost.

Each tick performs one planning/dispatch/reconcile/review/apply transition.
Persisted intents survive crashes; an ambiguous action is coordinated, never
blindly replayed. Manager progress is not execution truth or verification.
"""
from __future__ import annotations
import json
import time
import uuid
from dual_agent.goal_ledger import (ConflictError, TransitionError, EvidenceError, ValidationError,
    AttemptStatus as A, WorkStatus as W, GoalStatus as G)
from dual_agent.goal_ledger.schemas import digest
from dual_agent.goal_ledger.state_machine import ACTIVE_ATTEMPTS
from .models import Limits
from .provider import CallControl, ProviderError
from .context import build_context
from .planner import validate_plan
from .decisions import validate_decision

TERMINAL={'COMPLETED','BLOCKED','ESCALATED','CANCELLED','NEEDS_RECONCILIATION','BUDGET_EXHAUSTED'}


class ManagerLoop:
    def __init__(self, goal_host, provider, binding, limits=None):
        self.gh=goal_host; self.s=goal_host.goals; self.provider=provider; self.binding=binding
        self.limits=limits or Limits()
        if provider.mode=='production_transport':
            from .provider import TransportManagerProvider
            from .transport import ProductionManagerTransport
            from .api_transport import APIManagerTransport
            if (type(provider) is not TransportManagerProvider or type(provider.transport) not in {ProductionManagerTransport,APIManagerTransport}
                or provider.transport.goals is not self.s or provider.transport.authority.project_id!=binding.project_id):
                raise ProviderError('Manager lacks matching trusted invocation service')
        elif provider.mode not in {'fake_manager','fake_transport'}:
            raise ProviderError('No reviewed production Manager transport')
        if binding.execution_mode=='real':
            raise ProviderError('Production Executor is independently disabled for Manager orchestration')
        # An epoch belongs to the GoalHost lifecycle, shared by all loops on it.
        self.epoch=goal_host.manager_epoch

    def start(self, goal_id):
        config=dict(binding=self.binding.to_dict(),limits=self.limits.to_dict(),provider_mode=self.provider.mode)
        if self.provider.mode=='production_transport':
            t=self.provider.transport
            from .transport import _safe_text
            config['binding']['description']=_safe_text(config['binding']['description'])
            if goal_id!=t.authority.goal_id:raise ProviderError('Manager goal authorization mismatch')
            t.authority.reload()
            config.update(backend=t.profile.summary(),authorization=t.authority.identity)
        return self.s.manager_start(goal_id,config)

    def state(self, goal_id): return self.s.manager_state(goal_id)
    def pause(self, goal_id): self.s.manager_signal(goal_id,'PAUSE')
    def cancel(self, goal_id): self.s.manager_signal(goal_id,'CANCEL')

    def resume(self, goal_id):
        state=self.state(goal_id)
        if state['phase']!='PAUSED': raise TransitionError('Only a paused Manager may resume automatically')
        self.s.manager_signal(goal_id,'')

    def _save(self, **changes):
        self.st.update(changes)
        self.st=self.s.manager_save(self.gid,self.owner,self.st)
        return self.st

    def _audit(self, action_id, kind, status, **details):
        return self.s.manager_action(self.gid,self.owner,action_id,
            dict(kind=kind,status=status,host_epoch=self.epoch,**details))

    def _intent(self, kind, identity):
        # A cancelled read-only provider request can be retried after pause;
        # mutation identities stay stable, ambiguous intents never replay.
        round_id=self.st['provider_calls'] if kind in {'PLAN','REPLAN','REVIEW'} else 0
        action_id='ma-'+digest([self.st['manager_run_id'],kind,identity,round_id])[:48]
        self._audit(action_id,kind,'INTENT',goal_revision=self.s.get_goal(self.gid).revision,
                    plan_revision=self.s.get_goal(self.gid).active_plan_revision,identity=identity)
        self._save(last_action=action_id)
        return action_id

    def _stop(self, phase, reason):
        return self._save(phase=phase,stop_reason=str(reason)[:4000])

    def _call(self, method, context):
        if self.st['decisions']>=self.limits.max_decisions:
            raise ProviderError('Manager decision budget exhausted')
        self._save(decisions=self.st['decisions']+1,provider_calls=self.st['provider_calls']+1)
        control=CallControl(time.monotonic()+min(self.limits.provider_timeout,
            max(.001,self.limits.max_seconds-self.st['elapsed_seconds'])),
            lambda: self.state(self.gid)['signal'] in {'CANCEL','PAUSE'},
            dict(goal_id=self.gid,manager_run_id=self.st['manager_run_id'],owner=self.owner,
                 action_id=self.st['last_action'],request_id='mc-'+digest(self.st['last_action'])[:48]))
        if self.provider.mode=='production_transport':
            context['system_capabilities']['production_manager_transport']=True
        # Provider implementations must honor CallControl and clean up their
        # transport. No SQLite transaction or execution lock spans this call.
        control.check(); result=method(context,control); control.check()
        if self.s.get_goal(self.gid).revision!=context['user_goal']['revision']:
            raise ConflictError('Goal changed while Manager was responding')
        return result

    def _planning(self, replan=False):
        goal=self.s.get_goal(self.gid)
        if replan:
            works=self.s.list_work_items(self.gid)
            if any(w.current_attempt_id and (w.status==W.COMPLETED or
                not self.gh.retry_safety(w.current_attempt_id)['safe']) for w in works):
                raise EvidenceError('Replan requires quiescent attempts and no cross-revision accepted artifact reuse')
        action=self._intent('REPLAN' if replan else 'PLAN',goal.active_plan_revision)
        context=build_context(self.gh,self.gid,self.binding)
        raw=self._call(self.provider.propose_replan if replan else self.provider.generate_plan,context)
        proposal=validate_plan(raw,goal=goal,binding=self.binding,limits=self.limits,parent_revision=goal.active_plan_revision)
        p=self.s.create_plan(self.gid,proposal,parent_revision=goal.active_plan_revision,
            expected_revision=goal.revision,idempotency_key=action+'-create',source='manager:'+self.provider.mode)
        self._audit(action,'REPLAN' if replan else 'PLAN','APPLIED',output_hash=digest(raw),
            normalized_plan_hash=p.content_hash,plan_revision=p.revision)
        return self._save(phase='ACTIVATE',plan_revision=p.revision)

    def _activate(self):
        p=self.st['plan_revision']; goal=self.s.get_goal(self.gid)
        if goal.active_plan_revision==p: return self._save(phase='READY')
        action=self._intent('ACTIVATE',p)
        self.s.activate_plan(self.gid,p,expected_revision=goal.revision,idempotency_key=action)
        self._audit(action,'ACTIVATE','APPLIED',plan_revision=p)
        return self._save(phase='READY')

    def _review(self, work):
        action=self._intent('REVIEW',[work.work_item_id,work.generation])
        context=build_context(self.gh,self.gid,self.binding,target=work)
        raw=self._call(self.provider.review_result,context)
        proposal=validate_decision(raw,work)
        self._audit(action,'REVIEW','APPLIED',output_hash=digest(raw),proposal=proposal,
            evidence=context['trusted_execution_facts']['verification'],
            failure=context['trusted_execution_facts']['failure'])
        return self._save(phase='APPLY',pending_decision=proposal)

    def _apply(self):
        proposal=self.st['pending_decision']; work=self.s.get_work_item(proposal['work_item_id'])
        validate_decision(proposal,work)
        goal=self.s.get_goal(self.gid)
        if goal.active_plan_revision!=proposal['plan_revision']: raise ConflictError('Plan changed before decision execution')
        aid=proposal['attempt_id']; kind=proposal['action']
        action=self._intent(kind,[aid,work.generation])
        if kind=='ACCEPT':
            self.gh.accept_result(aid,reason=proposal['reason'],idempotency_key=action)
            self._audit(action,kind,'APPLIED',attempt_id=aid,actual_action='GoalHost.accept_result')
            return self._save(phase='READY',waiting_attempt=None,pending_decision=None)
        # Evidence IDs are selected exclusively by GoalHost for ACCEPT.
        decision=self.s.record_decision(self.gid,{**proposal,'evidence_ids':[]},
            expected_revision=goal.revision,idempotency_key=action+'-decision')
        if kind=='REQUEST_REPAIR':
            count=self.st['repairs'].get(work.work_item_id,0)
            if count>=self.limits.max_repairs: raise EvidenceError('Per-work Manager repair budget exhausted')
            if len(self.st['failures'])>=self.limits.max_failures: raise EvidenceError('Total Manager failure budget exhausted')
            safety=self.gh.retry_safety(aid)
            if not safety['safe']: raise EvidenceError('Unsafe Manager retry: '+safety['reason'])
            self.s.retry_work_item(self.gid,work.work_item_id,reconciliation_note=safety['reason'],
                expected_revision=self.s.get_goal(self.gid).revision,idempotency_key=action+'-retry')
            repairs={**self.st['repairs'],work.work_item_id:count+1}
            self._audit(action,kind,'APPLIED',decision_id=decision.decision_id,safety=safety,actual_action='GoalService.retry_work_item')
            return self._save(phase='READY',repairs=repairs,waiting_attempt=None,pending_decision=None)
        self._audit(action,kind,'APPLIED',decision_id=decision.decision_id,
            actual_action='request new plan' if kind=='REPLAN' else 'stop Manager')
        if kind=='REPLAN': return self._save(phase='REPLAN',waiting_attempt=None,pending_decision=None)
        return self._stopping('ESCALATED' if kind=='ESCALATE' else 'BLOCKED',proposal['reason'])

    def _dispatch(self, work):
        action=self._intent('DISPATCH',[work.work_item_id,work.generation+1])
        request=dict(goalId=self.gid,workItemId=work.work_item_id,projectId=self.binding.project_id,
            templateId=self.binding.template_id,executionMode=self.binding.execution_mode,
            expectedRevision=self.s.get_goal(self.gid).revision,idempotencyKey=action)
        mapping=self.gh.submit_work_item(json.dumps(request,ensure_ascii=False),inherit_dependencies=True)
        self._audit(action,'DISPATCH','APPLIED',attempt_id=mapping['attempt_id'],run_id=mapping['run_id'],
            actual_action='GoalHost.submit_work_item')
        return self._save(phase='READY',waiting_attempt=mapping['attempt_id'])

    def _stopping(self, final_phase, reason):
        """Cancellation intent is never process-cleanup confirmation."""
        for w in self.s.list_work_items(self.gid):
            if not w.current_attempt_id: continue
            a=self.s.get_attempt(w.current_attempt_id)
            if a.status in ACTIVE_ATTEMPTS:
                if a.status==A.RESULT_PENDING and final_phase not in {'CANCELLED','BUDGET_EXHAUSTED'}:
                    continue  # A stopped verified result stays unaccepted for inspection.
                result=self.gh.cancel_attempt(a.attempt_id)
                a=self.s.get_attempt(a.attempt_id)
                if a.status in ACTIVE_ATTEMPTS:
                    if result['recovery'] in {'UNKNOWN_SIDE_EFFECT','NEEDS_RECONCILIATION'}:
                        return self._stop('NEEDS_RECONCILIATION','Cancellation cleanup cannot be confirmed: '+reason)
                    return self._save(phase='STOPPING',stop_reason=reason,final_phase=final_phase)
        goal=self.s.get_goal(self.gid)
        if final_phase=='CANCELLED' and goal.status not in {G.CANCELLED,G.COMPLETED}:
            self.s.cancel_goal(self.gid,reason=reason,expected_revision=goal.revision,
                idempotency_key=self.st['manager_run_id']+'-cancel')
        elif final_phase in {'BLOCKED','ESCALATED','BUDGET_EXHAUSTED'} and goal.status not in {G.CANCELLED,G.COMPLETED,G.BLOCKED}:
            self.s.set_goal_blocked(self.gid,blocked=True,reason=reason,expected_revision=goal.revision,
                idempotency_key=self.st['manager_run_id']+'-halt')
        return self._stop(final_phase,reason)

    def _advance(self):
        # Signals are checked even while a bounded provider call is in progress.
        signal=self.state(self.gid)['signal']
        if self.st['phase'] in TERMINAL and not (self.st['phase']=='NEEDS_RECONCILIATION' and signal=='CANCEL'):
            return self.st
        if signal=='CANCEL': return self._stopping('CANCELLED','User cancellation requested')
        if signal=='PAUSE':
            return self._save(phase='PAUSED',paused_phase=self.st.get('paused_phase','READY') if self.st['phase']=='PAUSED' else self.st['phase'],
                stop_reason='User pause requested')
        if self.st['phase']=='STOPPING': return self._stopping(self.st['final_phase'],self.st['stop_reason'])
        if self.st['elapsed_seconds']>=self.limits.max_seconds:
            return self._stopping('BUDGET_EXHAUSTED','Manager runtime budget exhausted')
        waiting=self.gh._contexts.get(self.st.get('waiting_attempt'))
        if waiting is not None and waiting.preparing:
            # Avoid repeatedly taking the source lease needed by the original
            # Coordinator's dependency preparer. No new work/model/ACCEPT here.
            return self._save(phase='READY')
        # Always coordinate before dispatch/review, including after pause/restart.
        mappings=self.gh.reconcile_goal(self.gid)
        if any(m['recovery'] in {'UNKNOWN_SIDE_EFFECT','NEEDS_RECONCILIATION'} for m in mappings):
            return self._stop('NEEDS_RECONCILIATION','Original host cannot confirm execution state; no replay')
        if self.st['host_epoch'] and self.st['host_epoch']!=self.epoch:
            if mappings: return self._stop('NEEDS_RECONCILIATION','Host lifecycle changed; historical JSON cannot restore trust')
        self._save(host_epoch=self.epoch)
        # An unresolved intent could have had effects before a crash.
        pending=[a for a in self.s.manager_actions(self.gid) if a['status']=='INTENT']
        if pending: return self._stop('NEEDS_RECONCILIATION','Unresolved Manager action '+pending[-1]['action_id'])
        goal=self.s.get_goal(self.gid)
        if goal.status==G.COMPLETED:
            if not self.s.current_state(self.gid)['completion_currently_usable']:
                return self._stop('NEEDS_RECONCILIATION','Historical goal completion has no current attestation')
            return self._stop('COMPLETED','All active work accepted with current trusted evidence')
        if goal.status in {G.CANCELLED,G.BLOCKED}: return self._stop(goal.status.value,'Goal is terminal or blocked')
        if self.st['phase']=='PAUSED': self._save(phase=self.st.get('paused_phase','READY'),stop_reason='')
        if self.st['phase']=='ACTIVATE': return self._activate()
        if self.st['phase']=='APPLY': return self._apply()
        if self.st['phase']=='REPLAN': return self._planning(True)
        if goal.active_plan_revision is None: return self._planning()
        works=self.s.list_work_items(self.gid)
        active=[w for w in works if w.current_attempt_id and self.s.get_attempt(w.current_attempt_id).status in ACTIVE_ATTEMPTS
                and w.status not in {W.AWAITING_CONFIRMATION,W.BLOCKED}]
        failures=sorted(set(self.st['failures']) | {w.current_attempt_id for w in works if w.status==W.FAILED and w.current_attempt_id})
        if failures!=self.st['failures']: self._save(failures=failures)
        if len(failures)>self.limits.max_failures:
            return self._stopping('BUDGET_EXHAUSTED','Total failure budget exceeded')
        for w in works:
            if w.status in {W.AWAITING_CONFIRMATION,W.FAILED}: return self._review(w)
            if w.status==W.BLOCKED: return self._stop('BLOCKED','Work item is blocked')
            if w.status==W.COMPLETED and not self.s.work_completion_usable(w.work_item_id):
                if active:
                    # The original executor's inheritance preparer can hold a
                    # short evidence lease. Do not classify lock contention as
                    # revocation or dispatch more work. Recheck after completion.
                    return self._save(phase='READY',waiting_attempt=active[0].current_attempt_id)
                return self._stop('BLOCKED','Accepted dependency evidence is no longer usable')
        ready=self.s.ready_work_items(self.gid,available_capacity=max(0,self.limits.max_concurrent-len(active)))
        if ready: return self._dispatch(ready[0])
        if active: return self._save(phase='READY',waiting_attempt=active[0].current_attempt_id)
        return self._stop('BLOCKED','No safe READY work or live execution remains')

    def tick(self, goal_id):
        # Instance fields below are only used while holding the durable owner.
        self.start(goal_id)  # Reject a differently configured driver even without run_goal().
        owner='owner-'+uuid.uuid4().hex
        state=self.s.manager_claim(goal_id,owner)
        self.gid,self.owner,self.st=goal_id,owner,state
        started=time.monotonic()
        try:
            try:
                if self.s.manager_calls_reconcile(goal_id,owner):
                    if self.state(goal_id)['signal']=='CANCEL':
                        # An unknown model charge/process does not prevent the
                        # original host from cancelling its Executor attempts.
                        # Overall cleanup remains unconfirmed and never replays.
                        self._stopping('CANCELLED','User cancellation requested; Manager call remains uncertain')
                    self._stop('NEEDS_RECONCILIATION','Manager model call needs reconciliation; no replay')
                else:
                    with self.s.manager_effects(goal_id,owner):self._advance()
            except (ValueError,OSError,RuntimeError) as exc:
                message=str(exc)
                if self.provider.mode=='production_transport':
                    from .transport import _safe_text
                    message=_safe_text(message)
                reason=type(exc).__name__+': '+message
                action=self.st.get('last_action')
                if action:
                    records={r['action_id']:r for r in self.s.manager_actions(goal_id)}
                    record=records.get(action)
                    if record and record['status']=='INTENT':
                        self._audit(action,record['kind'],'REJECTED',error=type(exc).__name__,reason=message[:4000])
                # Dispatch failures may have external effects. Reconcile on the
                # next explicit recovery; never treat them as a retryable repair.
                mappings=self.s.list_dispatches(goal_id)
                uncertain=any(m['stage'] in {'INTENT','UNCERTAIN'} for m in mappings) or any(
                    c['state'] in {'INTENT','STARTED','NEEDS_RECONCILIATION'} for c in self.s.manager_calls(goal_id))
                signal=self.state(goal_id)['signal']
                if uncertain: self._stop('NEEDS_RECONCILIATION',reason)
                elif signal in {'CANCEL','PAUSE'}:
                    self._stopping('CANCELLED','User cancellation requested') if signal=='CANCEL' else self._save(phase='PAUSED',paused_phase=self.st['phase'])
                else: self._stopping('BLOCKED',reason)
            self._save(elapsed_seconds=self.st['elapsed_seconds']+time.monotonic()-started)
            return self.st
        finally:
            self.s.manager_release(goal_id,owner)

    def run_goal(self, goal_id, limits=None):
        if limits is not None and limits!=self.limits: raise ValidationError('Run limits must match the persisted configuration')
        self.start(goal_id)
        # Finite decisions, tasks, repair attempts and runtime; waiting consumes
        # time but never re-submits a running attempt or consumes decision rounds.
        while True:
            result=self.tick(goal_id)
            if result['phase'] in TERMINAL|{'PAUSED'}: return result
            if result['waiting_attempt'] or result['phase']=='STOPPING':
                waited=time.monotonic(); time.sleep(self.limits.poll_seconds)
                owner='wait-'+uuid.uuid4().hex
                st=self.s.manager_claim(goal_id,owner)
                try:
                    st['elapsed_seconds']+=time.monotonic()-waited
                    self.s.manager_save(goal_id,owner,st)
                finally: self.s.manager_release(goal_id,owner)
