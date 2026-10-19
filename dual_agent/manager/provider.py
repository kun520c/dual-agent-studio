"""Structured Manager protocol; process authority lives in the trusted service."""
from copy import deepcopy
from dataclasses import dataclass
import threading
import time
from typing import Protocol
from dual_agent.goal_ledger import ValidationError
from .models import bounded_json


class ProviderError(RuntimeError): pass
class ProviderTimeout(ProviderError): pass
class ProviderCancelled(ProviderError): pass


@dataclass(frozen=True)
class CallControl:
    deadline: float
    cancelled: object
    identity: dict | None = None

    def check(self):
        if self.cancelled(): raise ProviderCancelled('Manager call cancelled')
        if time.monotonic()>=self.deadline: raise ProviderTimeout('Manager call exceeded deadline')


class ManagerProvider(Protocol):
    mode: str
    def generate_plan(self, context: dict, control: CallControl): ...
    def review_result(self, context: dict, control: CallControl): ...
    def propose_replan(self, context: dict, control: CallControl): ...


class FakeManagerProvider:
    """Scripted alternatives + fact-based default; never grants verified."""
    mode='fake_manager'
    def __init__(self, plan, *, decisions=(), replans=(), delay=0):
        self.plan=deepcopy(plan); self.decisions=list(decisions); self.replans=list(replans)
        self.delay=delay; self.calls=[]; self._guard=threading.Lock()

    def _call(self, kind, context, control, value):
        with self._guard: self.calls.append(dict(kind=kind,goal_id=context['user_goal']['goal_id']))
        until=time.monotonic()+self.delay
        while time.monotonic()<until:
            control.check(); time.sleep(min(.01,max(0,until-time.monotonic())))
        control.check()
        if isinstance(value,Exception): raise value
        return deepcopy(value(context) if callable(value) else value)

    def generate_plan(self, context, control): return self._call('PLAN',context,control,self.plan)
    def propose_replan(self, context, control):
        return self._call('REPLAN',context,control,self.replans.pop(0) if self.replans else self.plan)
    def review_result(self, context, control):
        facts=context['trusted_execution_facts']; target=facts['target']
        default=dict(action='ACCEPT' if facts['verification'].get('available') else 'BLOCK',
            reason='Offline review of current trusted execution facts',plan_revision=target['plan_revision'],
            work_item_id=target['work_item_id'],attempt_id=target['current_attempt_id'])
        value=self.decisions.pop(0) if self.decisions else default
        return self._call('REVIEW',context,control,value)


class TransportManagerProvider:
    """Preserves fake_transport; production requires the concrete trusted service."""
    mode='fake_transport'
    def __init__(self, transport, *, model_config, transport_mode='fake_transport'):
        if transport_mode=='production_transport':
            from .transport import ProductionManagerTransport
            from .api_transport import APIManagerTransport
            if type(transport) not in {ProductionManagerTransport,APIManagerTransport}:
                raise ProviderError('Production Manager requires the signed invocation service')
            self.mode='production_transport'
        elif transport_mode!='fake_transport' or getattr(transport,'mode',None)!='fake_transport':
            raise ProviderError('Unknown/unreviewed Manager transport')
        if set(model_config)!={'model','profile'} or not all(isinstance(x,str) and 0<len(x)<100 for x in model_config.values()):
            raise ValidationError('Invalid backend model configuration')
        self.transport=transport; self.model_config=dict(model_config)

    def _call(self, kind, context, control):
        control.check()
        result=self.transport.request(dict(operation=kind,model_config=self.model_config,context=context),control)
        control.check()
        return bounded_json(result)
    def generate_plan(self, context, control): return self._call('generate_plan',context,control)
    def review_result(self, context, control): return self._call('review_result',context,control)
    def propose_replan(self, context, control): return self._call('propose_replan',context,control)
