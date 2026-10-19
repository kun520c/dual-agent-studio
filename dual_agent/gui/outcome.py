"""把协调器的 stop_reason 显示成中文。

result.status 仍由 derive_terminal_status 生成：flow_complete 在状态字段里是 failed。
界面以 stop_reason 区分流程结束和执行失败，不另建状态机，也不把未验收显示成通过。
"""

from __future__ import annotations

from typing import Any

_READONLY_ROLES = {"reviewer", "diagnoser", "acceptor"}
_ACTIVE = {
    "queued": "排队",
    "running": "运行",
    "verifying": "验证",
    "reviewing": "审查",
    "repairing": "修复",
}

VERIFIED_LABEL = "可信验证通过"
FLOW_LABEL = "流程正常结束，但不代表代码功能已验证"
REVIEW_LABEL = "审查完成，未进行功能验收"
FAILED_LABEL = "执行或验证发生失败"
CANCELLED_LABEL = "用户取消"


def is_readonly_review(task: dict[str, Any] | None) -> bool:
    if not task:
        return False
    participants = {
        str(item.get("id")): item for item in task.get("participants") or [] if isinstance(item, dict)
    }
    flow = task.get("flow") or list(participants)
    if not isinstance(flow, list) or not flow:
        return False
    roles: list[str] = []
    for step in flow:
        item = participants.get(str(step)) or {}
        role = str(item.get("role") or "")
        if not role:
            return False
        roles.append(role)
    return all(role in _READONLY_ROLES for role in roles)


def presentation(
    state: str,
    stop_reason: str = "",
    *,
    success: object = None,
    task: dict[str, Any] | None = None,
) -> dict[str, object]:
    stop = stop_reason or ""
    if stop == "verified":
        if success is True:
            return {"kind": "verified", "label": VERIFIED_LABEL, "passed": True}
        return {"kind": "failed", "label": FAILED_LABEL, "passed": False}
    if stop == "cancelled" or state == "cancelled":
        return {"kind": "cancelled", "label": CANCELLED_LABEL, "passed": False}
    if stop == "flow_complete":
        label = REVIEW_LABEL if is_readonly_review(task) else FLOW_LABEL
        return {"kind": "flow_complete", "label": label, "passed": False}
    if state in _ACTIVE and stop not in {"verified", "flow_complete", "cancelled"}:
        return {"kind": state, "label": _ACTIVE[state], "passed": False}
    if state == "verified" and success is True and stop in {"", "verified"}:
        return {"kind": "verified", "label": VERIFIED_LABEL, "passed": True}
    return {"kind": "failed", "label": FAILED_LABEL, "passed": False}
