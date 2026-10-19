"""批次级真实模型调用额度。失败的已发起请求也计数，拒绝发起的请求不计数。"""

from __future__ import annotations

import threading
from contextvars import ContextVar


class CallBudget:
    def __init__(self, codex_limit: int | None, cursor_limit: int | None) -> None:
        self.codex_limit = codex_limit
        self.cursor_limit = cursor_limit
        self.codex_used = 0
        self.cursor_used = 0
        self._lock = threading.Lock()

    def try_consume(self, kind: str) -> bool:
        if kind not in {"codex", "cursor"}:
            raise ValueError(f"未知额度类型：{kind}")
        with self._lock:
            used = self.codex_used if kind == "codex" else self.cursor_used
            limit = self.codex_limit if kind == "codex" else self.cursor_limit
            if limit is not None and used >= limit:
                return False
            if kind == "codex":
                self.codex_used += 1
            else:
                self.cursor_used += 1
            return True

    def release(self, kind: str) -> None:
        """Return a reservation when the process was never started."""
        if kind not in {"codex", "cursor"}:
            raise ValueError(f"未知额度类型：{kind}")
        with self._lock:
            if kind == "codex" and self.codex_used:
                self.codex_used -= 1
            elif kind == "cursor" and self.cursor_used:
                self.cursor_used -= 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"codex": self.codex_used, "cursor": self.cursor_used}


_BUDGET: ContextVar[CallBudget | None] = ContextVar("dual_agent_call_budget", default=None)


def push_budget(budget: CallBudget | None):
    return _BUDGET.set(budget)


def reset_budget(token) -> None:
    _BUDGET.reset(token)


def current_budget() -> CallBudget | None:
    return _BUDGET.get()
