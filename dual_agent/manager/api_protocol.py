"""Nonstream Responses protocol; provider-specific request allowlists."""
from types import SimpleNamespace
from dual_agent.goal_ledger import ValidationError
from dual_agent.goal_ledger.schemas import canonical
from .models import bounded_json
from .api_profiles import require_profile
from .context import POLICY


def object_schema(properties):
    return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)


def output_schema(operation):
    string={'type':'string'};strings={'type':'array','items':string}
    if operation=='review_result':
        return object_schema(dict(action={'type':'string','enum':['ACCEPT','REQUEST_REPAIR','REPLAN','BLOCK','ESCALATE']},
            reason=string,plan_revision={'type':'integer'},work_item_id=string,attempt_id=string))
    item=object_schema(dict(work_item_id=string,title=string,description=string,dependencies=strings,
        acceptance_criteria=strings,allowed_write_scope=strings))
    return object_schema(dict(summary=string,work_items={'type':'array','items':item}))


def build_request(profile,operation,context):
    require_profile(profile)
    if operation not in {'generate_plan','review_result','propose_replan'}:raise ValidationError('Unknown API operation')
    schema=output_schema(operation)
    instructions=POLICY+' Return one JSON object for '+operation+'. JSON schema: '+canonical(schema)
    result=dict(model=profile.model,instructions=instructions,input=canonical(context),stream=False,
        max_output_tokens=profile.max_output_tokens,tools=[],tool_choice='none',
        reasoning={'effort':profile.reasoning_effort},text={'format':{'type':profile.output_mode}})
    if profile.provider=='openai':
        result.update(store=False,background=False,truncation='disabled')
        result['text']['format'].update(name='manager_output',schema=schema,strict=True)
    # DeepSeek: never send ignored store/background/truncation/parallel flags.
    wire=canonical(result).encode('utf8')
    if len(wire)>profile.max_request_bytes:raise ValidationError('API request exceeds byte limit')
    return wire


def validate_output(body,operation,context,profile):
    if operation=='review_result':
        from .decisions import validate_decision
        return validate_decision(body,SimpleNamespace(**context['trusted_execution_facts']['target']))
    from dual_agent.goal_ledger.schemas import plan_proposal
    proposal=plan_proposal(body)
    if len(proposal['work_items'])>profile.max_work_items:
        raise ValidationError('API Manager is restricted to a single work item')
    return proposal


def parse_api_response(raw,profile):
    require_profile(profile)
    if not isinstance(raw,bytes) or len(raw)>profile.max_response_bytes:raise ValidationError('API response byte limit')
    try:response=bounded_json(raw.decode('utf8'))
    except (UnicodeError,ValueError):raise ValidationError('API response JSON invalid') from None
    if type(response) is not dict:raise ValidationError('API envelope invalid')
    # Unknown metadata is rejected as well: changes in vendor output require review.
    allowed={'id','object','created_at','status','error','incomplete_details','model','output','usage',
        'instructions','max_output_tokens','parallel_tool_calls','previous_response_id','reasoning','store',
        'temperature','text','tool_choice','tools','top_p','truncation','user','metadata','background',
        'service_tier','safety_identifier','prompt_cache_key','max_tool_calls','top_logprobs'}
    if set(response)-allowed:raise ValidationError('Unknown API envelope field')
    if (response.get('object')!='response' or response.get('status')!='completed' or response.get('error') is not None
        or response.get('incomplete_details') is not None or response.get('model')!=profile.model):
        raise ValidationError('API refusal, incomplete or model mismatch')
    if response.get('tools',[])!=[] or response.get('tool_choice','none')!='none':raise ValidationError('API tools denied')
    rid=response.get('id')
    if not isinstance(rid,str) or not 1<=len(rid)<=200 or any(ord(x)<33 or ord(x)>126 for x in rid):
        raise ValidationError('API response identity invalid')
    output=response.get('output')
    if not isinstance(output,list) or len(output)!=1:raise ValidationError('API requires one message; tools/reasoning denied')
    msg=output[0]
    if (type(msg) is not dict or set(msg)-{'id','type','role','status','content','phase'} or msg.get('type')!='message'
        or msg.get('role')!='assistant' or msg.get('status')!='completed'):
        raise ValidationError('API tool/unknown message denied')
    content=msg.get('content')
    if not isinstance(content,list) or len(content)!=1:raise ValidationError('API requires one output text')
    part=content[0]
    if (type(part) is not dict or set(part)-{'type','text','annotations','logprobs'} or part.get('type')!='output_text'
        or part.get('annotations',[]) or part.get('logprobs',[]) or not isinstance(part.get('text'),str)):
        raise ValidationError('API refusal/tool/annotation denied')
    body=bounded_json(part['text'])
    if type(body) is not dict:raise ValidationError('API output must be one object')
    usage={'source':'UNKNOWN','provider_response_id':rid,'provider_request_id':'UNKNOWN','provider_model':profile.model}
    values=response.get('usage')
    if values is not None:
        if type(values) is not dict or set(values)-{'input_tokens','output_tokens','total_tokens','input_tokens_details','output_tokens_details'}:
            raise ValidationError('API usage fields invalid')
        for name in ('input_tokens','output_tokens','total_tokens'):
            if type(values.get(name)) is not int or not 0<=values[name]<=10**12:raise ValidationError('API usage invalid')
        if values['total_tokens']!=values['input_tokens']+values['output_tokens']:raise ValidationError('API usage totals invalid')
        usage.update(source='provider_response',input_tokens=values['input_tokens'],output_tokens=values['output_tokens'])
        for field,key,total in [('input_tokens_details','cached_tokens','input_tokens'),('output_tokens_details','reasoning_tokens','output_tokens')]:
            details=values.get(field)
            if details is not None:
                if type(details) is not dict or set(details)-({key,'cache_write_tokens'} if field=='input_tokens_details' else {key}):
                    raise ValidationError('API usage details invalid')
                for detail,value in details.items():
                    if type(value) is not int or not 0<=value<=values[total]:raise ValidationError('API usage detail invalid')
                    usage[detail]=value
    return body,usage
