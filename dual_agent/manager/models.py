"""Backend configuration is trusted; every provider response is untrusted JSON."""
from dataclasses import dataclass, asdict
from dual_agent.goal_ledger import ValidationError
from dual_agent.goal_ledger.schemas import identifier, integer, text, relative_path, canonical


@dataclass(frozen=True)
class Limits:
    max_tasks: int = 8
    max_decisions: int = 24
    max_repairs: int = 1
    max_failures: int = 3
    max_seconds: float = 120
    max_concurrent: int = 1
    provider_timeout: float = 10
    poll_seconds: float = .2

    def __post_init__(self):
        for key in ('max_tasks','max_decisions','max_failures','max_concurrent'):
            integer(getattr(self,key),1)
        integer(self.max_repairs)
        if self.max_tasks>100 or self.max_decisions>1000 or self.max_concurrent>2:
            raise ValidationError('Manager limits exceed supported task/Scheduler capacity')
        for value in (self.max_seconds,self.provider_timeout,self.poll_seconds):
            if type(value) not in (int,float) or not .05<=value<=86400:
                raise ValidationError('Invalid bounded Manager duration')
        if self.poll_seconds<.05 or self.poll_seconds>5: raise ValidationError('Invalid observation interval')

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class ProjectBinding:
    project_id: str
    template_id: str
    execution_mode: str
    allowed_scope: tuple[str,...]
    acceptance_capabilities: tuple[str,...]
    enforced_constraints: tuple[str,...] = ()
    description: str = 'Registered project'

    def __post_init__(self):
        identifier(self.project_id); identifier(self.template_id)
        if self.execution_mode not in {'mock','offline_cli','real'}: raise ValidationError('Unknown execution mode')
        if not self.allowed_scope or not self.acceptance_capabilities: raise ValidationError('Scope and acceptance capabilities required')
        for p in self.allowed_scope: relative_path(p)
        for p in self.acceptance_capabilities: text(p,500)
        for p in self.enforced_constraints: text(p,1000)
        text(self.description,4000)

    def to_dict(self): return asdict(self)


def bounded_json(raw):
    if isinstance(raw,str):
        if len(raw)>128000: raise ValidationError('Provider output exceeds size bound')
        import json
        def unique(pairs):
            data={}
            for key,value in pairs:
                if key in data: raise ValidationError('Duplicate JSON key')
                data[key]=value
            return data
        try: raw=json.loads(raw,object_pairs_hook=unique)
        except (ValueError,TypeError,RecursionError) as exc: raise ValidationError('Provider returned invalid JSON') from exc
    if len(canonical(raw))>128000: raise ValidationError('Provider output exceeds size bound')
    return raw
