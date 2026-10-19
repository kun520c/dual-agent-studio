"""Signed API-only Manager invocation on the existing durable call ledger."""
import hashlib
import time
from dual_agent.execution_gate import ExecutionDenied
from dual_agent.goal_ledger import ValidationError
from dual_agent.goal_ledger.schemas import digest, canonical
from .authorization import DATA_SCOPE
from .api_authorization import APIManagerAuthority
from .api_profiles import api_profile, require_profile
from .api_protocol import build_request, parse_api_response, validate_output
from .api_http import OfficialHTTPSClient, HTTPFailure, HTTPResult
from .provider import CallControl, ProviderError, ProviderCancelled, ProviderTimeout
from .transport import OPERATIONS, outbound_context, _safe_text, ProviderUncertain


class APIManagerTransport:
    mode='production_transport'
    def __init__(self,goals,authority,profile_name,*,http_client=None,credential=None):
        self.goals=goals;self.profile=api_profile(profile_name)
        if type(authority) is not APIManagerAuthority or authority.profile!=self.profile:
            raise ExecutionDenied('Independent API Manager authority required')
        if authority.runs!=goals.database_path.parents[2]:raise ExecutionDenied('API audit deployment mismatch')
        self.authority=authority;self.http_client=http_client if http_client is not None else OfficialHTTPSClient()
        # Credential is supplied by a future trusted deployment, never discovered
        # from env/files/project configuration. It is neither hashed nor persisted.
        self._credential=credential

    def request(self,request,control):
        control.check();require_profile(self.profile)
        if set(request)!={'operation','model_config','context'} or request['operation'] not in OPERATIONS:
            raise ValidationError('API operation denied')
        if request['model_config']!={'model':self.profile.model,'profile':self.profile.name}:
            raise ValidationError('API model configuration denied')
        identity=control.identity
        if not identity or set(identity)!={'goal_id','manager_run_id','action_id','request_id','owner'}:
            raise ExecutionDenied('Trusted API Manager action identity required')
        grant=self.authority.authorize(identity,self.profile)
        official=type(self.http_client) is OfficialHTTPSClient
        if official and (grant['networkPolicy']!='official_https' or self._credential is None):
            raise ExecutionDenied('API network disabled; explicit production grant and credential required')
        if not official and grant['networkPolicy']!='offline_only':
            raise ExecutionDenied('Injected HTTP client requires isolated offline grant')
        context=outbound_context(request['context'])
        if context['user_goal']['goal_id']!=identity['goal_id']:raise ExecutionDenied('API context goal mismatch')
        wire=build_request(self.profile,request['operation'],context)
        gid,owner,rid=identity['goal_id'],identity['owner'],identity['request_id']
        profile_hash=digest(self.profile.summary())
        metadata={k:v for k,v in identity.items() if k not in {'owner','goal_id'}}
        metadata.update(operation=request['operation'],provider=self.profile.provider,model=self.profile.model,
            backend_kind='api',profile_hash=profile_hash,authorization_ref=self.authority.identity,
            role='manager',transport_mode=self.profile.mode,backend_profile=self.profile.name,
            authorization=self.authority.identity,config_hash=profile_hash,input_hash=digest(context),
            request_hash=hashlib.sha256(wire).hexdigest(),goal_revision=context['user_goal']['revision'],
            start_intent=True,confirmed_result=False,usage_source='UNKNOWN',billing_state='UNKNOWN',
            failure_kind='',cancellation=False,outcome='INTENT',network_policy=grant['networkPolicy'])
        self.goals.manager_call_reserve(gid,owner,metadata,max_calls=grant['maxCalls'],expected_revision=context['user_goal']['revision'])
        bounded=CallControl(min(control.deadline,time.monotonic()+grant['timeoutSeconds'],
            time.monotonic()+self.profile.timeout_seconds,time.monotonic()+max(0,grant['expires']-time.time())),control.cancelled,identity)
        attempted=False
        def update(state,**kw):
            kw.setdefault('outcome',state)
            return self.goals.manager_call_update(gid,owner,rid,state,**kw)
        try:
            bounded.check();self.authority.authorize(identity,self.profile)
            update('STARTED')
            attempted=True
            result=self.http_client.exchange(self.profile,wire,self._credential,bounded)
        except HTTPFailure as exc:
            uncertain=exc.sent
            state='NEEDS_RECONCILIATION' if uncertain else 'CANCELLED' if exc.kind=='cancelled' else 'TIMED_OUT' if exc.kind=='timeout' else 'FAILED'
            update(state,failure_kind=exc.kind,failure_reason=exc.kind,cancellation=exc.kind=='cancelled',
                cancel_reason='user_or_pause' if exc.kind=='cancelled' else '',
                billing='UNKNOWN' if uncertain else 'NOT_SENT',billing_state='UNKNOWN' if uncertain else 'NOT_SENT')
            raise (ProviderUncertain if uncertain else ProviderError)('API '+exc.kind+'; no automatic retry') from None
        except (ProviderCancelled,ProviderTimeout) as exc:
            state='NEEDS_RECONCILIATION' if attempted else 'CANCELLED' if isinstance(exc,ProviderCancelled) else 'TIMED_OUT'
            update(state,failure_kind=type(exc).__name__,cancellation=isinstance(exc,ProviderCancelled),
                billing='UNKNOWN' if attempted else 'NOT_SENT',billing_state='UNKNOWN' if attempted else 'NOT_SENT')
            raise (ProviderUncertain if attempted else ProviderError)('API interrupted; no automatic retry') from None
        except Exception:
            update('NEEDS_RECONCILIATION' if attempted else 'FAILED',failure_kind='confirmation_lost',failure_reason='confirmation_lost')
            raise ProviderUncertain('API confirmation lost; no automatic retry') from None
        try:
            bounded.check()
            if type(result) is not HTTPResult:raise ValidationError('HTTP result type invalid')
            if result.status!=200:
                # Redirects, 429 and server failures are final; no fallback/retry.
                raise ValidationError('HTTP status denied')
            if self.goals.get_goal(gid).revision!=context['user_goal']['revision']:raise ProviderError('API response stale')
            body,usage=parse_api_response(result.body,self.profile)
            if result.request_id is not None:
                if not isinstance(result.request_id,str) or not 1<=len(result.request_id)<=200 or any(ord(c)<33 or ord(c)>126 for c in result.request_id):
                    raise ValidationError('Provider request id invalid')
                usage['provider_request_id']=result.request_id
            if _safe_text(canonical(usage))!=canonical(usage) or (self._credential and self._credential in canonical(usage)):
                raise ValidationError('API metadata contains credential patterns')
            if not official and usage['source']=='provider_response':usage['source']='offline_fixture_response'
            if _safe_text(canonical(body))!=canonical(body) or (self._credential and self._credential in canonical(body)):
                raise ValidationError('API output contains credential patterns')
            validate_output(body,request['operation'],context,self.profile)
            bounded.check();self.authority.authorize(identity,self.profile)
        except (ValueError,RuntimeError) as exc:
            interrupted=isinstance(exc,(ProviderCancelled,ProviderTimeout))
            state='NEEDS_RECONCILIATION' if interrupted else 'FAILED'
            update(state,confirmed_result=True,failure_kind=type(exc).__name__,failure_reason=type(exc).__name__,
                http_status=result.status if type(result) is HTTPResult else None,
                cancellation=isinstance(exc,ProviderCancelled),
                cancel_reason='user_or_pause' if isinstance(exc,ProviderCancelled) else '')
            raise (ProviderUncertain if interrupted else ProviderError)('API response rejected: '+type(exc).__name__) from None
        billing='UNKNOWN' if official else 'OFFLINE_NO_CHARGE'
        update('SUCCEEDED',confirmed_result=True,result_hash=digest(body),usage=usage,usage_source=usage['source'],
            billing=billing,billing_state=billing,http_status=result.status,response_hash=hashlib.sha256(result.body).hexdigest())
        return body
