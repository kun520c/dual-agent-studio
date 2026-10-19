"""Manager v3 API grant. No executable hash fiction or production key loader."""
import base64
import hashlib
import math
import time
from dual_agent import execution_gate as gate
from dual_agent.goal_ledger.schemas import identifier, digest
from .models import bounded_json
from .api_profiles import require_profile


class APIManagerAuthority:
    def __init__(self,projects,runs,project_id,goal_id,profile):
        self.projects=gate.exact_path(str(projects));self.runs=gate.exact_path(str(runs))
        self.project_id=identifier(project_id);self.goal_id=identifier(goal_id)
        self.profile=require_profile(profile)
        self.file=self.runs/'studio-manager-authorizations'/(goal_id+'.json')
        self.payload=self.reload()
        self.identity=hashlib.sha256(gate.canonical(self.payload)).hexdigest()

    def reload(self):
        try:
            require_profile(self.profile);gate.exact_path(str(self.file))
            if self.file.stat().st_size>65536:raise ValueError()
            envelope=bounded_json(self.file.read_text(encoding='utf8'))
            if set(envelope)!={'payload','signature'}:raise ValueError()
            p=envelope['payload'];key=gate.TRUSTED_SIGNERS.get(p['signer'])
            fields={'version','purpose','signer','mode','backendKind','provider','endpoint','model','protocol',
                'projectId','goalId','projects','runs','projectSha256','profileSha256','expires',
                'maxCalls','timeoutSeconds','dataScope','networkPolicy'}
            if set(p)!=fields or not key or 'manager_api' not in key['modes']:raise ValueError()
            if not gate.verify_signature(p,base64.b64decode(envelope['signature'],validate=True),key):raise ValueError()
            expected=(3,'manager_call','manager_api','api',self.profile.provider,self.profile.endpoint,
                      self.profile.model,self.profile.protocol,self.project_id,self.goal_id)
            if tuple(p[k] for k in ('version','purpose','mode','backendKind','provider','endpoint','model',
                                    'protocol','projectId','goalId'))!=expected:raise ValueError()
            if gate.exact_path(p['projects'])!=self.projects or gate.exact_path(p['runs'])!=self.runs:raise ValueError()
            if p['profileSha256']!=digest(self.profile.summary()) or p['dataScope']!=list(self.profile.data_scope):raise ValueError()
            if p['networkPolicy'] not in {'offline_only','official_https'}:raise ValueError()
            for k in ('expires','timeoutSeconds'):
                if type(p[k]) not in {int,float} or not math.isfinite(p[k]):raise ValueError()
            if p['expires']<=time.time() or not .05<=p['timeoutSeconds']<=self.profile.timeout_seconds:raise ValueError()
            if type(p['maxCalls']) is not int or not 1<=p['maxCalls']<=1000:raise ValueError()
            if gate.digest(gate.exact_path(str(self.projects/(self.project_id+'.json'))))!=p['projectSha256']:raise ValueError()
            if hasattr(self,'identity') and hashlib.sha256(gate.canonical(p)).hexdigest()!=self.identity:raise ValueError()
            return p
        except (OSError,ValueError,KeyError,TypeError,OverflowError,RecursionError):
            raise gate.ExecutionDenied('API Manager signature, backend, deployment, profile or budget denied') from None

    def authorize(self,identity,profile):
        if profile!=self.profile or identity['goal_id']!=self.goal_id:
            raise gate.ExecutionDenied('API Manager grant binding denied')
        return self.reload()
