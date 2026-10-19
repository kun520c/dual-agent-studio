"""路径、命令和输出分类。协调器不把 Agent 文本当作命令执行。"""

from __future__ import annotations

import ctypes
import json
import os
import re
from ctypes import wintypes
from pathlib import Path

from dual_agent import PROJECT_ROOT
from dual_agent.models import TaskConfig

PACKAGE_DIR = PROJECT_ROOT / "dual_agent"


class PathRejected(ValueError):
    """任务路径不能使用。"""


class SafetyError(ValueError):
    """命令越过了允许的权限边界。"""


BANNED_FLAGS = (
    "--yolo",
    "--force",
    "--approve-for-me",
    "--ask-for-approval",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "danger-full-access",
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)\bghp_[A-Za-z0-9]{8,}\b"),
    re.compile(r"(?i)\b((?:api[_-]?key|token|password|secret|authorization)\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)\b(bearer\s+)(\S+)"),
)

_QUOTA_RE = re.compile(
    r"(insufficient[_\s-]?quota|usage limit|rate limit exceeded|too many requests|"
    r"exceeded your (current )?quota|hit your usage limit|\b429\b|"
    r"额度不足|配额已用尽|配额不足)",
    re.IGNORECASE,
)
_NETWORK_RE = re.compile(
    r"(tls handshake|certificate verify failed|certificate|"
    r"econnreset|econnrefused|enotfound|getaddrinfo|"
    r"connection (reset|refused|timed out|aborted)|"
    r"temporary failure in name resolution|network is unreachable|"
    r"网络错误|网络不可达|网络连接失败)",
    re.IGNORECASE,
)
_INJECTION_RE = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior)\s+instructions|"
    r"忽略(之前|以上|前面|全部).{0,16}(指令|规则)|"
    r"--yolo|dangerously-bypass|<\s*/?\s*system\s*>)",
    re.IGNORECASE,
)


def normalize_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def is_inside(child: Path, parent: Path) -> bool:
    child_text = normalize_path(child).rstrip("\\/")
    parent_text = normalize_path(parent).rstrip("\\/")
    return child_text == parent_text or child_text.startswith(parent_text + os.sep)


def paths_overlap(left: Path, right: Path) -> bool:
    return is_inside(left, right) or is_inside(right, left)


def redact_text(text: str) -> str:
    """落盘前去掉常见凭据，避免执行现场保存密钥。"""
    redacted = _SECRET_PATTERNS[0].sub("[REDACTED]", text)
    redacted = _SECRET_PATTERNS[1].sub("[REDACTED]", redacted)

    def _keep_label(match: re.Match[str]) -> str:
        return match.group(1) + "[REDACTED]"

    redacted = _SECRET_PATTERNS[2].sub(_keep_label, redacted)
    return _SECRET_PATTERNS[3].sub(_keep_label, redacted)


