"""可选 Git Worktree。

第六阶段工作方式：从指定提交创建任务分支和独立目录，Agent 只在该目录中执行。
Worktree 不是操作系统沙箱。路径、Junction、符号链接和写入范围仍走原有检查。
创建和读取 diff 时短暂占用 git-metadata 资源锁；不同 Worktree 的任务随后可以并行。
不 merge、不 push、不删除原始分支，也不使用 --force 覆盖已有路径。
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

from dual_agent import PROJECT_ROOT
from dual_agent.locks import ResourceLockManager, default_resource_lock_dir
from dual_agent.models import TaskConfig, VerificationCheck, dump_json
from dual_agent.safety import (
    PathRejected,
    declared_path,
    finalize_declared,
    is_inside,
    is_reparse_point,
    normalize_path,
    same_path,
    write_utf8,
)
from dual_agent.winproc import run_captured


class WorktreeError(PathRejected):
    """Worktree 不能安全创建。"""


def _safe_name(text: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in text)
    return (cleaned.strip("_") or "task")[:60]


def planned_worktree_path(config: TaskConfig) -> Path:
    if config.worktree.path:
        return _contained_path(config.worktree.path)
    root_raw = config.worktree.root or str(PROJECT_ROOT / "workspaces" / "worktrees")
    root = _contained_path(root_raw)
    project = _safe_name(config.project_id or "task")
    token = _safe_name(config.schedule_id or config.task_id)
    return _contained_path(str(root / project / token))


def prepare_worktree_config(config: TaskConfig) -> tuple[TaskConfig, dict[str, object]]:
    """创建 Worktree 并返回指向新目录的任务配置。未启用时原样返回。"""
    if not config.worktree.enabled:
        return config, {}
    repo = finalize_declared(config.workspace)
    if not repo.exists() or not repo.is_dir():
        raise WorktreeError(f"Worktree 的原始仓库不存在：{repo}")
    if not _is_git_repo(repo):
        raise WorktreeError(f"工作区不是 Git 仓库，不能创建 Worktree：{repo}")
    destination = planned_worktree_path(config)
    if destination.exists():
        raise WorktreeError(f"Worktree 目标已存在，拒绝覆盖：{destination}")
    if is_inside(destination, repo) or same_path(destination, repo):
        raise WorktreeError("Worktree 不能放在原始工作区内部")
    if is_inside(destination, PROJECT_ROOT / "dual_agent"):
        raise WorktreeError("Worktree 不能放在协调器源码目录")
    code, before, err = _git(repo, ["rev-parse", "HEAD"])
    if code != 0 or not before.strip():
        raise WorktreeError(f"无法读取原始仓库 HEAD：{err.strip() or before.strip()}")
    original_head = before.strip()
    code, commit, err = _git(repo, ["rev-parse", "--verify", f"{config.worktree.base_ref}^{{commit}}"])
    if code != 0 or not commit.strip():
        raise WorktreeError(f"找不到可信提交 {config.worktree.base_ref}：{err.strip() or commit.strip()}")
    base_commit = commit.strip()
    branch = f"dual-agent/{_safe_name(config.task_id)}/{_safe_name(config.schedule_id or base_commit[:8])}"
    ref = f"refs/heads/{branch}"
    exists, _, _ = _git(repo, ["show-ref", "--verify", "--quiet", ref])
    if exists == 0:
        raise WorktreeError(f"任务分支已存在，拒绝覆盖：{branch}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_name = "git-metadata:" + normalize_path(_git_common_dir(repo))
    handle, reason = _acquire_git_lock(config.task_id or "worktree", lock_name)
    if handle is None:
        raise WorktreeError(reason or "Git 元数据锁被占用")
    try:
        code, out, err = _git(
            repo,
            ["worktree", "add", "-b", branch, str(destination), base_commit],
        )
        if code != 0:
            raise WorktreeError(f"创建 Worktree 失败：{(err or out).strip()}")
    finally:
        handle.release()
    code, after, err = _git(repo, ["rev-parse", "HEAD"])
    after_head = after.strip()
    if code != 0 or after_head != original_head:
        raise WorktreeError("创建 Worktree 后原始仓库 HEAD 发生变化，已停止")
    if not destination.is_dir() or is_reparse_point(destination):
        raise WorktreeError(f"Worktree 目录不可用：{destination}")
    retargeted = _retarget(config, repo, destination)
    meta = {
        "repo": str(repo),
        "path": str(destination),
        "branch": branch,
        "base_ref": config.worktree.base_ref,
        "base_commit": base_commit,
        "original_head": original_head,
        "original_head_after": after_head,
        "preserved_original_head": after_head == original_head,
        "sandbox": False,
    }
    return retargeted, meta


def save_worktree_diff(worktree: Path, run_dir: Path) -> str:
    """保存 status 和 diff。发现冲突标记只记录，不合并、不覆盖。"""
    pieces: list[str] = []
    conflict = False
    for args in (
        ["status", "--short"],
        ["diff", "--no-ext-diff", "--"],
        ["diff", "--cached", "--no-ext-diff", "--"],
    ):
        code, out, err = _git(worktree, args, timeout=20)
        text = out.strip()
        if code != 0:
            pieces.append(f"$ git {' '.join(args)}\n{(err or out).strip()}")
            continue
        if text:
            pieces.append(f"$ git {' '.join(args)}\n{text}")
        if "<<<<<<<" in text:
            conflict = True
    body = "\n\n".join(pieces) if pieces else "（无差异）"
    if conflict:
        body = "检测到冲突标记。协调器不会自动合并或覆盖。\n\n" + body
    write_utf8(run_dir / "worktree.diff", body + "\n")
    dump_json(run_dir / "worktree-status.json", {"conflict_markers": conflict, "path": str(worktree)})
    return body


def cleanup_clean_worktree(meta: dict[str, object]) -> dict[str, object]:
    """Remove only an unchanged owned checkout; keep dirty work and its branch.

    Never force removal, reset files or delete branches. The caller has already
    saved the diff and must confirm all CLI processes have exited.
    """
    repo = _contained_path(str(meta['repo'])); path = _contained_path(str(meta['path']))
    if same_path(repo, path) or is_inside(path, repo): raise WorktreeError('拒绝清理源仓库')
    code, head, _ = _git(repo, ['rev-parse','HEAD'])
    if code != 0 or head.strip() != meta['original_head']: raise WorktreeError('源仓库 HEAD 改变，保留 Worktree')
    code, branch, _ = _git(path, ['symbolic-ref','--short','HEAD'])
    if code != 0 or branch.strip() != meta['branch'] or _git_common_dir(path) != _git_common_dir(repo):
        raise WorktreeError('Worktree 归属无法确认，拒绝清理')
    handle, reason = _acquire_git_lock('worktree-cleanup', 'git-metadata:' + normalize_path(_git_common_dir(repo)))
    if handle is None: raise WorktreeError(reason)
    try:
        code, status, _ = _git(path, ['status','--porcelain','--untracked-files=all'])
        if code != 0 or status.strip(): return {'removed':False,'reason':'改动已保留；不会强制删除','path':str(path)}
        code, out, err = _git(repo, ['worktree','remove',str(path)])
        return {'removed':code == 0,'reason':(err or out).strip(),'path':str(path),'branch_preserved':True}
    finally: handle.release()


def _retarget(config: TaskConfig, repo: Path, destination: Path) -> TaskConfig:
    roots = list(config.allowed_roots)
    if roots and not any(_inside_root(destination, item) for item in roots):
        roots.append(str(destination.parent))
    writes = [_map_write(item, repo, destination) for item in config.write_paths]
    checks = [_map_check(item, repo, destination) for item in config.verification]
    spec = replace(config.worktree, path=str(destination), enabled=False)
    return replace(
        config,
        workspace=str(destination),
        allowed_roots=roots,
        write_paths=writes or [str(destination)],
        verification=checks,
        worktree=spec,
    )


def _map_check(check: VerificationCheck, repo: Path, destination: Path) -> VerificationCheck:
    return replace(
        check,
        path=_swap(check.path, repo, destination),
        cwd=_swap(check.cwd, repo, destination),
        argv=[_swap(part, repo, destination) for part in check.argv],
    )


def _map_write(raw: str, repo: Path, destination: Path) -> str:
    try:
        path = finalize_declared(raw)
    except PathRejected:
        return _swap(raw, repo, destination)
    if same_path(path, repo):
        return str(destination)
    if is_inside(path, repo):
        relative = Path(os.path.relpath(str(path), str(repo)))
        return str(destination / relative)
    return raw


def _replace_path_prefix(text: str, old: str, new: str) -> str:
    """Replace a path only at a separator boundary, never as a sibling prefix."""
    if not old or old not in text:
        return text
    pieces: list[str] = []
    start = 0
    while True:
        at = text.find(old, start)
        if at < 0:
            pieces.append(text[start:])
            break
        end = at + len(old)
        if end == len(text) or text[end] in "\\/":
            pieces.append(text[start:at])
            pieces.append(new)
        else:
            pieces.append(text[start:end])
        start = end
    return "".join(pieces)


def _swap(text: str, repo: Path, destination: Path) -> str:
    if not text:
        return text
    variants = sorted(
        {str(repo), str(repo).replace("\\", "/"), str(repo).replace("/", "\\")},
        key=len,
        reverse=True,
    )
    result = text
    destination_text = str(destination)
    for item in variants:
        result = _replace_path_prefix(result, item, destination_text)
    return result


def _inside_root(path: Path, raw_root: str) -> bool:
    try:
        return is_inside(path, finalize_declared(raw_root))
    except PathRejected:
        return False


def _contained_path(raw: str) -> Path:
    declared = declared_path(raw)
    resolved = finalize_declared(raw)
    parent = declared.parent
    if parent != declared:
        boundary = finalize_declared(str(parent))
        if not is_inside(resolved, boundary):
            raise WorktreeError(f"Worktree 路径越过重解析点或 ..：{raw} -> {resolved}")
    if resolved.exists() and is_reparse_point(resolved):
        raise WorktreeError(f"Worktree 目标本身是重解析点：{resolved}")
    return resolved


def _is_git_repo(repo: Path) -> bool:
    code, out, _ = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


def _git_common_dir(repo: Path) -> Path:
    code, out, err = _git(repo, ["rev-parse", "--git-common-dir"])
    if code != 0 or not out.strip():
        raise WorktreeError(f"无法定位 Git 公共目录：{err.strip()}")
    path = Path(out.strip())
    if not path.is_absolute():
        path = repo / path
    return finalize_declared(str(path))


def _acquire_git_lock(task_id: str, name: str):
    manager = ResourceLockManager(default_resource_lock_dir())
    deadline = time.time() + 20
    reason = ""
    while True:
        handle, reason = manager.try_acquire_all(task_id, [name])
        if handle is not None:
            return handle, ""
        if time.time() > deadline:
            return None, reason
        time.sleep(0.05)


def _git(repo: Path, args: list[str], timeout: float = 30) -> tuple[int | None, str, str]:
    captured = run_captured(
        ["git", "-c", "core.quotepath=false", "-c", "core.hooksPath=" + os.devnull,
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-C", str(repo), *args],
        cwd=str(repo),
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        input_bytes=None,
        timeout=timeout,
    )
    stdout = captured.stdout.decode("utf-8", errors="replace")
    stderr = captured.stderr.decode("utf-8", errors="replace")
    return captured.exit_code, stdout, stderr
