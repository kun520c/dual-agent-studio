"""Offline no-tools reference protocol. No network, shell, file or MCP dispatcher.

This fixed simulator is hash pinned by a separate Manager test grant. It is
never substituted for an installed vendor CLI or evidence of a real model.
"""
import json
import sys


def main():
    raw=sys.stdin.buffer.read(128001)
    if len(raw)>128000:raise ValueError('input bound')
    request=json.loads(raw)
    if set(request)!={'version','request_id','role','operation','model','tools','context'}:
        raise ValueError('fields')
    if request['version']!=1 or request['tools']!=[] or request['role']!='manager':raise ValueError('no tools')
    context=request['context'];operation=request['operation']
    if operation in {'generate_plan','propose_replan'}:
        caps=context['system_capabilities']
        body=dict(summary='OFFLINE reference manager plan',work_items=[dict(work_item_id='OFFLINE_ONE',
            title='OFFLINE one task',description='Implement value.txt and pass the registered trusted command.',
            dependencies=[],acceptance_criteria=caps['acceptance_capabilities'],allowed_write_scope=caps['allowed_scope'])])
    elif operation=='review_result':
        facts=context['trusted_execution_facts'];t=facts['target']
        body=dict(action='ACCEPT' if facts['verification'].get('available') else 'BLOCK',
            reason='OFFLINE proposal from supplied execution facts',plan_revision=t['plan_revision'],
            work_item_id=t['work_item_id'],attempt_id=t['current_attempt_id'])
    else:raise ValueError('operation')
    print(json.dumps(dict(type='response',request_id=request['request_id'],model=request['model'],body=body),ensure_ascii=False))
    print(json.dumps(dict(type='session',id='offline-reference-session')))
    # Tokens deliberately absent: no actual model usage exists.


if __name__=='__main__':main()
