"""Closed Phase A plan schema plus trusted project/constraint/capability bounds."""
from dual_agent.goal_ledger import ValidationError
from dual_agent.goal_ledger.schemas import plan_proposal, digest
from dual_agent.goal_host.inheritance import covered
from .models import bounded_json


def validate_plan(raw, *, goal, binding, limits, parent_revision):
    proposal=plan_proposal(bounded_json(raw))
    if len(proposal['work_items'])>limits.max_tasks: raise ValidationError('Plan exceeds Manager task budget')
    if not set(goal.constraints)<=set(binding.enforced_constraints):
        raise ValidationError('User constraints lack a trusted enforcement binding')
    mapping={w['work_item_id']:'mw-'+digest([goal.goal_id,parent_revision,w['work_item_id']])[:48] for w in proposal['work_items']}
    for w in proposal['work_items']:
        if not w['allowed_write_scope'] or not all(covered(p,binding.allowed_scope) for p in w['allowed_write_scope']):
            raise ValidationError('Plan write scope exceeds trusted project boundary')
        if not set(w['acceptance_criteria'])<=set(binding.acceptance_capabilities):
            raise ValidationError('Acceptance criteria have no registered executable capability')
        w['work_item_id']=mapping[w['work_item_id']]
        w['dependencies']=[mapping[x] for x in w['dependencies']]
    return proposal
