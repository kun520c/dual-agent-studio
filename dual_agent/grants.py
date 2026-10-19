"""精确到一条规范化绝对路径的只读授权。

比较的是路径本身，不是目录名。复制出来的目录、改过名字的目录、Junction
和符号链接都不能获得这条授权。授权只覆盖只读角色，并且 write_paths 必须为空。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dual_agent.roles import canonicalize_role
from dual_agent.safety import PathRejected, declared_path, finalize_declared, is_reparse_point, normalize_path

# Public source carries no author-specific access grants.
READONLY_GRANT_PATHS: tuple[str, ...] = ()

READONLY_ROLES = frozenset({"reviewer", "diagnoser", "acceptor"})

# 这些词出现在其他路径或任务文本里时继续阻断。授权路径本身也含有 ai-interview，
# 只有字面路径与最终路径都精确相等、且任务满足只读约束时才例外。
FORBIDDEN_MARKERS: tuple[str, ...] = ("模拟面试", "修图", "ai面试", "ai-interview")

SKIP_DIR_NAMES = frozenset({".git", "node_modules", "target", "dist", ".idea", ".gradle"})

_SECRET_SUFFIXES = frozenset({".pem", ".p12", ".key", ".jks", ".pfx", ".kdbx"})


def forbidden_text(text: str) -> bool:
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in FORBIDDEN_MARKERS)


def grant_keys() -> set[str]:
    return {normalize_path(Path(item)) for item in READONLY_GRANT_PATHS}


def lexical_key(raw: str) -> str:
    """只规范化字面路径，不跟随重解析点。"""
    declared = declared_path(raw)
    return normalize_path(Path(os.path.abspath(os.path.normpath(str(declared)))))


def same_grant_path(raw: str) -> bool:
    """字面路径正好是授权路径，且最终路径没有被 Junction 或符号链接改写。"""
    try:
        declared = declared_path(raw)
        key = lexical_key(raw)
    except PathRejected:
        return False
    if key not in grant_keys():
        return False
    if declared.exists() and is_reparse_point(declared):
        return False
    try:
        final = finalize_declared(raw)
    except PathRejected:
        return False
    return normalize_path(final) == key


def targets_grant(raw: str) -> bool:
    """最终落点是授权目录或其内部，但调用方给出的并不是那条授权路径。"""
    try:
        if same_grant_path(raw):
            return False
        key = lexical_key(raw)
    except PathRejected:
        return False
    if key in grant_keys():
        return True
    if forbidden_text(raw):
        return False
    try:
        final = finalize_declared(raw)
    except PathRejected:
        return False
    final_key = normalize_path(final)
    for grant in grant_keys():
        grant_key = grant.rstrip("\\/")
        if final_key == grant_key or final_key.startswith(grant_key + os.sep):
            return True
    return False


def is_secret_filename(name: str) -> bool:
    """可能含凭据的文件名。调用方不得读取这些文件的内容。"""
    folded = name.casefold()
    if folded == ".env" or folded.startswith(".env."):
        return True
    if folded.startswith("application") and folded.endswith((".yml", ".yaml")):
        return True
    if Path(folded).suffix in _SECRET_SUFFIXES:
        return True
    return any(token in folded for token in ("credential", "secret", "password"))


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    grant: bool
    detail: str


def _roles_of(task: dict | None) -> list[str] | None:
    if not task or task.get("participants") is None:
        return []
    participants = task.get("participants")
    if not isinstance(participants, list):
        return None
    roles: list[str] = []
    for item in participants:
        if not isinstance(item, dict):
            return None
        raw = item.get("role")
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            roles.append(canonicalize_role(raw))
        except ValueError:
            return None
    return roles


def _write_paths_of(task: dict | None) -> list | None:
    if not task or task.get("write_paths") is None:
        return []
    value = task.get("write_paths")
    if not isinstance(value, list):
        return None
    return value


def _worktree_enabled(task: dict | None) -> bool:
    if not task:
        return False
    worktree = task.get("worktree")
    if not worktree:
        return False
    if isinstance(worktree, dict):
        return bool(worktree.get("enabled"))
    return bool(getattr(worktree, "enabled", False))


def grant_constraints(task: dict | None) -> tuple[bool, str]:
    """没有任务配置时，只允许做只读检查，不表示可以启动开发者。"""
    if task is None:
        return True, "精确路径只读授权有效。没有任务配置，因此没有授权开发者、写入路径或 Worktree。"
    writes = _write_paths_of(task)
    if writes is None:
        return False, "write_paths 必须是数组，并且在这条只读授权下必须为空。"
    if writes:
        return False, "这条路径只授权只读试点，write_paths 必须为空。"
    roles = _roles_of(task)
    if roles is None:
        return False, "只读授权无法识别参与者角色。"
    if any(role not in READONLY_ROLES for role in roles):
        return False, "这条路径只允许审查者、诊断者和验收者。真实开发者继续禁止。"
    if _worktree_enabled(task):
        return False, "这条路径的只读授权禁止创建 Worktree。"
    if not roles:
        return True, "精确路径只读授权有效。任务没有声明参与者，不能据此启动开发者。"
    return (
        True,
        "精确路径只读授权有效：角色只读，write_paths 为空，不创建 Worktree。"
        "真实 Codex 只能使用 read-only 沙箱。真实 Cursor 开发者仍然禁止。",
    )


def _task_blob_forbidden(task: dict) -> bool:
    return forbidden_text(" ".join(_walk(task)))


def _walk(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_walk(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item))
    return found


def assess(workspace: str, task: dict | None = None) -> AccessDecision:
    """决定这条路径能不能被检查或被 Coordinator.run 使用。拒绝时不搜索目录。"""
    blocked = AccessDecision(
        False,
        False,
        "路径或配置指向不允许接入的正式项目。检查器没有搜索、列举或读取该目录。",
    )
    try:
        key = lexical_key(workspace)
    except PathRejected as exc:
        if forbidden_text(workspace) or (task is not None and _task_blob_forbidden(task)):
            return blocked
        return AccessDecision(False, False, str(exc))

    if key in grant_keys():
        if not same_grant_path(workspace):
            return AccessDecision(
                False,
                False,
                "授权路径被重解析点改写，或最终目录与声明的绝对路径不一致。"
                "不能把 Junction、符号链接或复制目录当成已授权项目。没有搜索该目录。",
            )
        ok, detail = grant_constraints(task)
        if not ok:
            return AccessDecision(False, False, detail + "没有搜索该目录。")
        return AccessDecision(True, True, detail)

    if forbidden_text(workspace) or (task is not None and _task_blob_forbidden(task)):
        return blocked
    if targets_grant(workspace):
        return AccessDecision(
            False,
            False,
            "该路径最终指向已授权目录，但本身不是被授权的规范化绝对路径。"
            "复制、重命名或 Junction 不能获得授权。没有搜索目标目录中的文件。",
        )
    return AccessDecision(True, False, "")


def reject_registration(raw: str) -> None:
    """只读授权不能通过项目注册变成可写项目。"""
    from dual_agent.models import ConfigError

    if forbidden_text(raw):
        raise ConfigError(f"路径指向不允许注册的项目：{raw}")
    if targets_grant(raw):
        raise ConfigError(f"该路径不能注册为项目。只读授权不会打开写入：{raw}")
