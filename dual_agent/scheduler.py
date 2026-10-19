"""多项目调度。

最多两个任务同时执行。同一项目默认只放行一个写入任务。
重叠写入路径、同一个 Worktree 和同名资源锁排队，不因此取消其他任务。
单个任务失败只结束自己。写入锁和资源锁由协调器持有到任务结束，交接和修复期间不释放。
达到并发上限后进入队列。旧的 parallel 命令仍在冲突时立即停止，本调度器负责排队。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from dual_agent.budget import CallBudget, push_budget, reset_budget
from dual_agent.coordinator import Coordinator, default_runs_dir
from dual_agent.models import ConfigError, RunResult, TaskConfig, dump_json, now_iso
from dual_agent.report import render_batch_report
from dual_agent.safety import PathRejected, declared_path, finalize_declared, normalize_path, paths_overlap
from dual_agent.status import derive_terminal_status, is_terminal, read_status, write_status
from dual_agent.worktree import planned_worktree_path

_CONFLICTS = {"write_conflict", "resource_conflict"}


@dataclass(frozen=True)
class SchedItem:
    schedule_id: str
    project_id: str
    write_paths: tuple[str, ...]
    resources: tuple[str, ...]
    worktree_path: str
    writes: bool
    max_write_tasks: int


@dataclass
class ScheduledTask:
    schedule_id: str
    config: TaskConfig
    project_name: str = ""
    state: str = "queued"
    result: RunResult | None = None
    error: str = ""
    queued_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    started_perf: float = 0.0
    finished_perf: float = 0.0
    requeues: int = 0
    phases: list[str] = field(default_factory=list)
    execution_context: object = field(default=None, repr=False, compare=False)


@dataclass
class BatchResult:
    batch_id: str
    report_path: Path
    tasks: list[ScheduledTask]
    peak_running: int
    overlapped: bool
    codex_calls: int
    cursor_calls: int
    input_tokens: int
    output_tokens: int
    started_at: str
    finished_at: str
    duration_seconds: float
    call_mode: str


def normalize_concurrency(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2:
        raise ConfigError("max_concurrent_tasks 必须是 1 或 2")
    return value


def scheduling_paths(config: TaskConfig) -> list[str]:
    """调度用的写入路径。启用 Worktree 时锁计划中的任务目录，而不是原始仓库。"""
    if config.worktree.enabled and config.worktree.path:
        return [normalize_path(Path(config.worktree.path))]
    raw = list(config.write_paths)
    if not raw and config.has_developer():
        raw = [config.workspace]
    found: list[str] = []
    for item in raw:
        try:
            found.append(normalize_path(finalize_declared(item)))
        except PathRejected:
            try:
                found.append(normalize_path(declared_path(item)))
            except PathRejected:
                found.append(os.path.normcase(item))
    return found


def pick_runnable(waiting: list[SchedItem], active: list[SchedItem], max_concurrent: int) -> str | None:
    if len(active) >= max_concurrent:
        return None
    for item in waiting:
        if _blocked(item, active):
            continue
        return item.schedule_id
    return None


def cancel_schedule(schedule_id: str, runs_dir: Path | None = None) -> str:
    path = _status_path(runs_dir or default_runs_dir(), schedule_id)
    data = read_status(path)
    if not data:
        raise ConfigError(f"找不到调度任务：{schedule_id}")
    cancel_file = data.get("cancel_file")
    if isinstance(cancel_file, str) and cancel_file:
        Path(cancel_file).parent.mkdir(parents=True, exist_ok=True)
        Path(cancel_file).write_text("cancel\n", encoding="utf-8", newline="\n")
    state = str(data.get("state") or "")
    if state == "queued":
        # The runner owns the terminal state. A queued snapshot can race with
        # completion; writing cancelled here could overwrite verified.
        write_status(path, detail="已请求取消排队任务")
        return "已请求取消排队任务"
    if is_terminal(state):
        return f"任务已结束：{state}"
    write_status(path, detail="已请求取消，当前步骤结束后停止")
    return "已请求取消正在运行的任务"


def run_batch(
    configs: list[TaskConfig],
    *,
    batch_id: str | None = None,
    max_concurrent: int = 2,
    codex_call_limit: int | None = None,
    cursor_call_limit: int | None = None,
    project_names: dict[str, str] | None = None,
) -> BatchResult:
    """运行一个批次。某个任务失败不会取消同批的其他任务。"""
    limit = normalize_concurrency(max_concurrent)
    if not configs:
        raise ConfigError("批次里没有任务")
    batch = batch_id or "batch-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    if any(part in batch for part in ("/", "\\", "..")):
        raise ConfigError("batch_id 不能包含路径")
    runs_root = _shared_runs_dir(configs)
    names = project_names or {}
    budget = CallBudget(codex_call_limit, cursor_call_limit)
    tasks: list[ScheduledTask] = []
    for config in configs:
        schedule_id = "sch-" + uuid.uuid4().hex[:12]
        status_path = _status_path(runs_root, schedule_id)
        cancel_path = runs_root / "schedule" / f"{schedule_id}.cancel"
        prepared = replace(
            config,
            batch_id=batch,
            schedule_id=schedule_id,
            status_file=str(status_path),
            cancel_file=str(cancel_path),
        )
        prepared = _assign_worktree_path(prepared)
        record = ScheduledTask(
            schedule_id=schedule_id,
            config=prepared,
            project_name=names.get(prepared.project_id, prepared.project_id),
            queued_at=now_iso(),
        )
        write_status(
            status_path,
            state="queued",
            schedule_id=schedule_id,
            batch_id=batch,
            project_id=prepared.project_id,
            task_id=prepared.task_id,
            cancel_file=str(cancel_path),
            runs_dir=str(runs_root),
            detail="等待并发名额或资源锁",
        )
        tasks.append(record)
    started = time.perf_counter()
    started_at = now_iso()
    runner = _Runner(tasks, limit, budget)
    workers = [
        threading.Thread(target=runner.worker, name=f"dual-agent-batch-{index}")
        for index in range(limit)
    ]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    finished_at = now_iso()
    duration = round(time.perf_counter() - started, 3)
    report_path = _write_batch_report(
        tasks,
        batch_id=batch,
        runs_root=runs_root,
        peak=runner.peak,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        max_concurrent=limit,
        budget=budget,
    )
    codex_calls, cursor_calls, input_tokens, output_tokens, call_mode = _totals(tasks)
    return BatchResult(
        batch_id=batch,
        report_path=report_path,
        tasks=tasks,
        peak_running=runner.peak,
        overlapped=_any_overlap(tasks),
        codex_calls=codex_calls,
        cursor_calls=cursor_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        call_mode=call_mode,
    )


class _Runner:
    def __init__(self, tasks: list[ScheduledTask], limit: int, budget: CallBudget) -> None:
        self.tasks = tasks
        self.limit = limit
        self.budget = budget
        self.peak = 0
        self._active: dict[str, ScheduledTask] = {}
        self._cv = threading.Condition()

    def worker(self) -> None:
        token = push_budget(self.budget)
        try:
            while True:
                record = self._claim()
                if record is None:
                    return
                self._execute(record)
        finally:
            reset_budget(token)

    def _claim(self) -> ScheduledTask | None:
        with self._cv:
            while True:
                self._apply_queued_cancels()
                waiting = [self._view(item) for item in self.tasks if item.state == "queued"]
                active = [self._view(item) for item in self._active.values()]
                if not waiting and not self._active:
                    return None
                chosen = pick_runnable(waiting, active, self.limit)
                if chosen:
                    record = next(item for item in self.tasks if item.schedule_id == chosen)
                    record.state = "running"
                    record.started_at = now_iso()
                    record.started_perf = time.perf_counter()
                    self._active[record.schedule_id] = record
                    self.peak = max(self.peak, len(self._active))
                    self._publish(record, "running", "已获得执行名额")
                    return record
                if self._active:
                    self._cv.wait(timeout=0.3)
                    continue
                if waiting:
                    self._cv.wait(timeout=0.3)
                    continue
                return None

    def _execute(self, record: ScheduledTask) -> None:
        if _cancel_requested(record.config):
            self._finish(record, None, "任务在开始前被取消", "cancelled")
            return
        try:
            coordinator = Coordinator(status_hook=lambda phase: self._phase(record, phase))
            result = coordinator.run(record.config, context=record.execution_context) if record.execution_context is not None else coordinator.run(record.config)
            error = ""
        except Exception as exc:
            result = None
            error = f"{type(exc).__name__}: {exc}"
        if result is not None and result.stop_reason in _CONFLICTS and record.requeues < 6 and record.execution_context is None:
            record.requeues += 1
            time.sleep(0.2)
            with self._cv:
                self._active.pop(record.schedule_id, None)
                if _cancel_requested(record.config):
                    record.state = "cancelled"
                    record.finished_at = now_iso()
                    record.finished_perf = time.perf_counter()
                    record.result = result
                    self._publish(record, "cancelled", "取消前遇到资源冲突")
                else:
                    record.state = "queued"
                    record.started_perf = 0
                    record.result = None
                    self._publish(record, "queued", "资源仍被占用，已重新排队")
                self._cv.notify_all()
            return
        status = "failed"
        if result is not None:
            status = result.status or derive_terminal_status(result.stop_reason)
        elif not error:
            error = "任务没有产生结果"
            status = "failed"
        self._finish(record, result, error, status)

    def _finish(self, record: ScheduledTask, result: RunResult | None, error: str, status: str) -> None:
        if status not in {"verified", "failed", "cancelled"}:
            status = "failed"
        with self._cv:
            record.state = status
            record.result = result
            record.error = error or (result.error_message if result else "")
            record.finished_at = now_iso()
            record.finished_perf = time.perf_counter()
            self._active.pop(record.schedule_id, None)
            detail = record.error or status
            self._publish(record, status, detail)
            if result is not None and result.run_dir:
                write_status(
                    Path(record.config.status_file or ""),
                    run_dir=str(result.run_dir),
                    report_path=str(result.report_path),
                    stop_reason=result.stop_reason,
                    success=result.success,
                )
            self._cv.notify_all()

    def _phase(self, record: ScheduledTask, phase: str) -> None:
        if phase not in {"running", "verifying", "reviewing", "repairing"}:
            return
        with self._cv:
            if is_terminal(record.state):
                return
            record.state = phase
            record.phases.append(phase)
            self._publish(record, phase, phase)

    def _apply_queued_cancels(self) -> None:
        for record in self.tasks:
            if record.state == "queued" and _cancel_requested(record.config):
                record.state = "cancelled"
                record.finished_at = now_iso()
                record.error = "排队任务已取消"
                self._publish(record, "cancelled", record.error)

    def _publish(self, record: ScheduledTask, state: str, detail: str) -> None:
        if not record.config.status_file:
            return
        evidence = {}
        if record.result is not None:
            evidence = {'run_dir':str(record.result.run_dir),'report_path':str(record.result.report_path),
                        'stop_reason':record.result.stop_reason,'success':record.result.success}
        elif state == 'cancelled': evidence = {'stop_reason':'cancelled','success':False}
        write_status(
            Path(record.config.status_file),
            state=state,
            detail=detail,
            schedule_id=record.schedule_id,
            batch_id=record.config.batch_id,
            project_id=record.config.project_id,
            task_id=record.config.task_id,
            project_name=record.project_name,
            started_at=record.started_at,
            finished_at=record.finished_at,
            **evidence,
        )

    def _view(self, record: ScheduledTask) -> SchedItem:
        paths = tuple(scheduling_paths(record.config))
        worktree = ""
        if record.config.worktree.enabled and record.config.worktree.path:
            worktree = normalize_path(Path(record.config.worktree.path))
        elif record.config.worktree.path and not record.config.worktree.enabled:
            worktree = ""
        return SchedItem(
            schedule_id=record.schedule_id,
            project_id=record.config.project_id,
            write_paths=paths,
            resources=tuple(record.config.resource_locks),
            worktree_path=worktree,
            writes=bool(paths),
            max_write_tasks=record.config.max_write_tasks,
        )


class TaskScheduler(_Runner):
    """Long-lived submission adapter using the existing claim/execute/locks.

    The desktop host owns its lifetime; closing a display does not close this
    scheduler. Crash recovery never calls submit for an existing schedule ID.
    """
    def __init__(self, runs_dir: Path):
        super().__init__([], 2, CallBudget(None, None))
        self.runs_dir = Path(runs_dir)
        self._stopping = False
        self._workers = [threading.Thread(target=self.worker, name=f"dual-agent-host-{i}") for i in range(2)]
        for worker in self._workers:
            worker.start()

    def submit(self, config: TaskConfig, schedule_id: str, project_name: str = "", *, execution_context=None) -> ScheduledTask:
        if not schedule_id or any(part in schedule_id for part in ("/", "\\", "..")):
            raise ConfigError("schedule_id 不能包含路径")
        if Path(config.runs_dir or default_runs_dir()).resolve() != self.runs_dir.resolve():
            raise ConfigError("宿主任务必须使用同一个 runs_dir")
        with self._cv:
            if self._stopping:
                raise ConfigError("调度宿主正在关闭")
            self.tasks[:] = [item for item in self.tasks if not is_terminal(item.state)]
            if len(self.tasks) >= 100:
                raise ConfigError("调度队列已满（100）")
            status = _status_path(self.runs_dir, schedule_id)
            if status.exists():
                raise ConfigError("调度 ID 已存在，拒绝重复执行")
            cancel = self.runs_dir / "schedule" / f"{schedule_id}.cancel"
            prepared = _assign_worktree_path(replace(config, schedule_id=schedule_id,
                batch_id="studio-v04a", status_file=str(status), cancel_file=str(cancel)))
            record = ScheduledTask(schedule_id, prepared, project_name=project_name, queued_at=now_iso())
            record.execution_context = execution_context
            write_status(status, state="queued", schedule_id=schedule_id, batch_id=prepared.batch_id,
                project_id=prepared.project_id, task_id=prepared.task_id, title=prepared.title,
                cancel_file=str(cancel), runs_dir=str(self.runs_dir), detail="等待并发名额或资源锁",
                queued_at=record.queued_at, mode=prepared.execution_mode)
            self.tasks.append(record)
            self._cv.notify_all()
            return record

    def _claim(self) -> ScheduledTask | None:
        while True:
            with self._cv:
                while not any(item.state == "queued" for item in self.tasks) and not self._active:
                    if self._stopping:
                        return None
                    self._cv.wait(timeout=0.3)
            record = super()._claim()
            if record is not None:
                return record

    def close(self):
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        for worker in self._workers:
            worker.join()


def _blocked(item: SchedItem, active: list[SchedItem]) -> bool:
    if item.writes and item.project_id:
        same = [other for other in active if other.writes and other.project_id == item.project_id]
        if same:
            limit = min([item.max_write_tasks, *[other.max_write_tasks for other in same]])
            if len(same) >= limit:
                return True
    for other in active:
        if _paths_conflict(item.write_paths, other.write_paths):
            return True
        if item.worktree_path and item.worktree_path == other.worktree_path:
            return True
        left = {name.casefold() for name in item.resources}
        right = {name.casefold() for name in other.resources}
        if left & right:
            return True
    return False


def _paths_conflict(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    for first in left:
        for second in right:
            if paths_overlap(Path(first), Path(second)):
                return True
    return False


def _assign_worktree_path(config: TaskConfig) -> TaskConfig:
    if not config.worktree.enabled or config.worktree.path:
        return config
    path = planned_worktree_path(config)
    return replace(config, worktree=replace(config.worktree, path=str(path)))


def _shared_runs_dir(configs: list[TaskConfig]) -> Path:
    roots = {str(Path(item.runs_dir)) if item.runs_dir else str(default_runs_dir()) for item in configs}
    if len(roots) != 1:
        raise ConfigError("同一批次的任务必须使用同一个 runs_dir")
    path = Path(next(iter(roots)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _status_path(runs_dir: Path, schedule_id: str) -> Path:
    return Path(runs_dir) / "schedule" / f"{schedule_id}.json"


def _cancel_requested(config: TaskConfig) -> bool:
    if not config.cancel_file:
        return False
    path = Path(config.cancel_file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "cancel" in text.lower()


def _any_overlap(tasks: list[ScheduledTask]) -> bool:
    spans = [(item.started_perf, item.finished_perf) for item in tasks if item.started_perf and item.finished_perf]
    for index, left in enumerate(spans):
        for right in spans[index + 1 :]:
            if left[0] < right[1] and right[0] < left[1]:
                return True
    return False


def _totals(tasks: list[ScheduledTask]) -> tuple[int, int, int, int, str]:
    codex = cursor = incoming = outgoing = 0
    modes: list[str] = []
    for item in tasks:
        result = item.result
        if result is None:
            continue
        codex += result.codex_calls
        cursor += result.cursor_calls
        incoming += result.input_tokens
        outgoing += result.output_tokens
        if result.mode_label and result.mode_label not in modes:
            modes.append(result.mode_label)
    if not modes:
        call_mode = "未执行"
    elif all("真实" in mode for mode in modes) and all("模拟" not in mode for mode in modes):
        call_mode = "真实调用"
    elif all(mode == "模拟" or mode == "未执行" for mode in modes):
        call_mode = "模拟调用"
    else:
        call_mode = "混合：" + "、".join(modes)
    return codex, cursor, incoming, outgoing, call_mode


def _write_batch_report(
    tasks: list[ScheduledTask],
    *,
    batch_id: str,
    runs_root: Path,
    peak: int,
    started_at: str,
    finished_at: str,
    duration: float,
    max_concurrent: int,
    budget: CallBudget,
) -> Path:
    codex_calls, cursor_calls, input_tokens, output_tokens, call_mode = _totals(tasks)
    rows: list[dict[str, object]] = []
    issues: list[str] = []
    for item in tasks:
        result = item.result
        verification_text = "（无）"
        acceptance = "（无）"
        diff_paths = "（无）"
        if result is not None:
            commands = [entry for entry in result.verification if entry.get("type") == "command" and not entry.get("superseded")]
            if commands:
                bits = [
                    f"退出码 {entry.get('exit_code')}（预定 {entry.get('expect_exit_code')}）"
                    for entry in commands
                ]
                verification_text = "；".join(bits)
            elif result.verification:
                verification_text = "有非命令验证，详见 verification.json"
            decisions = [message.decision for message in result.messages if message.role == "acceptor"]
            acceptance = decisions[-1] if decisions else "（没有验收者结论）"
            paths = _diff_paths(result)
            diff_paths = "、".join(paths) if paths else "（无）"
            if item.state != "verified":
                issues.append(f"{item.config.task_id}：{result.stop_reason_zh} {result.error_message}".strip())
        elif item.state != "verified":
            issues.append(f"{item.config.task_id}：{item.state} {item.error}".strip())
        rows.append(
            {
                "schedule_id": item.schedule_id,
                "project_id": item.config.project_id,
                "project_name": item.project_name,
                "task_id": item.config.task_id,
                "state": item.state,
                "stop_reason": result.stop_reason if result else "",
                "mode_label": result.mode_label if result else "未执行",
                "started_at": item.started_at,
                "finished_at": item.finished_at,
                "workspace": result.workspace if result else item.config.workspace,
                "codex_calls": result.codex_calls if result else 0,
                "cursor_calls": result.cursor_calls if result else 0,
                "input_tokens": result.input_tokens if result else 0,
                "output_tokens": result.output_tokens if result else 0,
                "repairs_used": result.repairs_used if result else 0,
                "verification_text": verification_text,
                "acceptance": acceptance,
                "diff_paths": diff_paths,
                "report_path": str(result.report_path) if result else "",
                "error_message": (result.error_message if result else item.error),
            }
        )
    note = (
        f"峰值同时运行 {peak} 个任务，上限 {max_concurrent}。"
        f"时间重叠：{'有' if _any_overlap(tasks) else '没有'}。"
        "重叠按任务开始到结束的单调时钟计算，不是只看是否使用了线程。"
    )
    payload = {
        "batch_id": batch_id,
        "call_mode": call_mode,
        "max_concurrent": max_concurrent,
        "peak_running": peak,
        "overlapped": _any_overlap(tasks),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "codex_calls": codex_calls,
        "cursor_calls": cursor_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "budget": budget.snapshot(),
        "tasks": rows,
        "issues": issues,
        "concurrency_note": note,
    }
    directory = runs_root / "batches" / batch_id
    directory.mkdir(parents=True, exist_ok=True)
    dump_json(directory / "batch.json", payload)
    report = directory / "汇总报告.md"
    report.write_text(render_batch_report(payload), encoding="utf-8", newline="\n")
    return report


def _diff_paths(result: RunResult) -> list[str]:
    paths: list[str] = []
    worktree_diff = result.run_dir / "worktree.diff"
    if worktree_diff.is_file():
        paths.append(str(worktree_diff))
    rounds = result.run_dir / "rounds"
    if rounds.is_dir():
        for path in sorted(rounds.glob("*/changes.diff")):
            paths.append(str(path))
    return paths
