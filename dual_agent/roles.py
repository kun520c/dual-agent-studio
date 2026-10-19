"""协作角色。角色决定提示词和 CLI 的权限边界。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleSpec:
    key: str
    title: str
    instruction: str
    codex_sandbox: str
    cursor_mode: str | None


ROLES: dict[str, RoleSpec] = {
    "developer": RoleSpec(
        key="developer",
        title="开发者",
        instruction=(
            "你是开发者。只在任务给出的工作区和写入路径内分析和修改代码。"
            "不要提权，不要访问工作区外的文件，不要读取或输出密钥。"
        ),
        codex_sandbox="workspace-write",
        cursor_mode=None,
    ),
    "reviewer": RoleSpec(
        key="reviewer",
        title="审查者",
        instruction=(
            "你是审查者。只阅读并指出缺陷、回归风险和安全问题。"
            "不要修改文件，不要执行会改变系统状态的命令。"
        ),
        codex_sandbox="read-only",
        cursor_mode="ask",
    ),
    "diagnoser": RoleSpec(
        key="diagnoser",
        title="诊断者",
        instruction=(
            "你是诊断者。根据已有现象判断可能原因和下一步证据。"
            "不要修改文件，不要把猜测写成已经执行过的操作。"
        ),
        codex_sandbox="read-only",
        cursor_mode="ask",
    ),
    "acceptor": RoleSpec(
        key="acceptor",
        title="验收者",
        instruction=(
            "你是验收者。判断目标是否已经满足。"
            "协调器验证证据区中的退出码、输出、耗时和修改范围来自协调器自己执行的预定命令，不是其他 Agent 的声明。"
            "证据表明目标已经满足时，decision 使用 accept；证据缺失、未通过或与源码差异不符时，decision 使用 reject。"
            "不要因为自己没有亲自执行命令就改用 continue，也不要修改文件。"
        ),
        codex_sandbox="read-only",
        cursor_mode="ask",
    ),
}

ROLE_ALIASES = {
    "developer": "developer",
    "开发者": "developer",
    "reviewer": "reviewer",
    "审查者": "reviewer",
    "diagnoser": "diagnoser",
    "诊断者": "diagnoser",
    "acceptor": "acceptor",
    "验收者": "acceptor",
}

EXECUTOR_ALIASES = {
    "mock": "mock",
    "模拟": "mock",
    "codex": "codex",
    "cursor": "cursor",
}


def canonicalize_role(value: str) -> str:
    key = ROLE_ALIASES.get(value.strip())
    if key is None:
        known = "、".join(sorted(set(ROLE_ALIASES)))
        raise ValueError(f"未知角色：{value}。可用角色：{known}")
    return key


def canonicalize_executor(value: str) -> str:
    key = EXECUTOR_ALIASES.get(value.strip().lower()) or EXECUTOR_ALIASES.get(value.strip())
    if key is None:
        raise ValueError(f"未知执行器：{value}。可用执行器：mock、codex、cursor")
    return key
