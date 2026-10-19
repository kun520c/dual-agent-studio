"""任务状态。旧运行记录没有 status 字段时，未知状态不得当成成功。"""

from __future__ import annotations

import json
import threading
import os
import uuid
import time
from pathlib import Path
from typing import Any

from dual_agent.models import dump_json, now_iso

TASK_STATES = {
    "queued",
    "running",
    "verifying",
    "reviewing",
    "repairing",
    "verified",
    "failed",
    "cancelled",
}
_TERMINAL = {"verified", "failed", "cancelled"}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def derive_terminal_status(stop_reason: str | None) -> str:
    if stop_reason == "verified":
        return "verified"
    if stop_reason == "cancelled":
        return "cancelled"
    return "failed"


def status_from_result_dict(data: dict[str, Any]) -> str:
    """读取历史 result.json。缺少或无法识别的状态一律失败，不能因为 success 字段单独算通过。"""
    explicit = data.get("status")
    stop = data.get("stop_reason")
    if isinstance(explicit, str) and explicit in TASK_STATES:
        if explicit == "verified" and stop != "verified":
            return "failed"
        return explicit
    if stop == "verified" and data.get("success") is True:
        return "verified"
    if stop == "cancelled":
        return "cancelled"
    return "failed"


def is_terminal(state: str) -> bool:
    return state in _TERMINAL


def _file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def read_status(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_status(path: Path, **fields: object) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        current = read_status(path)
        for key, value in fields.items():
            current[key] = value
        state = current.get("state")
        if not isinstance(state, str) or state not in TASK_STATES:
            current["state"] = "failed"
        current["updated_at"] = now_iso()
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            dump_json(temporary,current)
            # Windows readers may briefly hold a handle without delete sharing.
            # Keep atomic status publication, retry only this bounded sharing
            # violation; never fall back to a partially written target file.
            for attempt in range(6):
                try:
                    os.replace(temporary,path)
                    break
                except PermissionError:
                    if attempt==5:raise
                    time.sleep(0.01*(attempt+1))
        finally:
            temporary.unlink(missing_ok=True)
        return current
