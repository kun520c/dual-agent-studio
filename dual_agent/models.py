"""任务配置、消息和运行结果。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from dual_agent.roles import canonicalize_executor, canonicalize_role


class ConfigError(ValueError):
    """任务配置无法解析。"""


class IntegrityError(ValueError):
    """磁盘上的消息与摘要不一致。"""


class ProtocolError(ValueError):
    """Agent 最终回答不符合交接协议。"""


_TASK_KEYS = {
    "task_id",
    "title",
    "goal",
    "workspace",
    "max_rounds",
    "timeout_seconds",
    "participants",
    "flow",
    "cycle",
    "write_paths",
    "mock_plan",
    "allowed_roots",
    "allow_real_codex",
    "allow_real_cursor",
    "lock_dir",
    "runs_dir",
    "codex_bin",
    "cursor_bin",
    "description",
    "verification",
    "max_repair_rounds",
    "max_model_calls",
    "max_run_seconds",
    "project_id",
    "batch_id",
    "schedule_id",
    "status_file",
    "cancel_file",
    "resource_locks",
    "worktree",
    "share_context",
    "shared_note",
    "max_write_tasks",
    "baseline_verify",
    "codex_call_limit",
    "cursor_call_limit",
    "execution_mode",
}
_WORKTREE_KEYS = {"enabled", "base_ref", "root", "path"}
_RESOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_REF_RE = re.compile(r"[A-Za-z0-9_./-]{1,120}$")
_VERIFICATION_TYPES = {"file_exists", "file_contains", "file_sha256", "command", "allowed_changes"}
_VERIFICATION_KEYS = {
    "type",
    "path",
    "text",
    "sha256",
    "argv",
    "cwd",
    "expect_exit_code",
    "timeout_seconds",
    "allowed_paths",
    "require_paths",
}
_SCRIPT_SUFFIXES = {".ps1", ".py", ".bat", ".cmd", ".exe", ".com"}
_PARTICIPANT_KEYS = {"id", "executor", "role"}
_TASK_ID_RE = re.compile(r"^[\w\-\u4e00-\u9fff]+$", re.UNICODE)
_DECISIONS = {"continue", "accept", "reject", "blocked"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} 必须是非空字符串")
    return value.strip()


def _as_bool(value: Any, key: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是 true 或 false")
    return value


def _as_int(value: Any, key: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数")
    if value < low or value > high:
        raise ConfigError(f"{key} 必须在 {low} 到 {high} 之间")
    return value


def _optional_limit(value: Any, key: str) -> int | None:
    """未填写表示不额外限制。0 表示不允许该执行器再发起调用。"""
    if value is None:
        return None
    return _as_int(value, key, 0, 0, 30)


def _as_timeout(value: Any) -> float:
    if value is None:
        return 120.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("timeout_seconds 必须是数字")
    number = float(value)
    if number < 0.2 or number > 3600:
        raise ConfigError("timeout_seconds 必须在 0.2 到 3600 之间")
    return number


def _token_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _optional_text(data: dict[str, Any], key: str, default: str = "") -> str:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须是字符串")
    return value.strip()


def _optional_path(data: dict[str, Any], key: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须是字符串")
    text = value.strip()
    return text or None


def _parse_resource_locks(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("resource_locks 必须是字符串数组")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _RESOURCE_RE.fullmatch(item.strip()):
            raise ConfigError("resource_locks 只能包含字母、数字、下划线、点、冒号和短横线")
        names.append(item.strip())
    return names


def _parse_worktree(value: Any) -> WorktreeSpec:
    if value is None:
        return WorktreeSpec()
    if not isinstance(value, dict):
        raise ConfigError("worktree 必须是对象")
    extra = sorted(set(value) - _WORKTREE_KEYS)
    if extra:
        raise ConfigError(f"worktree 含有无法识别的字段：{', '.join(extra)}")
    enabled = _as_bool(value.get("enabled"), "worktree.enabled", False)
    base_ref = value.get("base_ref", "HEAD")
    root = value.get("root", "")
    path = value.get("path", "")
    for key, item in (("base_ref", base_ref), ("root", root), ("path", path)):
        if item is None:
            item = ""
        if not isinstance(item, str):
            raise ConfigError(f"worktree.{key} 必须是字符串")
    base = str(base_ref).strip() or "HEAD"
    if ".." in base or "\\" in base or not _REF_RE.fullmatch(base):
        raise ConfigError("worktree.base_ref 只能包含字母、数字、下划线、点、斜杠和短横线")
    return WorktreeSpec(enabled=enabled, base_ref=base, root=str(root).strip(), path=str(path).strip())


def _as_str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{key} 必须是非空字符串组成的数组")
    return [item.strip() for item in value]


@dataclass
class VerificationCheck:
    type: str
    path: str = ""
    text: str = ""
    sha256: str = ""
    argv: list[str] = field(default_factory=list)
    cwd: str = ""
    expect_exit_code: int = 0
    timeout_seconds: float = 60
    allowed_paths: list[str] = field(default_factory=list)
    require_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorktreeSpec:
    """可选 Git Worktree。这不是操作系统沙箱。"""

    enabled: bool = False
    base_ref: str = "HEAD"
    root: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Participant:
    id: str
    executor: str
    role: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class TaskConfig:
    task_id: str
    title: str
    goal: str
    workspace: str
    participants: list[Participant]
    flow: list[str]
    max_rounds: int = 6
    timeout_seconds: float = 120.0
    cycle: bool = False
    write_paths: list[str] = field(default_factory=list)
    mock_plan: list[str] = field(default_factory=list)
    allowed_roots: list[str] = field(default_factory=list)
    allow_real_codex: bool = False
    allow_real_cursor: bool = False
    execution_mode: str = 'mock'
    execution_authority: Any = field(default=None, repr=False, compare=False)
    lock_dir: str | None = None
    runs_dir: str | None = None
    codex_bin: str | None = None
    cursor_bin: str | None = None
    description: str = ""
    verification: list[VerificationCheck] = field(default_factory=list)
    max_repair_rounds: int = 1
    max_model_calls: int = 8
    max_run_seconds: float = 900
    project_id: str = ""
    batch_id: str = ""
    schedule_id: str = ""
    status_file: str | None = None
    cancel_file: str | None = None
    resource_locks: list[str] = field(default_factory=list)
    worktree: WorktreeSpec = field(default_factory=WorktreeSpec)
    share_context: bool = False
    shared_note: str = ""
    max_write_tasks: int = 1
    baseline_verify: bool = False
    codex_call_limit: int | None = None
    cursor_call_limit: int | None = None
    source: str = "<memory>"

    def has_developer(self) -> bool:
        selected = set(self.flow)
        return any(item.role == "developer" and item.id in selected for item in self.participants)

    def participant(self, participant_id: str) -> Participant:
        for item in self.participants:
            if item.id == participant_id:
                return item
        raise ConfigError(f"流程引用了不存在的参与者：{participant_id}")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(replace(self, execution_authority=None))
        data.pop('execution_authority', None)
        data.pop("source", None)
        return data


@dataclass
class Message:
    id: str
    task_id: str
    round_index: int
    sender: str
    receiver: str | None
    role: str
    content: str
    content_sha256: str
    parent_id: str | None
    created_at: str
    executor: str
    invoked: bool
    mode_label: str
    exit_code: int | None
    error_kind: str
    error_message: str
    duration_seconds: float
    decision: str | None
    injection_suspected: bool
    claimed_files: list[str]
    unsafe_paths: list[str]
    untrusted: bool = True
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        verify_message_dict(data)
        return cls(
            id=data["id"],
            task_id=data["task_id"],
            round_index=data["round_index"],
            sender=data["sender"],
            receiver=data.get("receiver"),
            role=data["role"],
            content=data["content"],
            content_sha256=data["content_sha256"],
            parent_id=data.get("parent_id"),
            created_at=data["created_at"],
            executor=data["executor"],
            invoked=bool(data["invoked"]),
            mode_label=data["mode_label"],
            exit_code=data.get("exit_code"),
            error_kind=data["error_kind"],
            error_message=data.get("error_message") or "",
            duration_seconds=float(data.get("duration_seconds") or 0),
            decision=data.get("decision"),
            injection_suspected=bool(data.get("injection_suspected")),
            claimed_files=list(data.get("claimed_files") or []),
            unsafe_paths=list(data.get("unsafe_paths") or []),
            untrusted=bool(data.get("untrusted", True)),
            input_tokens=_token_count(data.get("input_tokens")),
            output_tokens=_token_count(data.get("output_tokens")),
        )


@dataclass
class RunResult:
    task_id: str
    title: str
    goal: str
    workspace: str
    success: bool
    stop_reason: str
    stop_reason_zh: str
    error_message: str
    warnings: list[str]
    run_dir: Path
    report_path: Path
    messages: list[Message]
    rounds_executed: int
    mode_label: str
    started_at: str
    finished_at: str
    retried: bool = False
    duration_seconds: float = 0.0
    processes_cleared: bool = True
    lock_released: bool = True
    verification: list[dict[str, Any]] = field(default_factory=list)
    project_id: str = ""
    batch_id: str = ""
    status: str = ""
    repairs_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    codex_calls: int = 0
    cursor_calls: int = 0
    worktree_path: str = ""
    execution_mode: str = 'mock'
    offline_cli_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["run_dir"] = str(self.run_dir)
        data["report_path"] = str(self.report_path)
        return data


class MessageLog:
    """JSONL 消息日志。每次读取都重新计算 UTF-8 摘要。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, message: Message) -> None:
        verify_message_dict(message.to_dict())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(message.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def load(self) -> list[Message]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        messages: list[Message] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IntegrityError(f"消息日志第 {line_no} 行不是合法 JSON") from exc
            if not isinstance(data, dict):
                raise IntegrityError(f"消息日志第 {line_no} 行不是对象")
            try:
                messages.append(Message.from_dict(data))
            except (KeyError, TypeError, IntegrityError) as exc:
                raise IntegrityError(f"消息日志第 {line_no} 行无法校验：{exc}") from exc
        return messages


def verify_message_dict(data: dict[str, Any]) -> None:
    content = data.get("content")
    digest = data.get("content_sha256")
    if not isinstance(content, str) or not isinstance(digest, str):
        raise IntegrityError("消息缺少正文或摘要")
    actual = content_hash(content)
    if actual != digest:
        raise IntegrityError("消息摘要不一致，内容可能已损坏或被篡改")


_PROTOCOL_PREFIX_LIMIT = 80


def adapt_protocol_text(text: str) -> tuple[str, str | None]:
    """从少量前置说明中取出唯一完整的 JSON 对象。

    使用 JSON 解码器确认对象边界。不使用从第一个花括号截到最后一个花括号的办法。
    成功时返回规范化 JSON，以及被保留的前置说明；无法无歧义提取时抛出 ProtocolError。
    """
    stripped = text.strip()
    if not stripped:
        raise ProtocolError("最终回答为空")
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        value, end = None, None
    if isinstance(value, dict) and not stripped[end:].strip():
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")), None

    found: list[tuple[int, int, dict[str, Any]]] = []
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            candidate, consumed = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            found.append((index, index + consumed, candidate))
    top_level: list[tuple[int, int, dict[str, Any]]] = []
    for start, end_at, candidate in found:
        if any(earlier < start and end_at <= later for earlier, later, _ in found):
            continue
        top_level.append((start, end_at, candidate))
    if len(top_level) != 1:
        raise ProtocolError("无法无歧义地确定唯一的 JSON 对象")
    start, end_at, candidate = top_level[0]
    prefix = stripped[:start]
    suffix = stripped[end_at:]
    if suffix.strip():
        raise ProtocolError("JSON 对象之后还有其他正文")
    if "```" in prefix or "{" in prefix or "}" in prefix or len(prefix) > _PROTOCOL_PREFIX_LIMIT:
        raise ProtocolError("JSON 前的说明超出保守适配范围")
    warning = prefix.strip() or None
    canonical = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
    return canonical, warning


def parse_protocol(text: str, role: str) -> dict[str, Any]:
    """只接受完整 JSON 对象。Markdown、缺字段、非法决策都是协议错误。"""
    raw = text.strip()
    if not raw.startswith("{") or not raw.endswith("}"):
        raise ProtocolError("最终回答必须是单个 JSON 对象，不能包含 Markdown 或其他文本")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"JSON 无法解析：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("JSON 根节点必须是对象")
    required = {"summary", "details", "decision", "files_touched", "risks"}
    missing = sorted(required - set(data))
    extra = sorted(set(data) - required)
    if missing or extra:
        raise ProtocolError(f"协议字段不正确，缺少 {'、'.join(missing) or '无'}，多余 {'、'.join(extra) or '无'}")
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        raise ProtocolError("summary 必须是非空字符串")
    if not isinstance(data["details"], str):
        raise ProtocolError("details 必须是字符串")
    decision = data["decision"]
    if not isinstance(decision, str) or decision not in _DECISIONS:
        raise ProtocolError("decision 不是允许的终态或中间态")
    if decision == "accept" and role != "acceptor":
        raise ProtocolError("只有验收者可以给出 accept，其他角色给出 accept 是协议错误")
    files = data["files_touched"]
    risks = data["risks"]
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ProtocolError("files_touched 必须是字符串数组")
    if not isinstance(risks, list) or not all(isinstance(item, str) for item in risks):
        raise ProtocolError("risks 必须是字符串数组")
    return data


def normalize_decision(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    decision = value.strip().lower()
    if decision in _DECISIONS:
        return decision
    return None


def claimed_files(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("files_touched") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item.strip()]


def parse_task(data: Any, *, source: str = "<memory>") -> TaskConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{source} 的根节点必须是对象")
    unknown = sorted(set(data) - _TASK_KEYS)
    if unknown:
        raise ConfigError(f"{source} 含有无法识别的字段：{', '.join(unknown)}")
    task_id = _require_str(data, "task_id")
    if not _TASK_ID_RE.fullmatch(task_id) or any(part in task_id for part in ("/", "\\", "..")):
        raise ConfigError("task_id 只能包含字母、数字、下划线、短横线和中文")
    participants_raw = data.get("participants")
    if not isinstance(participants_raw, list) or not participants_raw:
        raise ConfigError("participants 必须是非空数组")
    participants: list[Participant] = []
    seen: set[str] = set()
    for index, item in enumerate(participants_raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"第 {index} 个参与者必须是对象")
        extra = sorted(set(item) - _PARTICIPANT_KEYS)
        if extra:
            raise ConfigError(f"参与者含有无法识别的字段：{', '.join(extra)}")
        participant_id = _require_str(item, "id")
        if participant_id in seen:
            raise ConfigError(f"参与者 id 重复：{participant_id}")
        seen.add(participant_id)
        try:
            executor = canonicalize_executor(_require_str(item, "executor"))
            role = canonicalize_role(_require_str(item, "role"))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        participants.append(Participant(participant_id, executor, role))
    flow = _as_str_list(data.get("flow"), "flow")
    if not flow:
        raise ConfigError("flow 必须是非空数组")
    known = {item.id for item in participants}
    missing = [step for step in flow if step not in known]
    if missing:
        raise ConfigError("flow 引用了不存在的参与者：" + "、".join(missing))
    description = data.get("description") or ""
    if not isinstance(description, str):
        raise ConfigError("description 必须是字符串")
    for identity_key in ("project_id", "batch_id", "schedule_id"):
        identity = _optional_text(data, identity_key)
        if identity and (not _TASK_ID_RE.fullmatch(identity) or any(part in identity for part in ("/", "\\", ".."))):
            raise ConfigError(f"{identity_key} 只能包含字母、数字、下划线、短横线和中文")
    for optional in ("lock_dir", "runs_dir", "codex_bin", "cursor_bin"):
        if data.get(optional) is not None and not isinstance(data.get(optional), str):
            raise ConfigError(f"{optional} 必须是字符串")
    return TaskConfig(
        task_id=task_id,
        title=_require_str(data, "title"),
        goal=_require_str(data, "goal"),
        workspace=_require_str(data, "workspace"),
        participants=participants,
        flow=flow,
        max_rounds=_as_int(data.get("max_rounds"), "max_rounds", 6, 1, 100),
        timeout_seconds=_as_timeout(data.get("timeout_seconds")),
        cycle=_as_bool(data.get("cycle"), "cycle", False),
        write_paths=_as_str_list(data.get("write_paths"), "write_paths"),
        mock_plan=_as_str_list(data.get("mock_plan"), "mock_plan"),
        allowed_roots=_as_str_list(data.get("allowed_roots"), "allowed_roots"),
        allow_real_codex=_as_bool(data.get("allow_real_codex"), "allow_real_codex", False),
        allow_real_cursor=_as_bool(data.get("allow_real_cursor"), "allow_real_cursor", False),
        execution_mode=_optional_text(data, 'execution_mode', 'mock'),
        lock_dir=data.get("lock_dir"),
        runs_dir=data.get("runs_dir"),
        codex_bin=data.get("codex_bin"),
        cursor_bin=data.get("cursor_bin"),
        description=description.strip(),
        verification=_parse_verification(data.get("verification")),
        max_repair_rounds=_as_int(data.get("max_repair_rounds"), "max_repair_rounds", 1, 0, 5),
        max_model_calls=_as_int(data.get("max_model_calls"), "max_model_calls", 8, 1, 30),
        max_run_seconds=_as_timeout(data.get("max_run_seconds")) if data.get("max_run_seconds") is not None else 900,
        project_id=_optional_text(data, "project_id"),
        batch_id=_optional_text(data, "batch_id"),
        schedule_id=_optional_text(data, "schedule_id"),
        status_file=_optional_path(data, "status_file"),
        cancel_file=_optional_path(data, "cancel_file"),
        resource_locks=_parse_resource_locks(data.get("resource_locks")),
        worktree=_parse_worktree(data.get("worktree")),
        share_context=_as_bool(data.get("share_context"), "share_context", False),
        shared_note=_optional_text(data, "shared_note"),
        max_write_tasks=_as_int(data.get("max_write_tasks"), "max_write_tasks", 1, 1, 2),
        baseline_verify=_as_bool(data.get("baseline_verify"), "baseline_verify", False),
        codex_call_limit=_optional_limit(data.get("codex_call_limit"), "codex_call_limit"),
        cursor_call_limit=_optional_limit(data.get("cursor_call_limit"), "cursor_call_limit"),
        source=source,
    )


def _parse_verification(value: Any) -> list[VerificationCheck]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("verification 必须是数组")
    checks: list[VerificationCheck] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"第 {index} 条验证必须是对象")
        extra = sorted(set(item) - _VERIFICATION_KEYS)
        if extra:
            raise ConfigError(f"验证项含有无法识别的字段：{', '.join(extra)}")
        kind = item.get("type")
        if kind not in _VERIFICATION_TYPES:
            raise ConfigError(f"第 {index} 条验证类型无法识别：{kind}")
        path = item.get("path") or ""
        text = item.get("text") or ""
        digest = item.get("sha256") or ""
        cwd = item.get("cwd") or ""
        if not isinstance(path, str) or not isinstance(text, str) or not isinstance(digest, str) or not isinstance(cwd, str):
            raise ConfigError(f"第 {index} 条验证的 path、text、sha256、cwd 必须是字符串")
        argv = item.get("argv") or []
        allowed_paths = item.get("allowed_paths") or []
        require_paths = item.get("require_paths") or []
        if not isinstance(argv, list) or not all(isinstance(part, str) and part.strip() for part in argv):
            raise ConfigError(f"第 {index} 条验证的 argv 必须是非空字符串数组")
        if not isinstance(allowed_paths, list) or not all(isinstance(part, str) and part.strip() for part in allowed_paths):
            raise ConfigError(f"第 {index} 条验证的 allowed_paths 必须是字符串数组")
        if not isinstance(require_paths, list) or not all(isinstance(part, str) and part.strip() for part in require_paths):
            raise ConfigError(f"第 {index} 条验证的 require_paths 必须是字符串数组")
        if kind in {"file_exists", "file_contains", "file_sha256"} and not path.strip():
            raise ConfigError(f"第 {index} 条验证缺少 path")
        if kind == "file_contains" and not text:
            raise ConfigError(f"第 {index} 条 file_contains 验证缺少 text")
        if kind == "file_sha256" and not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ConfigError(f"第 {index} 条 file_sha256 验证的 sha256 必须是 64 位十六进制")
        if kind == "command" and not argv:
            raise ConfigError(f"第 {index} 条 command 验证缺少 argv")
        if kind == "command" and digest and not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ConfigError(f"第 {index} 条 command 验证的 sha256 必须是 64 位十六进制")
        expect_exit = item.get("expect_exit_code", 0)
        if isinstance(expect_exit, bool) or not isinstance(expect_exit, int) or not 0 <= expect_exit <= 255:
            raise ConfigError(f"第 {index} 条验证的 expect_exit_code 必须是 0 到 255 的整数")
        timeout = item.get("timeout_seconds", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.2 <= float(timeout) <= 120:
            raise ConfigError(f"第 {index} 条验证的 timeout_seconds 必须在 0.2 到 120 之间")
        checks.append(
            VerificationCheck(
                kind,
                path.strip(),
                text,
                digest.lower(),
                [part.strip() for part in argv],
                cwd.strip(),
                expect_exit,
                float(timeout),
                [part.strip().replace("\\", "/") for part in allowed_paths],
                [part.strip().replace("\\", "/") for part in require_paths],
            )
        )
    return checks


def load_task(path: Path) -> TaskConfig:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"无法读取配置：{path}（{exc}）") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置不是合法 JSON：{path}（第 {exc.lineno} 行）") from exc
    return parse_task(data, source=str(path))