def write_utf8(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(redact_text(text).encode("utf-8"))


def same_path(left: Path, right: Path) -> bool:
    return normalize_path(left).rstrip("\\/") == normalize_path(right).rstrip("\\/")


def declared_path(raw: str) -> Path:
    """只做绝对化，不提前折掉 `..`，避免把链接后面的 `..` 当成普通目录。"""
    if not isinstance(raw, str) or not raw.strip():
        raise PathRejected("路径为空")
    text = raw.strip()
    if "\x00" in text:
        raise PathRejected("路径包含非法空字符")
    if text.startswith("\\\\"):
        raise PathRejected(f"不支持 UNC 路径：{text}")
    path = Path(text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def resolve_user_path(raw: str) -> Path:
    return finalize_declared(raw)


def is_reparse_point(path: Path) -> bool:
    if os.name != "nt" or not path.exists():
        return path.is_symlink()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    attrs = kernel.GetFileAttributesW(str(path))
    if attrs == _INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def final_existing_path(path: Path) -> Path:
    if not path.exists():
        raise PathRejected(f"路径不存在：{path}")
    if os.name != "nt":
        return path.resolve()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(
        str(path),
        0x80000000,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        raise PathRejected(f"无法解析路径：{path}")
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = kernel.GetFinalPathNameByHandleW(handle, buffer, 32768, 0)
        if size == 0:
            raise PathRejected(f"无法取得路径最终目标：{path}")
        text = buffer.value
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
        return Path(text)
    finally:
        kernel.CloseHandle(handle)


def finalize_declared(raw: str) -> Path:
    path = declared_path(raw)
    current = Path(path.anchor)
    saw_reparse = False
    for part in path.parts[1:]:
        if part == ".":
            continue
        if part == "..":
            if saw_reparse:
                raise PathRejected(f"重解析点之后不能使用 ..：{raw}")
            parent = current.parent
            if parent == current:
                raise PathRejected(f"路径越出根目录：{raw}")
            current = parent
            continue
        nxt = current / part
        if nxt.exists() and is_reparse_point(nxt):
            saw_reparse = True
            current = final_existing_path(nxt)
            continue
        current = nxt
    return current


def assert_no_escaping_links(root: Path, boundary: Path) -> None:
    """目录里的 Junction / 符号链接只要指向边界外，就拒绝整个任务。"""
    if not root.exists():
        return
    if is_reparse_point(root):
        target = final_existing_path(root)
        if not is_inside(target, boundary):
            raise PathRejected(f"重解析点越出工作区：{root} -> {target}")
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            candidate = Path(dirpath) / name
            if not is_reparse_point(candidate):
                continue
            target = final_existing_path(candidate)
            if not is_inside(target, boundary):
                raise PathRejected(f"重解析点越出工作区：{candidate} -> {target}")
            if name in dirnames:
                dirnames.remove(name)


def _system_dirs() -> list[Path]:
    keys = ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    found: list[Path] = []
    for key in keys:
        value = os.environ.get(key)
        if value:
            found.append(Path(value))
    return found


def ensure_workspace(raw: str, allowed_roots: list[str]) -> Path:
    path = finalize_declared(raw)
    if not path.exists():
        raise PathRejected(f"工作区不存在：{path}")
    if not path.is_dir():
        raise PathRejected(f"工作区不是目录：{path}")
    if path.parent == path:
        raise PathRejected(f"不能把磁盘根目录当作工作区：{path}")
    home = finalize_declared(str(Path.home()))
    if same_path(path, home):
        raise PathRejected("不能把整个用户主目录当作工作区")
    for system_dir in _system_dirs():
        if system_dir.exists() and is_inside(path, finalize_declared(str(system_dir))):
            raise PathRejected(f"工作区位于不允许的系统路径：{path}")
    if is_inside(path, PACKAGE_DIR):
        raise PathRejected("不能把协调器源码目录当作工作区")
    if allowed_roots:
        roots: list[Path] = []
        for item in allowed_roots:
            root = finalize_declared(item)
            if not root.exists() or not root.is_dir():
                raise PathRejected(f"允许根目录不存在或不是目录：{root}")
            roots.append(root)
        if not any(is_inside(path, root) for root in roots):
            raise PathRejected(f"工作区不在允许的根目录内：{path}")
    assert_no_escaping_links(path, path)
    return path


def ensure_write_paths(config: TaskConfig, workspace: Path) -> list[Path]:
    """返回调度锁要覆盖的路径。这不是操作系统写入权限。"""
    has_developer = any(item.role == "developer" and item.id in config.flow for item in config.participants)
    raw_paths = list(config.write_paths)
    if not raw_paths and not has_developer:
        return []
    if not raw_paths and has_developer:
        raw_paths = [str(workspace)]
    resolved: list[Path] = []
    for raw in raw_paths:
        path = finalize_declared(raw)
        if not is_inside(path, workspace):
            raise PathRejected(f"写入路径不在工作区内：{path}")
        if paths_overlap(path, PACKAGE_DIR):
            raise PathRejected(f"开发者写入范围不能覆盖协调器源码：{path}")
        if path.exists():
            assert_no_escaping_links(path, workspace)
        resolved.append(path)
    return resolved


def injection_suspected(text: str) -> bool:
    return _INJECTION_RE.search(text) is not None


def assert_command_allowed(command: list[str], family: str) -> None:
    """只检查参数。工作区路径和提示词里出现这些词，也不会被当成参数。"""
    if not command or any(not isinstance(part, str) or part == "" for part in command):
        raise SafetyError("命令为空或含有空参数")
    flag_tokens = [
        part.lower()
        for part in command
        if part.startswith("-") or part.lower() in {"danger-full-access", "read-only", "workspace-write"}
    ]
    blob = " ".join(flag_tokens)
    for banned in BANNED_FLAGS:
        if banned in blob:
            raise SafetyError(f"命令包含禁止的参数：{banned}")
    lowered = [part.lower() for part in command]
    if family == "cursor":
        if "--sandbox" in lowered:
            raise SafetyError(
                "Cursor 命令不使用 --sandbox。Windows 原生 sandbox enabled 不可用，"
                "Ask 模式也不是操作系统沙箱。"
            )
    elif family == "codex":
        if "--sandbox" not in lowered:
            raise SafetyError("Codex 命令必须显式指定 sandbox")
        sandbox_at = lowered.index("--sandbox")
        if sandbox_at + 1 >= len(lowered):
            raise SafetyError("Codex sandbox 缺少取值")
        mode = lowered[sandbox_at + 1]
        if mode not in {"read-only", "workspace-write"}:
            raise SafetyError("Codex sandbox 只能是 read-only 或 workspace-write")
        if "--ask-for-approval" in lowered or "--approve-for-me" in lowered:
            raise SafetyError("codex exec 0.161.0 不使用 --ask-for-approval 或 --approve-for-me")
    elif family != "mock":
        raise SafetyError(f"未知命令族：{family}")


def decode_process_bytes(data: bytes | None) -> tuple[str, bool]:
    """严格按 UTF-8 解码。失败时仍返回替换后的文本供现场保留，但第二个值必须为 True。"""
    if not data:
        return "", False
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True


def answer_text_damaged(answer: str) -> bool:
    """只检查最终交接回答。工具回显里的 U+FFFD 不在这里判断。"""
    if not answer:
        return False
    return "\ufffd" in answer or "锟斤拷" in answer


def output_marks_error(stdout: str) -> bool:
    try:
        data = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("is_error") is True:
        return True
    error = data.get("error")
    if isinstance(error, str) and error.strip():
        return True
    return isinstance(error, dict) and bool(error)


def classify_output(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    garbled: bool,
    answer: str,
) -> str:
    if timed_out:
        return "timeout"
    # garbled 只表示原始字节无法严格按 UTF-8 解码。已解码文本里的 U+FFFD 由最终回答单独判断。
    if garbled or answer_text_damaged(answer):
        return "garbled"
    if exit_code is None:
        return "unexpected"
    blob = f"{stdout}\n{stderr}"
    failure = exit_code != 0 or output_marks_error(stdout)
    if failure and _QUOTA_RE.search(blob):
        return "quota"
    if failure and _NETWORK_RE.search(blob):
        return "network"
    if exit_code != 0 or output_marks_error(stdout):
        return "nonzero"
    if not answer.strip():
        return "empty"
    return "ok"


def resolve_workspace_claim(raw: str, workspace: Path) -> Path:
    """相对路径相对任务工作区解析。绝对路径和重解析点仍按最终目标检查。"""
    text = raw.strip()
    if not text or "\x00" in text or text.startswith("\\\\"):
        raise PathRejected(f"无法解析声明路径：{raw}")
    path = Path(text)
    if not path.is_absolute():
        path = workspace / path
    return finalize_declared(str(path))


def outside_workspace(paths: list[str], workspace: Path) -> list[str]:
    unsafe: list[str] = []
    for raw in paths:
        try:
            path = resolve_workspace_claim(raw, workspace)
        except PathRejected:
            unsafe.append(raw)
            continue
        if not is_inside(path, workspace):
            unsafe.append(raw)
    return unsafe
