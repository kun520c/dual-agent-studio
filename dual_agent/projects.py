"""项目注册。每个项目一个 JSON 文件，不使用数据库。

项目之间默认不共享任务上下文。工作区可以在其他磁盘，但本模块拒绝面试项目和修图项目路径。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dual_agent import PROJECT_ROOT
from dual_agent.models import (
    ConfigError,
    Participant,
    TaskConfig,
    VerificationCheck,
    WorktreeSpec,
    _as_bool,
    _as_int,
    _as_str_list,
    _as_timeout,
    _parse_resource_locks,
    _parse_verification,
    _parse_worktree,
    _require_str,
    dump_json,
    parse_task,
)
from dual_agent.roles import canonicalize_executor, canonicalize_role
from dual_agent.grants import reject_registration
from dual_agent.safety import PACKAGE_DIR, PathRejected, finalize_declared, is_inside
from dual_agent.status import status_from_result_dict

_PROJECT_KEYS = {
    "project_id",
    "name",
    "workspace",
    "allowed_roots",
    "enabled",
    "verification",
    "participants",
    "max_repair_rounds",
    "max_model_calls",
    "max_run_seconds",
    "timeout_seconds",
    "max_rounds",
    "concurrency",
    "resource_locks",
    "worktree",
    "description",
}
_CONCURRENCY_KEYS = {"max_write_tasks", "allow_shared_context"}
_TASK_SPEC_KEYS = {
    "task_id",
    "title",
    "goal",
    "description",
    "mock_plan",
    "timeout_seconds",
    "max_rounds",
    "max_repair_rounds",
    "max_model_calls",
    "max_run_seconds",
    "allow_real_codex",
    "allow_real_cursor",
    "flow",
    "runs_dir",
    "codex_bin",
    "cursor_bin",
    "worktree",
    "resource_locks",
    "verification",
    "share_context",
    "shared_note",
    "baseline_verify",
    "codex_call_limit",
    "cursor_call_limit",
}
_SKIP_RUN_DIRS = {"locks", "schedule", "batches"}


@dataclass
class ProjectConfig:
    project_id: str
    name: str
    workspace: str
    allowed_roots: list[str]
    participants: list[Participant]
    verification: list[VerificationCheck] = field(default_factory=list)
    enabled: bool = True
    max_repair_rounds: int = 1
    max_model_calls: int = 8
    max_run_seconds: float = 900
    timeout_seconds: float = 120
    max_rounds: int = 6
    max_write_tasks: int = 1
    allow_shared_context: bool = False
    resource_locks: list[str] = field(default_factory=list)
    worktree: WorktreeSpec = field(default_factory=WorktreeSpec)
    description: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source", None)
        data["concurrency"] = {
            "max_write_tasks": self.max_write_tasks,
            "allow_shared_context": self.allow_shared_context,
        }
        data.pop("max_write_tasks", None)
        data.pop("allow_shared_context", None)
        return data


def projects_dir(root: Path | None = None) -> Path:
    return Path(root) if root is not None else PROJECT_ROOT / "projects"


def register_project(path: Path, root: Path | None = None) -> ProjectConfig:
    project = load_project_file(path)
    target = _project_path(project.project_id, root)
    if target.exists():
        raise ConfigError(f"项目已存在：{project.project_id}")
    dump_json(target, project.to_dict())
    project.source = str(target)
    return project


def update_project(project_id: str, path: Path, root: Path | None = None) -> ProjectConfig:
    current = _project_path(project_id, root)
    if not current.exists():
        raise ConfigError(f"项目不存在：{project_id}")
    project = load_project_file(path)
    if project.project_id != project_id:
        raise ConfigError("不能在更新时更改 project_id")
    dump_json(current, project.to_dict())
    project.source = str(current)
    return project


def disable_project(project_id: str, root: Path | None = None) -> ProjectConfig:
    return _set_enabled(project_id, False, root)


def enable_project(project_id: str, root: Path | None = None) -> ProjectConfig:
    return _set_enabled(project_id, True, root)


def list_projects(root: Path | None = None) -> list[ProjectConfig]:
    directory = projects_dir(root)
    if not directory.exists():
        return []
    found: list[ProjectConfig] = []
    for path in sorted(directory.glob("*.json")):
        found.append(load_project_file(path))
    return found


def show_project(project_id: str, root: Path | None = None) -> ProjectConfig:
    path = _project_path(project_id, root)
    if not path.exists():
        raise ConfigError(f"项目不存在：{project_id}")
    return load_project_file(path)


def build_project_task(project_id: str, spec: dict[str, Any], root: Path | None = None) -> TaskConfig:
    project = show_project(project_id, root)
    if not project.enabled:
        raise ConfigError(f"项目已禁用，拒绝新任务：{project_id}")
    if not isinstance(spec, dict):
        raise ConfigError("任务规格必须是对象")
    unknown = sorted(set(spec) - _TASK_SPEC_KEYS)
    if unknown:
        raise ConfigError("任务规格含有无法识别的字段：" + "、".join(unknown))
    share = _as_bool(spec.get("share_context"), "share_context", False)
    if share and not project.allow_shared_context:
        raise ConfigError("项目没有允许共享任务上下文")
    note = spec.get("shared_note") or ""
    if not isinstance(note, str):
        raise ConfigError("shared_note 必须是字符串")
    if note and not share:
        raise ConfigError("未开启共享上下文时不能写入 shared_note")
    flow = spec.get("flow") or [item.id for item in project.participants]
    if not isinstance(flow, list) or not all(isinstance(item, str) and item.strip() for item in flow):
        raise ConfigError("flow 必须是参与者 id 数组")
    known = {item.id for item in project.participants}
    missing = [item for item in flow if item not in known]
    if missing:
        raise ConfigError("flow 引用了项目中不存在的参与者：" + "、".join(missing))
    task_id = spec.get("task_id") or f"{project.project_id}-task"
    if not isinstance(task_id, str) or not task_id.strip():
        raise ConfigError("task_id 必须是非空字符串")
    verification = spec.get("verification")
    if verification is None:
        verification = [item.to_dict() for item in project.verification]
    worktree = spec.get("worktree")
    if worktree is None:
        worktree = project.worktree.to_dict()
    resources = spec.get("resource_locks")
    if resources is None:
        resources = list(project.resource_locks)
    data = {
        "task_id": task_id.strip(),
        "title": _require_str(spec, "title"),
        "goal": _require_str(spec, "goal"),
        "description": spec.get("description", project.description),
        "workspace": project.workspace,
        "allowed_roots": list(project.allowed_roots),
        "participants": [item.to_dict() for item in project.participants],
        "flow": [item.strip() for item in flow],
        "verification": verification,
        "max_rounds": spec.get("max_rounds", project.max_rounds),
        "timeout_seconds": spec.get("timeout_seconds", project.timeout_seconds),
        "max_repair_rounds": spec.get("max_repair_rounds", project.max_repair_rounds),
        "max_model_calls": spec.get("max_model_calls", project.max_model_calls),
        "max_run_seconds": spec.get("max_run_seconds", project.max_run_seconds),
        "allow_real_codex": spec.get("allow_real_codex", False),
        "allow_real_cursor": spec.get("allow_real_cursor", False),
        "mock_plan": spec.get("mock_plan") or [],
        "write_paths": [project.workspace],
        "project_id": project.project_id,
        "resource_locks": resources,
        "worktree": worktree,
        "share_context": share,
        "shared_note": note.strip(),
        "max_write_tasks": project.max_write_tasks,
        "runs_dir": spec.get("runs_dir"),
        "codex_bin": spec.get("codex_bin"),
        "cursor_bin": spec.get("cursor_bin"),
    }
    if data["runs_dir"] is None:
        data.pop("runs_dir")
    if data["codex_bin"] is None:
        data.pop("codex_bin")
    if data["cursor_bin"] is None:
        data.pop("cursor_bin")
    for key in ("baseline_verify", "codex_call_limit", "cursor_call_limit"):
        if key in spec:
            data[key] = spec[key]
    if not data["description"]:
        data["description"] = ""
    return parse_task(data, source=f"project:{project.project_id}")


def project_history(project_id: str, runs_dir: Path | None = None) -> list[dict[str, Any]]:
    root = Path(runs_dir) if runs_dir is not None else PROJECT_ROOT / "runs"
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for task_dir in root.iterdir():
        if not task_dir.is_dir() or task_dir.name in _SKIP_RUN_DIRS:
            continue
        for run_dir in task_dir.iterdir():
            task_file = run_dir / "task.json"
            if not task_file.is_file():
                continue
            try:
                task_data = json.loads(task_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(task_data, dict) or task_data.get("project_id") != project_id:
                continue
            result_data: dict[str, Any] = {}
            result_file = run_dir / "result.json"
            if result_file.is_file():
                try:
                    loaded = json.loads(result_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    loaded = {}
                if isinstance(loaded, dict):
                    result_data = loaded
            status = status_from_result_dict(result_data) if result_data else "failed"
            rows.append(
                {
                    "project_id": project_id,
                    "task_id": task_data.get("task_id") or task_dir.name,
                    "run_dir": str(run_dir),
                    "report_path": str(run_dir / "报告.md"),
                    "status": status,
                    "stop_reason": result_data.get("stop_reason") or "",
                    "success": status == "verified" and result_data.get("success") is True,
                    "error_message": result_data.get("error_message") or "",
                    "started_at": result_data.get("started_at") or "",
                    "finished_at": result_data.get("finished_at") or "",
                    "mode_label": result_data.get("mode_label") or "",
                }
            )
    rows.sort(key=lambda item: (str(item.get("finished_at") or ""), str(item.get("run_dir") or "")), reverse=True)
    return rows


def project_status(project_id: str, root: Path | None = None, runs_dir: Path | None = None) -> dict[str, Any]:
    project = show_project(project_id, root)
    history = project_history(project_id, runs_dir)
    latest = history[0] if history else None
    active = _active_schedules(project_id, runs_dir)
    return {
        "project_id": project.project_id,
        "name": project.name,
        "enabled": project.enabled,
        "workspace": project.workspace,
        "latest": latest,
        "active": active,
    }


def load_project_file(path: Path) -> ProjectConfig:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"无法读取项目配置：{path}（{exc}）") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"项目配置不是合法 JSON：{path}（第 {exc.lineno} 行）") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 的根节点必须是对象")
    unknown = sorted(set(data) - _PROJECT_KEYS)
    if unknown:
        raise ConfigError(f"{path} 含有无法识别的字段：{', '.join(unknown)}")
    project_id = _require_str(data, "project_id")
    name = _require_str(data, "name")
    workspace = _require_str(data, "workspace")
    reject_registration(workspace)
    try:
        workspace_path = finalize_declared(workspace)
    except PathRejected as exc:
        raise ConfigError(str(exc)) from exc
    if not workspace_path.is_dir():
        raise ConfigError(f"项目工作区不存在或不是目录：{workspace_path}")
    if is_inside(workspace_path, PACKAGE_DIR) or workspace_path == PACKAGE_DIR:
        raise ConfigError("项目工作区不能是协调器源码目录")
    allowed = _as_str_list(data.get("allowed_roots"), "allowed_roots")
    if not allowed:
        raise ConfigError("allowed_roots 必须是非空数组")
    for item in allowed:
        reject_registration(item)
        try:
            root_path = finalize_declared(item)
        except PathRejected as exc:
            raise ConfigError(str(exc)) from exc
        if not root_path.is_dir():
            raise ConfigError(f"允许根目录不存在或不是目录：{root_path}")
    participants = _parse_participants(data.get("participants"))
    concurrency = data.get("concurrency") or {}
    if not isinstance(concurrency, dict):
        raise ConfigError("concurrency 必须是对象")
    extra = sorted(set(concurrency) - _CONCURRENCY_KEYS)
    if extra:
        raise ConfigError("concurrency 含有无法识别的字段：" + "、".join(extra))
    description = data.get("description") or ""
    if not isinstance(description, str):
        raise ConfigError("description 必须是字符串")
    return ProjectConfig(
        project_id=project_id,
        name=name,
        workspace=str(workspace_path),
        allowed_roots=[str(finalize_declared(item)) for item in allowed],
        participants=participants,
        verification=_parse_verification(data.get("verification")),
        enabled=_as_bool(data.get("enabled"), "enabled", True),
        max_repair_rounds=_as_int(data.get("max_repair_rounds"), "max_repair_rounds", 1, 0, 5),
        max_model_calls=_as_int(data.get("max_model_calls"), "max_model_calls", 8, 1, 30),
        max_run_seconds=_as_timeout(data.get("max_run_seconds")) if data.get("max_run_seconds") is not None else 900,
        timeout_seconds=_as_timeout(data.get("timeout_seconds")),
        max_rounds=_as_int(data.get("max_rounds"), "max_rounds", 6, 1, 100),
        max_write_tasks=_as_int(concurrency.get("max_write_tasks"), "max_write_tasks", 1, 1, 2),
        allow_shared_context=_as_bool(concurrency.get("allow_shared_context"), "allow_shared_context", False),
        resource_locks=_parse_resource_locks(data.get("resource_locks")),
        worktree=_parse_worktree(data.get("worktree")),
        description=description.strip(),
        source=str(path),
    )


def _parse_participants(value: Any) -> list[Participant]:
    if not isinstance(value, list) or not value:
        raise ConfigError("participants 必须是非空数组")
    found: list[Participant] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"第 {index} 个参与者必须是对象")
        extra = sorted(set(item) - {"id", "executor", "role"})
        if extra:
            raise ConfigError("参与者含有无法识别的字段：" + "、".join(extra))
        participant_id = _require_str(item, "id")
        if participant_id in seen:
            raise ConfigError(f"参与者 id 重复：{participant_id}")
        seen.add(participant_id)
        try:
            executor = canonicalize_executor(_require_str(item, "executor"))
            role = canonicalize_role(_require_str(item, "role"))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        found.append(Participant(participant_id, executor, role))
    return found


def _project_path(project_id: str, root: Path | None) -> Path:
    if not project_id or any(part in project_id for part in ("/", "\\", "..")):
        raise ConfigError("project_id 不能包含路径")
    return projects_dir(root) / f"{project_id}.json"


def _set_enabled(project_id: str, enabled: bool, root: Path | None) -> ProjectConfig:
    project = show_project(project_id, root)
    project.enabled = enabled
    dump_json(_project_path(project_id, root), project.to_dict())
    return project


def _active_schedules(project_id: str, runs_dir: Path | None) -> list[dict[str, Any]]:
    root = Path(runs_dir) if runs_dir is not None else PROJECT_ROOT / "runs"
    schedule = root / "schedule"
    if not schedule.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(schedule.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("project_id") != project_id:
            continue
        state = data.get("state")
        if state in {"queued", "running", "verifying", "reviewing", "repairing"}:
            rows.append(data)
    return rows
