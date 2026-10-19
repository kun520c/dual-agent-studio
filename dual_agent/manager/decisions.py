"""Model decisions carry target identities, never evidence or authority objects."""
from dual_agent.goal_ledger import ValidationError, ConflictError
from dual_agent.goal_ledger.model import DecisionAction
from dual_agent.goal_ledger.schemas import object_fields, text, integer, identifier
from .models import bounded_json


def validate_decision(raw, target):
    value=object_fields(bounded_json(raw),{'action','reason','plan_revision','work_item_id','attempt_id'})
    try: action=DecisionAction(value['action']).value
    except (ValueError,TypeError) as exc: raise ValidationError('Unknown Manager decision') from exc
    text(value['reason'],10000); integer(value['plan_revision'],1)
    identifier(value['work_item_id']); identifier(value['attempt_id'])
    if (value['work_item_id'],value['attempt_id'],value['plan_revision'])!=(target.work_item_id,target.current_attempt_id,target.plan_revision):
        raise ConflictError('Manager decision is stale or targets another attempt/plan')
    return {**value,'action':action}
