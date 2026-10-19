"""Separate Manager purpose using the deployment-pinned RSA trust root.

No signer installation, key loading, GUI approval or grant generation API.
Executor version-1 grants and Manager version-2 grants are not interchangeable.
"""
import base64
import hashlib
import json
import math
import time
from pathlib import Path
from dual_agent import execution_gate as gate
from dual_agent.goal_ledger.schemas import identifier, digest

DATA_SCOPE = ('system_policy','user_goal','system_capabilities','project_materials',
              'trusted_execution_facts','active_plan','untrusted_model_suggestions')


class ManagerAuthority:
    def __init__(self,projects,runs,project_id,goal_id,profile):
        self.projects=gate.exact_path(str(projects));self.runs=gate.exact_path(str(runs))
        self.project_id=identifier(project_id);self.goal_id=identifier(goal_id)
        self.profile=profile
        self.file=self.runs/'studio-manager-authorizations'/(goal_id+'.json')
        self.payload=self.reload()
        self.identity=hashlib.sha256(gate.canonical(self.payload)).hexdigest()

    def reload(self):
        try:
            gate.exact_path(str(self.file))
            if self.file.stat().st_size>65536:raise ValueError()
            from .models import bounded_json
            envelope=bounded_json(self.file.read_text(encoding='utf8'))
            if set(envelope)!={'payload','signature'}:raise ValueError()
            p=envelope['payload'];key=gate.TRUSTED_SIGNERS.get(p['signer'])
            fields={'version','purpose','signer','mode','projectId','goalId','projects','runs',
                'projectSha256','profileSha256','files','expires','maxCalls','timeoutSeconds','dataScope'}
            if set(p)!=fields or not key or self.profile.mode not in key['modes']:raise ValueError()
            if not gate.verify_signature(p,base64.b64decode(envelope['signature'],validate=True),key):raise ValueError()
            if (p['version'],p['purpose'],p['mode'],p['projectId'],p['goalId'])!=(2,'manager_call',self.profile.mode,self.project_id,self.goal_id):raise ValueError()
            if gate.exact_path(p['projects'])!=self.projects or gate.exact_path(p['runs'])!=self.runs:raise ValueError()
            if p['profileSha256']!=digest(self.profile.summary()):raise ValueError()
            if p['dataScope']!=list(DATA_SCOPE):raise ValueError()
            for name in ('expires','timeoutSeconds'):
                if type(p[name]) not in {int,float} or not math.isfinite(p[name]):raise ValueError()
            if p['expires']<=time.time() or not .05<=p['timeoutSeconds']<=600:raise ValueError()
            if type(p['maxCalls']) is not int or not 1<=p['maxCalls']<=1000:raise ValueError()
            project=self.projects/(self.project_id+'.json')
            if gate.digest(gate.exact_path(str(project)))!=p['projectSha256']:raise ValueError()
            # Fixed executable and adapter are all hash pinned. Signed data
            # cannot introduce a new command, parameter, tool or environment.
            required=[str(gate.exact_path(token)) for token in self.profile.files]
            if set(p['files'])!=set(required):raise ValueError()
            for path in required:
                if gate.digest(gate.exact_path(path))!=p['files'][path]:raise ValueError()
            if hasattr(self,'identity') and hashlib.sha256(gate.canonical(p)).hexdigest()!=self.identity:raise ValueError()
            return p
        except (OSError,ValueError,KeyError,TypeError,OverflowError,RecursionError):
            raise gate.ExecutionDenied('Manager signature, purpose, deployment, profile or budget denied') from None

    def authorize(self,identity,profile):
        if profile!=self.profile or identity['goal_id']!=self.goal_id:
            raise gate.ExecutionDenied('Manager grant binding denied')
        return self.reload()
