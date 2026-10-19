"""图形界面到现有 CLI / 协调器公开函数的薄封装。

安全判断仍由 Coordinator、build_project_task 和执行器完成。
任务文本只写入 JSON 文件，不会拼进 Shell 命令。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from dual_agent import PROJECT_ROOT
from dual_agent.coordinator import default_runs_dir
from dual_agent.locks import pid_alive
from dual_agent.models import ConfigError, TaskConfig, dump_json
from dual_agent.projects import (
    disable_project,
    enable_project,
    list_projects,
    project_history,
    register_project,
    show_project,
    update_project,
)
from dual_agent.scheduler import cancel_schedule
from dual_agent.status import read_status

_SKIP_DIRS = {"locks", "schedule", "batches", "gui-batches", "readiness"}
_CREATE_NO_WINDOW = 0x08000000
_NEW_PROCESS_GROUP = 0x00000200


class ConsoleAdapter:
    def __init__(self, root: Path | None = None, runs_dir: Path | None = None) -> None:
        self.root = root
        self.runs_dir = runs_dir
        self.processes: list[dict[str, Any]] = []
        self.last_argv: list[str] = []
        self.last_batch_path: Path | None = None
        self.last_child_env: dict[str, str] = {}
        self._lock = threading.Lock()

    def runs_path(self) -> Path:
        return Path(self.runs_dir) if self.runs_dir is not None else default_runs_dir()

    def codex_env_enabled(self) -> bool:
        return os.environ.get("DUAL_AGENT_ALLOW_CODEX") == "1"

    def list_project_rows(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in list_projects(self.root)]

    def project_row(self, project_id: str) -> dict[str, Any]:
        return show_project(project_id, self.root).to_dict()

    def register_from_mapping(self, data: dict[str, Any]):
        path = self._dump_temp(data)
        try:
            return register_project(path, self.root)
        finally:
            path.unlink(missing_ok=True)

    def update_from_mapping(self, project_id: str, data: dict[str, Any]):
        path = self._dump_temp(data)
        try:
            return update_project(project_id, path, self.root)
        finally:
            path.unlink(missing_ok=True)

    def set_enabled(self, project_id: str, enabled: bool):
        if enabled:
            return enable_project(project_id, self.root)
        return disable_project(project_id, self.root)

    def build_task(self, project_id: str, spec: dict[str, Any]):
        return build_task(project_id, spec, self.root)

    def history(self, project_id: str) -> list[dict[str, Any]]:
        return project_history(project_id, self.runs_dir)

    def schedules(self) -> list[dict[str, Any]]:
        directory = self.runs_path() / "schedule"
        if not directory.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(directory.glob("sch-*.json")):
            data = read_status(path)
            if not data:
                continue
            located = self.locate_run(data)
            if located and not data.get("run_dir"):
                data["run_dir"] = located
            if located:
                task = _json_obj(Path(located) / "task.json")
                if task.get("title"):
                    data["title"] = task["title"]
            rows.append(data)
        rows.sort(key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""), reverse=True)
        return rows

    def locate_run(self, schedule: dict[str, Any]) -> str:
        run_dir = schedule.get("run_dir")
        if isinstance(run_dir, str) and Path(run_dir).is_dir():
            return run_dir
        schedule_id = str(schedule.get("schedule_id") or "")
        if not schedule_id:
            return ""
        runs = self.runs_path()
        if not runs.is_dir():
            return ""
        task_id = str(schedule.get("task_id") or "")
        roots: list[Path] = []
        direct = runs / task_id
        if direct.is_dir():
            roots.append(direct)
        else:
            for item in runs.iterdir():
                if item.is_dir() and item.name not in _SKIP_DIRS:
                    roots.append(item)
        found: list[Path] = []
        for task_dir in roots:
            try:
                children = list(task_dir.iterdir())
            except OSError:
                continue
            for run in children:
                task_file = run / "task.json"
                if not task_file.is_file():
                    continue
                data = _json_obj(task_file)
                if data.get("schedule_id") == schedule_id:
                    found.append(run)
        if not found:
            return ""
        found.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return str(found[0])

    def load_run(self, run_dir: str) -> dict[str, Any]:
        root = Path(run_dir)
        view: dict[str, Any] = {
            "run_dir": str(root),
            "messages": [],
            "events": [],
            "verification": [],
            "result": {},
            "task": {},
            "report": "",
            "rounds": [],
            "worktree_diff": "",
            "error": "",
        }
        if not root.is_dir():
            view["error"] = "运行目录不存在"
            return view
        view["messages"] = _jsonl(root / "messages.jsonl")
        view["events"] = _jsonl(root / "events.jsonl")
        verification = _json_any(root / "verification.json")
        view["verification"] = verification if isinstance(verification, list) else []
        view["result"] = _json_obj(root / "result.json")
        view["task"] = _json_obj(root / "task.json")
        report = root / "报告.md"
        if report.is_file():
            view["report"] = _read_limited(report, 80_000)
        diff = root / "worktree.diff"
        if diff.is_file():
            view["worktree_diff"] = _read_limited(diff, 40_000)
        rounds = root / "rounds"
        if rounds.is_dir():
            for item in sorted(p for p in rounds.iterdir() if p.is_dir()):
                changes = item / "changes.diff"
                stderr = item / "stderr.txt"
                answer = item / "answer.txt"
                view["rounds"].append(
                    {
                        "name": item.name,
                        "meta": _json_obj(item / "meta.json"),
                        "diff": _read_limited(changes, 20_000) if changes.is_file() else "",
                        "stderr": _read_limited(stderr, 8_000) if stderr.is_file() else "",
                        "answer": _read_limited(answer, 8_000) if answer.is_file() else "",
                    }
                )
        return view

    def cancel(self, schedule_id: str) -> str:
        return cancel_schedule(schedule_id, self.runs_dir)

    def prepare_specs(self, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for spec in specs:
            item = dict(spec)
            if self.runs_dir is not None and not item.get("runs_dir"):
                item["runs_dir"] = str(self.runs_dir)
            prepared.append(item)
        return prepared

    def start_batch(
        self,
        specs: list[dict[str, Any]],
        *,
        max_concurrent: int = 2,
        allow_codex_env: bool = False,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        if not specs:
            raise ConfigError("没有可启动的任务")
        if len(specs) > 2:
            raise ConfigError("同一批次最多两个任务")
        if self.has_live_process() or self.blocking_batches():
            raise ConfigError("已有协调器批次仍在运行，拒绝再次提交")
        prepared = self.prepare_specs(specs)
        configs: list[TaskConfig] = []
        for spec in prepared:
            project_id = spec.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                raise ConfigError("任务缺少 project_id")
            payload = {key: value for key, value in spec.items() if key != "project_id"}
            configs.append(self.build_task(project_id, payload))
        identity = batch_id or "gui-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        batch: dict[str, Any] = {
            "batch_id": identity,
            "max_concurrent_tasks": max_concurrent,
            "tasks": prepared,
        }
        if any(item.allow_real_codex or item.allow_real_cursor for item in configs):
            codex_limit, cursor_limit = _real_call_limits(configs)
            batch["codex_call_limit"] = codex_limit
            batch["cursor_call_limit"] = cursor_limit
        directory = self.runs_path() / "gui-batches"
        directory.mkdir(parents=True, exist_ok=True)
        batch_path = directory / f"{identity}.json"
        dump_json(batch_path, batch)
        log_path = directory / f"{identity}.log"
        argv = [sys_executable(), "-X", "utf8", "-m", "dual_agent", "batch", "--file", str(batch_path)]
        if self.root is not None:
            argv.extend(["--root", str(self.root)])
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("QT_QPA_PLATFORM", None)
        if allow_codex_env:
            env["DUAL_AGENT_ALLOW_CODEX"] = "1"
        else:
            env.pop("DUAL_AGENT_ALLOW_CODEX", None)
        log_handle = log_path.open("ab")
        popen_kwargs: dict[str, Any] = {
            "cwd": str(PROJECT_ROOT),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = _CREATE_NO_WINDOW | _NEW_PROCESS_GROUP
        process = subprocess.Popen(argv, **popen_kwargs)
        pid_path = directory / f"{identity}.pid"
        pid_path.write_text(str(process.pid), encoding="utf-8", newline="\n")
        record = {
            "popen": process,
            "batch_id": identity,
            "log_path": log_path,
            "log_handle": log_handle,
            "argv": list(argv),
        }
        with self._lock:
            self.processes.append(record)
            self.last_argv = list(argv)
            self.last_batch_path = batch_path
            self.last_child_env = dict(env)
        return {"batch_id": identity, "batch_path": str(batch_path), "pid": process.pid}

    def has_live_process(self) -> bool:
        with self._lock:
            return any(item["popen"].poll() is None for item in self.processes)

    def blocking_batches(self) -> list[str]:
        """关闭窗口后仍在跑的批次。只认 pid 文件，不把调度状态改写成成功。"""
        directory = self.runs_path() / "gui-batches"
        if not directory.exists():
            return []
        found: list[str] = []
        for path in sorted(directory.glob("*.pid")):
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if pid_alive(pid):
                found.append(path.stem)
        return found

    def process_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._lock:
            snapshot = list(self.processes)
        for item in snapshot:
            code = item["popen"].poll()
            if code is not None:
                handle = item.get("log_handle")
                if handle is not None and not handle.closed:
                    handle.close()
                    item["log_handle"] = None
            rows.append(
                {
                    "batch_id": item["batch_id"],
                    "pid": item["popen"].pid,
                    "running": code is None,
                    "exit_code": code,
                    "log": _tail(Path(item["log_path"])),
                }
            )
        return rows

    def overview(self, *, history: bool = True) -> dict[str, Any]:
        projects = self.list_project_rows()
        schedules = self.schedules()
        histories: dict[str, list[dict[str, Any]]] = {}
        if history:
            for project in projects:
                project_id = str(project.get("project_id") or "")
                try:
                    histories[project_id] = self.history(project_id)[:40]
                except (OSError, ConfigError):
                    histories[project_id] = []
        return {
            "projects": projects,
            "schedules": schedules,
            "histories": histories,
            "processes": self.process_rows(),
        }

    def _dump_temp(self, data: dict[str, Any]) -> Path:
        handle = tempfile.NamedTemporaryFile(prefix="dual-agent-gui-", suffix=".json", delete=False)
        path = Path(handle.name)
        handle.close()
        dump_json(path, data)
        return path


def _real_call_limits(configs: list[TaskConfig]) -> tuple[int, int]:
    """真实调用额度不超过本批流程步数，修复轮次只加在开发者对应的执行器上。"""
    codex_cap = 0
    cursor_cap = 0
    for config in configs:
        codex_steps = 0
        cursor_steps = 0
        developer_executor = ""
        for step in config.flow:
            participant = config.participant(step)
            if participant.executor == "codex":
                codex_steps += 1
            elif participant.executor == "cursor":
                cursor_steps += 1
            if participant.role == "developer":
                developer_executor = participant.executor
        extra = config.max_repair_rounds if developer_executor else 0
        if developer_executor == "codex":
            codex_steps += extra
        elif developer_executor == "cursor":
            cursor_steps += extra
        codex_cap += min(config.max_model_calls, codex_steps)
        cursor_cap += min(config.max_model_calls, cursor_steps)
    return codex_cap, cursor_cap


def build_task(project_id: str, spec: dict[str, Any], root: Path | None):
    from dual_agent.projects import build_project_task

    return build_project_task(project_id, spec, root)


def sys_executable() -> str:
    import sys

    return sys.executable


def _json_obj(path: Path) -> dict[str, Any]:
    data = _json_any(path)
    return data if isinstance(data, dict) else {}


def _json_any(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _jsonl(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    rows: list[Any] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_limited(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断）"


def _tail(path: Path, limit: int = 4000) -> str:
    if not path.is_file():
        return ""
    text = _read_limited(path, 200_000)
    if len(text) <= limit:
        return text
    return text[-limit:]
