"""Source-owned API profiles. No URL/model registry is exposed to project data."""
from dataclasses import dataclass, asdict
from dual_agent.execution_gate import ExecutionDenied
from .authorization import DATA_SCOPE


@dataclass(frozen=True)
class APIProfile:
    name: str
    provider: str
    model: str
    endpoint: str
    protocol: str = 'responses-v1'
    mode: str = 'manager_api'
    backend_kind: str = 'api'
    data_scope: tuple = DATA_SCOPE
    max_request_bytes: int = 128000
    max_response_bytes: int = 128000
    timeout_seconds: float = 30
    max_output_tokens: int = 4096
    max_work_items: int = 1
    output_mode: str = 'json_schema'
    reasoning_effort: str = 'none'
    tool_policy: str = 'none'
    authorization_purpose: str = 'manager_call'

    def summary(self):
        result=asdict(self);result['data_scope']=list(self.data_scope)
        return result


def api_profile(name):
    if name=='openai-responses-manager-v1':
        return APIProfile(name,'openai','gpt-5.4-mini-2026-03-17','https://api.openai.com/v1/responses')
    if name=='deepseek-responses-manager-v1':
        return APIProfile(name,'deepseek','deepseek-flash','https://api.deepseek.com/responses',output_mode='json_object')
    raise ExecutionDenied('Unknown API Manager profile')


def require_profile(profile):
    if type(profile) is not APIProfile or profile!=api_profile(profile.name):
        raise ExecutionDenied('API profile differs from trusted deployment source')
    return profile
