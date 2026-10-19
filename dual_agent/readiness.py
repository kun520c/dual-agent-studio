"""正式项目接入前的静态检查。

不调用模型，不注册项目，不创建 Worktree，不连接数据库或网络。
目录检查通过不能当成操作系统级隔离。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dual_agent import PROJECT_ROOT
from dual_agent.grants import SKIP_DIR_NAMES, assess, is_secret_filename, lexical_key
from dual_agent.models import now_iso
from dual_agent.safety import (
    PACKAGE_DIR,
    PathRejected,
    declared_path,
    is_inside,
    is_reparse_point,
    normalize_path,
    write_utf8,
)

_STATUSES = {"PASS", "BLOCKED", "NOT CHECKED"}
_DEP_PATTERNS = (
    ("jdbc", re.compile(r"jdbc:", re.IGNORECASE)),
    ("mysql", re.compile(r"\bmysql\b", re.IGNORECASE)),
    ("redis", re.compile(r"\bredis\b", re.IGNORECASE)),
    ("milvus", re.compile(r"\bmilvus\b", re.IGNORECASE)),
    ("mongodb", re.compile(r"\bmongodb\b", re.IGNORECASE)),
    ("postgres", re.compile(r"\bpostgres(?:ql)?\b", re.IGNORECASE)),
    ("openai", re.compile(r"\bopenai\b", re.IGNORECASE)),
    ("http", re.compile(r"https?://", re.IGNORECASE)),
    ("RestTemplate", re.compile(r"\bRestTemplate\b")),
    ("WebClient", re.compile(r"\bWebClient\b")),
)
_TEXT_SUFFIXES = {".java", ".py", ".md", ".txt", ".json", ".xml", ".yml", ".yaml", ".properties", ".gradle", ".kts"}
_SCRIPT_SUFFIXES = (".ps1", ".py", ".bat", ".cmd")


@dataclass
class CheckItem:
    code: str
    title: str
    status: str
    detail: str

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"非法检查状态：{self.status}")


def check_workspace(workspace: str, task: dict | None = None) -> list[CheckItem]:
    """检查一个未来可能接入的目录。拒绝的正式项目路径不会被读取。"""
    decision = assess(workspace, task)
    if not decision.allowed:
        return _stopped(
            CheckItem(
                "forbidden",
                "禁止访问的项目",
                "BLOCKED",
                decision.detail,
            )
        )
    try:
        declared = declared_path(workspace)
    except Exception as exc:
        return [CheckItem("workspace", "工作区路径", "BLOCKED", str(exc))]
    if declared.exists() and is_reparse_point(declared):
        return _stopped(
            CheckItem(
                "symlink",
                "符号链接与重解析点",
                "BLOCKED",
                "工作区本身是 Junction 或符号链接。已停止后续检查，没有进入链接目标。",
            )
        )
    if _is_system_path(declared) or _inside_package(declared):
        return _stopped(
            CheckItem(
                "workspace",
                "工作区路径",
                "BLOCKED",
                "工作区位于系统目录或协调器源码目录。没有继续扫描其中的文件。",
            )
        )
    if not declared.exists() or not declared.is_dir():
        return _stopped(CheckItem("workspace", "工作区路径", "BLOCKED", f"工作区不存在或不是目录：{declared}"))

    items: list[CheckItem] = [CheckItem("workspace", "工作区路径", "PASS", f"工作区是普通目录：{declared}")]
    items.append(_git_item(declared))
    items.append(_dirty_item(declared))
    if decision.grant:
        items.append(
            CheckItem(
                "worktree",
                "独立 Worktree",
                "BLOCKED",
                "该路径只有只读授权，禁止创建 Worktree。本次没有创建。Worktree 也不是操作系统级隔离。",
            )
        )
    else:
        items.append(_worktree_item(declared))
    items.append(_symlink_item(declared, grant=decision.grant))
    items.extend(_trusted_items(declared, task))
    items.append(_dependency_item(declared, grant=decision.grant))
    if decision.grant:
        items.append(
            CheckItem(
                "readonly_grant",
                "精确路径只读授权",
                "PASS",
                decision.detail,
            )
        )
    items.append(_write_item(declared))
    items.append(_lock_item(task))
    items.append(_readonly_item(declared))
    items.append(_budget_item(task))
    items.append(
        CheckItem(
            "os_isolation",
            "操作系统级隔离",
            "NOT CHECKED",
            "目录、Git 和 write_paths 检查不能证明操作系统级隔离。"
            "Worktree 只是另一个目录。Cursor Ask 模式不是沙箱。"
            "Codex workspace-write 可以写整个任务工作区。",
        )
    )
    return items


def render_readiness_report(workspace: str, items: list[CheckItem]) -> str:
    counts = {name: sum(1 for item in items if item.status == name) for name in ("PASS", "BLOCKED", "NOT CHECKED")}
    lines = [
        "# 项目接入前检查",
        "",
        f"- 检查对象：{workspace}",
        f"- 检查时间：{now_iso()}",
        "- 检查方式：只读查看本机目录、Git 状态和文本关键字。没有调用模型，没有注册项目，没有连接数据库、网络或付费 API。",
        "- 隔离说明：下面任何 PASS 都不表示该项目拥有操作系统级隔离。",
        "",
        "## 结果",
        "",
        "| 检查项 | 结果 | 说明 |",
        "| --- | --- | --- |",
    ]
    for item in items:
        detail = item.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item.title} | {item.status} | {detail} |")
    lines.extend(
        [
            "",
            "## 汇总",
            "",
            f"- PASS：{counts['PASS']}",
            f"- BLOCKED：{counts['BLOCKED']}",
            f"- NOT CHECKED：{counts['NOT CHECKED']}",
            "",
            "## 接入前仍须人工确认",
            "",
            "- 可信测试是否覆盖真正要守住的行为，而不只是文件存在。",
            "- 模型额度、修复次数和总时长是否还承担得起一次失败后的修复。",
            "- 工作区里没有凭据、生产数据和指向外部的链接。",
            "- 真正接入前先用审查者做一次只读运行，确认不会写入。",
            "",
        ]
    )
    return "\n".join(lines)


def write_readiness_report(workspace: str, text: str, destination: Path | None = None) -> Path:
    if destination is None:
        name = _safe_name(Path(workspace).name or "workspace")
        destination = PROJECT_ROOT / "runs" / "readiness" / name / "接入检查报告.md"
    _assert_report_destination(destination, workspace)
    write_utf8(destination, text)
    return destination


def _assert_report_destination(destination: Path, workspace: str) -> None:
    """报告只能留在协调器项目里，不能写进被检查的正式项目。"""
    if not is_inside(destination, PROJECT_ROOT):
        raise ValueError("接入报告只能写在协调器项目内")
    try:
        key = lexical_key(workspace).rstrip("\\/")
    except PathRejected:
        return
    dest_key = normalize_path(destination).rstrip("\\/")
    if dest_key == key or dest_key.startswith(key + os.sep):
        raise ValueError("拒绝把接入报告写到被检查的项目路径")


def _stopped(reason: CheckItem) -> list[CheckItem]:
    return [
        reason,
        CheckItem("os_isolation", "操作系统级隔离", "NOT CHECKED", "检查在进入目录前停止。这不能说明项目有或没有操作系统级隔离。"),
    ]


def _is_system_path(path: Path) -> bool:
    keys = ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    for key in keys:
        raw = os.environ.get(key)
        if raw and is_inside(path, Path(raw)):
            return True
    return False


def _inside_package(path: Path) -> bool:
    return is_inside(path, PACKAGE_DIR) or path == PACKAGE_DIR


def _git(repo: Path, args: list[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return completed.returncode, _decode(completed.stdout), _decode(completed.stderr)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def _git_item(repo: Path) -> CheckItem:
    code, out, err = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out != "true":
        return CheckItem("git", "Git 仓库", "BLOCKED", "不是 Git 工作区，不能安全创建独立 Worktree。" + (err or out))
    head, commit, head_err = _git(repo, ["rev-parse", "HEAD"])
    if head != 0 or not commit:
        return CheckItem("git", "Git 仓库", "BLOCKED", "Git 仓库没有 HEAD。" + head_err)
    return CheckItem("git", "Git 仓库", "PASS", f"是 Git 工作区，HEAD 为 {commit[:12]}。")


def _dirty_item(repo: Path) -> CheckItem:
    code, out, err = _git(repo, ["status", "--porcelain"])
    if code != 0:
        return CheckItem("uncommitted", "未提交修改", "NOT CHECKED", "无法读取 git status。" + (err or out))
    if not out:
        return CheckItem("uncommitted", "未提交修改", "PASS", "工作区干净，没有未提交修改。")
    count = len([line for line in out.splitlines() if line.strip()])
    return CheckItem(
        "uncommitted",
        "未提交修改",
        "BLOCKED",
        f"有 {count} 条未提交变更。接入前应先处理，避免 Worktree 漏掉这些修改，也避免协调器和人工改动叠在一起。",
    )


def _worktree_item(repo: Path) -> CheckItem:
    code, out, _ = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out != "true":
        return CheckItem("worktree", "独立 Worktree", "BLOCKED", "没有 Git 仓库，不能创建 Worktree。这仍然不是操作系统级隔离。")
    head, commit, _ = _git(repo, ["rev-parse", "HEAD"])
    if head != 0 or not commit:
        return CheckItem("worktree", "独立 Worktree", "BLOCKED", "没有可用提交，不能作为 Worktree 基线。")
    return CheckItem(
        "worktree",
        "独立 Worktree",
        "PASS",
        "从目录和 Git 状态看，可以另建工作树。本次没有实际创建。Worktree 不是操作系统级隔离。",
    )


def _symlink_item(root: Path, *, grant: bool = False) -> CheckItem:
    found: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        if current != root and is_reparse_point(current):
            found.append(str(current))
            dirnames[:] = []
            continue
        kept: list[str] = []
        for name in dirnames:
            seen += 1
            candidate = current / name
            if name == ".git" or (grant and name in SKIP_DIR_NAMES) or is_reparse_point(candidate):
                if is_reparse_point(candidate):
                    found.append(str(candidate))
                continue
            kept.append(name)
            if seen > 400:
                return CheckItem(
                    "symlink",
                    "符号链接与重解析点",
                    "NOT CHECKED",
                    "目录过多，符号链接检查在 400 项后停止。",
                )
        dirnames[:] = kept
        for name in filenames:
            seen += 1
            candidate = current / name
            if is_reparse_point(candidate):
                found.append(str(candidate))
            if seen > 400:
                break
        if seen > 400:
            break
    if found:
        sample = "、".join(found[:5])
        return CheckItem("symlink", "符号链接与重解析点", "BLOCKED", f"发现重解析点，没有进入其目标：{sample}")
    return CheckItem("symlink", "符号链接与重解析点", "PASS", "抽样范围内没有发现 Junction 或符号链接。")


def _trusted_items(workspace: Path, task: dict | None) -> list[CheckItem]:
    if not isinstance(task, dict) or "verification" not in task:
        return [CheckItem("trusted", "可信测试", "NOT CHECKED", "没有提供带 verification 的任务配置，不能判断可信测试是否已固定。")]
    verification = task.get("verification")
    if not isinstance(verification, list) or not verification:
        return [CheckItem("trusted", "可信测试", "BLOCKED", "任务配置没有可信验证。接入后不能只靠模型声明验收。")]
    commands = [item for item in verification if isinstance(item, dict) and item.get("type") == "command"]
    if not commands:
        return [CheckItem("trusted", "可信测试", "BLOCKED", "验证里没有协调器亲自执行的 command。")]
    problems: list[str] = []
    for index, item in enumerate(commands, start=1):
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv:
            problems.append(f"第 {index} 条命令没有 argv")
            continue
        digest = str(item.get("sha256") or "")
        script = _script_from_argv(argv, str(item.get("cwd") or ""))
        if script is None:
            problems.append(f"第 {index} 条命令没有工作区外的脚本路径")
            continue
        if not script.is_file():
            problems.append(f"脚本不存在：{script}")
            continue
        if is_reparse_point(script):
            problems.append(f"脚本是重解析点：{script}")
            continue
        if is_inside(script, workspace):
            problems.append(f"脚本位于可写工作区内：{script}")
            continue
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            problems.append(f"脚本没有固定 SHA-256：{script.name}")
            continue
        actual = hashlib.sha256(script.read_bytes()).hexdigest()
        if actual != digest.lower():
            problems.append(f"脚本摘要与配置不一致：{script.name}")
    if problems:
        return [CheckItem("trusted", "可信测试", "BLOCKED", "；".join(problems))]
    return [CheckItem("trusted", "可信测试", "PASS", "可信命令位于工作区外，并且 SHA-256 与配置一致。")]


def _script_from_argv(argv: list, cwd: str) -> Path | None:
    for part in argv[1:]:
        if not isinstance(part, str) or not part.lower().endswith(_SCRIPT_SUFFIXES):
            continue
        path = Path(part)
        if not path.is_absolute() and cwd:
            path = Path(cwd) / path
        return path
    return None


def _dependency_item(root: Path, *, grant: bool = False) -> CheckItem:
    hits: list[str] = []
    scanned = 0
    skipped_secrets = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            candidate = current / name
            if name == ".git" or (grant and name in SKIP_DIR_NAMES) or is_reparse_point(candidate):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if scanned >= 200:
                truncated = True
                break
            path = current / name
            if is_secret_filename(name):
                skipped_secrets += 1
                continue
            if path.suffix.lower() not in _TEXT_SUFFIXES or is_reparse_point(path):
                continue
            if path.stat().st_size > 262144:
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for label, pattern in _DEP_PATTERNS:
                if pattern.search(text):
                    relative = path.relative_to(root).as_posix()
                    hits.append(f"{relative} 含有 {label}")
                    break
        if truncated:
            break
    secret_note = f"另有 {skipped_secrets} 个可能含凭据的文件没有读取。" if skipped_secrets else ""
    if hits:
        return CheckItem(
            "dependencies",
            "外部数据库、网络或付费 API",
            "BLOCKED",
            "静态文本里发现外部依赖关键字，没有实际连接：" + "；".join(hits[:8]) + "。" + secret_note,
        )
    if truncated:
        return CheckItem(
            "dependencies",
            "外部数据库、网络或付费 API",
            "NOT CHECKED",
            "文本扫描在 200 个文件处停止，不能把未扫描部分说成没有外部依赖。" + secret_note,
        )
    if skipped_secrets:
        return CheckItem(
            "dependencies",
            "外部数据库、网络或付费 API",
            "NOT CHECKED",
            f"跳过了 {skipped_secrets} 个可能含凭据的文件，没有读取内容，因此不能判定没有外部依赖。",
        )
    if scanned == 0:
        return CheckItem("dependencies", "外部数据库、网络或付费 API", "NOT CHECKED", "没有可扫描的文本源码。")
    return CheckItem(
        "dependencies",
        "外部数据库、网络或付费 API",
        "PASS",
        f"静态扫描了 {scanned} 个文本文件，没有发现数据库、HTTP 或付费 API 关键字。这不是运行时证明。",
    )


def _write_item(workspace: Path) -> CheckItem:
    if os.access(workspace, os.W_OK):
        return CheckItem(
            "write",
            "写入权限",
            "PASS",
            "当前进程对工作区有写入权限。这不是资源锁，也不能把 write_paths 变成文件系统白名单。",
        )
    return CheckItem("write", "写入权限", "BLOCKED", "当前进程不能写入该工作区。")


def _lock_item(task: dict | None) -> CheckItem:
    if not isinstance(task, dict) or "resource_locks" not in task:
        return CheckItem("locks", "资源锁", "NOT CHECKED", "没有资源锁声明。检查器不会连接数据库、缓存或端口。")
    locks = task.get("resource_locks")
    if locks is None:
        return CheckItem("locks", "资源锁", "NOT CHECKED", "资源锁字段为空。检查器不连接外部资源。")
    if not isinstance(locks, list):
        return CheckItem("locks", "资源锁", "BLOCKED", "resource_locks 必须是数组。")
    if not locks:
        return CheckItem("locks", "资源锁", "PASS", "未声明外部资源锁。检查器没有连接任何服务。")
    names = "、".join(str(item) for item in locks[:8])
    return CheckItem("locks", "资源锁", "NOT CHECKED", f"声明了 {names}。检查器不连接这些资源，也不能确认它们空闲。")


def _readonly_item(workspace: Path) -> CheckItem:
    if os.access(workspace, os.R_OK):
        return CheckItem(
            "readonly",
            "只读试运行",
            "PASS",
            "工作区可读。只读试运行应只使用审查者或诊断者、空的 write_paths，并且不要打开真实写入开关。本次没有启动模型。",
        )
    return CheckItem("readonly", "只读试运行", "BLOCKED", "当前进程不能读取该工作区。")


def _budget_item(task: dict | None) -> CheckItem:
    if not isinstance(task, dict):
        return CheckItem("budget", "模型与修复预算", "NOT CHECKED", "没有任务配置，无法判断调用预算。")
    problems: list[str] = []
    calls = task.get("max_model_calls", 8)
    repairs = task.get("max_repair_rounds", 1)
    seconds = task.get("max_run_seconds", 900)
    if isinstance(calls, bool) or not isinstance(calls, int) or not 1 <= calls <= 8:
        problems.append("max_model_calls 应在 1 到 8")
    if isinstance(repairs, bool) or not isinstance(repairs, int) or not 0 <= repairs <= 2:
        problems.append("max_repair_rounds 应在 0 到 2")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= float(seconds) <= 1800:
        problems.append("max_run_seconds 应在 1 到 1800")
    for key, label in (("codex_call_limit", "Codex"), ("cursor_call_limit", "Cursor")):
        value = task.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
            problems.append(f"{label} 调用上限应在 0 到 4")
    if problems:
        return CheckItem("budget", "模型与修复预算", "BLOCKED", "；".join(problems))
    return CheckItem(
        "budget",
        "模型与修复预算",
        "PASS",
        f"max_model_calls={calls}，max_repair_rounds={repairs}，max_run_seconds={seconds}。剩余额度不是必须消耗的目标。",
    )


def _safe_name(text: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in text)
    return (cleaned.strip("_") or "workspace")[:60]
