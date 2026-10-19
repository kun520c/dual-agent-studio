"""Trusted Manager invocation service; model data never becomes process options.

ProductionManagerTransport is exercised with offline processes and injected
runners. Installed Codex/Cursor profiles remain closed until a no-tools
mechanism is confirmed; neither a mode string nor signed flags enable them.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
from dual_agent import PROJECT_ROOT
from dual_agent.budget import current_budget
from dual_agent.goal_ledger import ValidationError
from dual_agent.goal_ledger.schemas import digest, canonical
from dual_agent.execution_gate import ExecutionDenied
from dual_agent.winproc import run_controlled
from dual_agent.safety import redact_text
from .authorization import ManagerAuthority, DATA_SCOPE
from .models import bounded_json
from .provider import CallControl, ProviderError, ProviderTimeout, ProviderCancelled

OPERATIONS = frozenset({'generate_plan','review_result','propose_replan'})


class ProviderUncertain(ProviderError): pass


@dataclass(frozen=True)
class BackendProfile:
    name: str
    provider: str
    model: str
    mode: str
    protocol: str
    command: tuple[str,...]
    files: tuple[str,...]

    def summary(self):
        return dict(profile=self.name,provider=self.provider,model=self.model,mode=self.mode,
            protocol=self.protocol,command=list(self.command),tools=[],data_scope=list(DATA_SCOPE))


def backend_profile(name):
    # There is no arbitrary user-command/flags registry. New production
    # adapters require a source change and independent capability validation.
    if name=='offline-manager-v1':
        script=str(PROJECT_ROOT/'desktop-validation/tests/fake_manager_cli.py')
        exe=str(Path(sys.executable).resolve())
        return BackendProfile(name,'codex','OFFLINE_FIXTURE','offline_cli','manager-jsonl-v1',
            (exe,'-I','-B',script),(exe,script))
    if name in {'codex-manager','cursor-manager'}:
        raise ExecutionDenied('Manager CLI no-tools isolation unverified; profile disabled')
    raise ExecutionDenied('Unknown Manager backend profile')


def _safe_text(value):
    # Redaction protects known credential forms; arbitrary secrets in free
    # text cannot be detected perfectly. No filesystem/config/env is uploaded.
    value=redact_text(value)
    value=re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[REDACTED]',value)
    return re.sub(r'(?i)\b(?:bearer|api[_-]?key|access[_-]?token|password)\s*[:= ]\s*[^\s,;"}]+','[REDACTED]',value)


def outbound_context(context):
    if type(context) is not dict or set(context)!=set(DATA_SCOPE)|{'context_hash'}:
        raise ValidationError('Manager context compartments invalid')
    from .context import POLICY
    if context['system_policy']!=POLICY:raise ValidationError('Manager policy modified')
    raw=canonical({k:context[k] for k in DATA_SCOPE})
    if len(raw.encode('utf8'))>128000:raise ValidationError('Manager context too large')
    def scrub(v):
        if isinstance(v,str):return _safe_text(v)
        if isinstance(v,list):return [scrub(x) for x in v]
        if isinstance(v,dict):return {k:scrub(x) for k,x in v.items()}
        return v
    return scrub(json.loads(raw))


def parse_response(stdout,request_id,profile):
    """Closed versioned JSONL, no brace extraction or log-as-answer parsing.

    This protocol is the offline reference adapter's protocol, not a claim
    about any installed vendor's event schema. Unsupported events fail closed.
    """
    try: raw=stdout.decode('utf8') if isinstance(stdout,bytes) else stdout
    except UnicodeDecodeError:raise ValidationError('Manager response UTF-8 invalid') from None
    if len(raw)>192000:raise ValidationError('Manager response too large')
    final=None;usage={'source':'UNKNOWN'};session=None
    events=raw.splitlines()
    if not 1<=len(events)<=64:raise ValidationError('Manager event count invalid')
    for line in events:
        event=bounded_json(line)
        if type(event) is not dict:raise ValidationError('Manager event invalid')
        kind=event.get('type')
        if kind=='response':
            if final is not None or set(event)!={'type','request_id','model','body'}:raise ValidationError('Manager final envelope invalid')
            if event['request_id']!=request_id or event['model']!=profile.model:raise ValidationError('Manager response identity mismatch')
            final=bounded_json(event['body'])
            if type(final) is not dict:raise ValidationError('Manager final body must be object')
        elif kind=='usage':
            if usage['source']!='UNKNOWN' or set(event)!={'type','input_tokens','output_tokens','cached_tokens'}:raise ValidationError('Manager usage invalid')
            if any(type(event[k]) is not int or not 0<=event[k]<=10**12 for k in ('input_tokens','output_tokens','cached_tokens')):raise ValidationError('Manager usage invalid')
            if event['cached_tokens']>event['input_tokens']:raise ValidationError('Manager cached usage invalid')
            usage={'source':'adapter_event','model_name':profile.model,'provider_usage':{k:v for k,v in event.items() if k!='type'},
                   **{k:v for k,v in event.items() if k!='type'}}
        elif kind=='session':
            from dual_agent.goal_ledger.schemas import identifier
            if session is not None or set(event)!={'type','id'}:raise ValidationError('Manager session invalid')
            session=identifier(event['id'])
        else:raise ValidationError('Manager tool/log/unknown event denied')
    if final is None:raise ValidationError('Manager final response missing')
    return final,usage,session


class FakeManagerTransport:
    mode='fake_transport'
    def __init__(self,responses):self.responses=list(responses);self.calls=[]
    def request(self,request,control):
        control.check();self.calls.append(request)
        value=self.responses.pop(0)
        if isinstance(value,Exception):raise value
        result=value(request,control) if callable(value) else value
        control.check();return result


class ProductionManagerTransport:
    mode='production_transport'
    def __init__(self,goals,authority,profile_name,*,runner=None):
        self.goals=goals;self.profile=backend_profile(profile_name)
        if type(authority) is not ManagerAuthority:raise ExecutionDenied('Independent Manager authority required')
        if authority.runs!=goals.database_path.parents[2]:raise ExecutionDenied('Manager audit deployment mismatch')
        self.authority=authority;self.runner=runner or run_controlled

    def request(self,request,control):
        control.check()
        if set(request)!={'operation','model_config','context'} or request['operation'] not in OPERATIONS:
            raise ValidationError('Manager operation denied')
        if request['model_config']!={'model':self.profile.model,'profile':self.profile.name}:
            raise ValidationError('Manager model configuration denied')
        identity=control.identity
        if not identity or set(identity)!={'goal_id','manager_run_id','action_id','request_id','owner'}:
            raise ExecutionDenied('Trusted Manager action identity required')
        grant=self.authority.authorize(identity,self.profile)
        context=outbound_context(request['context'])
        if context['user_goal']['goal_id']!=identity['goal_id']:
            raise ExecutionDenied('Manager context goal mismatch')
        gid,owner,rid=identity['goal_id'],identity['owner'],identity['request_id']
        payload=dict(version=1,request_id=rid,role='manager',operation=request['operation'],
                     model=self.profile.model,tools=[],context=context)
        wire=canonical(payload).encode('utf8')
        if len(wire)>128000:raise ValidationError('Manager request envelope too large')
        metadata={k:v for k,v in identity.items() if k not in {'owner','goal_id'}}
        metadata.update(operation=request['operation'],provider=self.profile.provider,model=self.profile.model,
            role='manager',transport_mode=self.profile.mode,
            backend_profile=self.profile.name,authorization=self.authority.identity,config_hash=digest(self.profile.summary()),
            input_hash=digest(context),goal_revision=context['user_goal']['revision'])
        self.goals.manager_call_reserve(gid,owner,metadata,max_calls=grant['maxCalls'],expected_revision=context['user_goal']['revision'])
        # Budget is the original batch ContextVar. Signed persistent Manager
        # quota additionally survives controller recreation and host restart.
        budget=current_budget()
        if budget and not budget.try_consume(self.profile.provider):
            self.goals.manager_call_update(gid,owner,rid,'FAILED',failure_reason='batch_budget',billing='NOT_STARTED',processes_cleared=True)
            raise ProviderError('Manager batch call budget exhausted')
        reserved=bool(budget)
        attempted=False
        try:
            control.check();self.authority.authorize(identity,self.profile)
            self.goals.manager_call_update(gid,owner,rid,'STARTED')
            deadline=min(control.deadline,time.monotonic()+grant['timeoutSeconds'])
            bounded=CallControl(deadline,control.cancelled,identity)
            # A backend-owned empty input directory is not a filesystem sandbox.
            from dual_agent.goal_ledger.db import exact_local_path
            cwd=exact_local_path(self.authority.runs/'studio-host/manager-input')
            cwd.mkdir(parents=True,exist_ok=True)
            env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR'}}
            env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
            attempted=True
            capture=self.runner(self.profile.command,cwd=str(cwd),env=env,input_bytes=wire,
                control=bounded,startup_timeout=min(5,grant['timeoutSeconds']),stdout_limit=192000,stderr_limit=16000)
            reserved=False # Once invoked/uncertain never refund the original batch.
        except (ProviderCancelled,ProviderTimeout):
            if attempted:
                self.goals.manager_call_update(gid,owner,rid,'NEEDS_RECONCILIATION',failure_reason='runner_interruption_unconfirmed')
                raise ProviderUncertain('Manager interruption cleanup unconfirmed') from None
            if reserved:budget.release(self.profile.provider)
            self.goals.manager_call_update(gid,owner,rid,'CANCELLED',failure_reason='cancelled_before_start',billing='NOT_STARTED',processes_cleared=True)
            raise
        except Exception:
            if not attempted:
                if reserved:budget.release(self.profile.provider)
                self.goals.manager_call_update(gid,owner,rid,'FAILED',failure_reason='pre_start_denied',billing='NOT_STARTED',processes_cleared=True)
                raise ProviderError('Manager pre-start validation denied') from None
            self.goals.manager_call_update(gid,owner,rid,'NEEDS_RECONCILIATION',failure_reason='runner_confirmation_lost')
            raise ProviderUncertain('Manager process confirmation lost; no automatic replay') from None
        common=dict(exit_code=capture.exit_code,processes_cleared=capture.processes_cleared)
        if not capture.processes_cleared:
            self.goals.manager_call_update(gid,owner,rid,'NEEDS_RECONCILIATION',failure_reason='cleanup_unknown',**common)
            raise ProviderUncertain('Manager process cleanup unconfirmed')
        try:
            bounded.check()
            if capture.error_kind in {'timeout','startup_timeout'}:raise ProviderTimeout('Manager call timed out')
            if capture.error_kind=='cancelled':raise ProviderCancelled('Manager call cancelled')
            if capture.error_kind or capture.exit_code!=0:raise ProviderError('Manager process failed: '+(capture.error_kind or 'nonzero'))
            if self.goals.get_goal(gid).revision!=context['user_goal']['revision']:
                raise ProviderError('Manager response stale')
            body,usage,session=parse_response(capture.stdout,rid,self.profile)
            if _safe_text(canonical(body))!=canonical(body):
                raise ValidationError('Manager output contains credential patterns')
            # Reuse closed domain schemas before returning anything to Loop.
            if request['operation']=='review_result':
                from .decisions import validate_decision
                target=context['trusted_execution_facts']['target']
                from types import SimpleNamespace
                validate_decision(body,SimpleNamespace(**target))
            else:
                from dual_agent.goal_ledger.schemas import plan_proposal
                plan_proposal(body)
            bounded.check()
        except (ValueError,RuntimeError) as exc:
            state='CANCELLED' if isinstance(exc,ProviderCancelled) else 'TIMED_OUT' if isinstance(exc,ProviderTimeout) else 'FAILED'
            # Error class only: neither model output nor raw stderr enters audit.
            self.goals.manager_call_update(gid,owner,rid,state,failure_reason=type(exc).__name__,
                cancel_reason='user_or_pause' if state=='CANCELLED' else '',**common)
            raise ProviderError('Manager response rejected: '+type(exc).__name__) from None
        self.goals.manager_call_update(gid,owner,rid,'SUCCEEDED',result_hash=digest(body),usage=usage,
            session_id=session,billing='UNKNOWN' if self.profile.mode=='real' else 'OFFLINE_NO_CHARGE',**common)
        return body
