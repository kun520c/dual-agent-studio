"""Owned offline process-crash fixture, never a production launch entry."""
from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT),str(ROOT/'desktop-validation/python'),str(ROOT/'desktop-validation/tests')]
from v05_support import install
from desktop_validation.task_api import TaskPolicy
from desktop_validation.task_host import TaskHost

base = Path(sys.argv[1])
install(Path(sys.argv[2]))
host = TaskHost(TaskPolicy(base/'projects',base/'runs',base/'policy.json'))
g = host.goals.create_goal(title='OFFLINE crash',original_goal='Persist intent and crash before submission',idempotency_key='goal')
p = host.goals.create_plan(g.goal_id,{'summary':'crash fixture','work_items':[dict(work_item_id='crash-A',title='crash-A',description='value=5',
    dependencies=[],acceptance_criteria=['value=5'],allowed_write_scope=['value.txt'])]},parent_revision=None,expected_revision=0,idempotency_key='plan')
host.goals.activate_plan(g.goal_id,1,expected_revision=1,idempotency_key='activate')


def crash_before_submit(*args,**kwargs):
    mapping = host.goals.list_dispatches(g.goal_id)[0]
    (base/'crash-proof.json').write_text(json.dumps(mapping),encoding='utf8')
    os._exit(77)


host.submit = crash_before_submit
host.goal_host.submit_work_item(json.dumps(dict(goalId=g.goal_id,workItemId='crash-A',projectId='offline-a',templateId='collaborate',
    executionMode='offline_cli',expectedRevision=2,idempotencyKey='crash-request')))
