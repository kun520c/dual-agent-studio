"""多项目注册、并行调度、Worktree 和状态兼容。Agent 调用仍由模拟器执行。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support
from dual_agent.budget import CallBudget, push_budget, reset_budget
from dual_agent.cli import main
from dual_agent.coordinator import Coordinator, failures_equivalent
from dual_agent.executors import AgentRequest, CodexExecutor
from dual_agent.models import ConfigError, content_hash, dump_json
from dual_agent.projects import (
    build_project_task,
    disable_project,
    list_projects,
    project_history,
    project_status,
    register_project,
    show_project,
    update_project,
)
from dual_agent.scheduler import SchedItem, cancel_schedule, pick_runnable, run_batch
from dual_agent.status import derive_terminal_status, status_from_result_dict
from dual_agent.worktree import WorktreeError, prepare_worktree_config

ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=dual-agent-test",
            "-c",
            "user.email=dual-agent-test@localhost",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    self_check = _git(repo, "init")
    if self_check.returncode != 0:
        raise AssertionError(self_check.stderr)
    (repo / "keep.txt").write_text("baseline\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "keep.txt")
    committed = _git(repo, "commit", "-m", "baseline")
    if committed.returncode != 0:
        raise AssertionError(committed.stderr)
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise AssertionError(head.stderr)
    return head.stdout.strip()


def _project_file(directory: Path, project_id: str, workspace: Path, **extra: object) -> Path:
    data: dict[str, object] = {
        "project_id": project_id,
        "name": extra.pop("name", project_id),
        "workspace": str(workspace),
        "allowed_roots": [str(workspace)],
        "enabled": True,
        "participants": [
            {"id": "codex", "executor": "mock", "role": "developer"},
            {"id": "cursor", "executor": "mock", "role": "reviewer"},
        ],
        "verification": [],
        "max_repair_rounds": 1,
        "max_model_calls": 8,
        "max_run_seconds": 60,
        "concurrency": {"max_write_tasks": 1, "allow_shared_context": False},
        "resource_locks": [],
        "description": "离线测试项目",
    }
    data.update(extra)
    path = directory / f"{project_id}.json"
    dump_json(path, data)
    return path


def _developer(root: Path, task_id: str, workspace: Path, plan: list[str], **extra: object):
    return support.make_task(
        root,
        task_id=task_id,
        workspace=str(workspace),
        write_paths=[str(workspace)],
        participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
        flow=["codex"],
        mock_plan=plan,
        timeout_seconds=extra.pop("timeout_seconds", 8),
        **extra,
    )


def _intervals(overlap: Path, task_ids: list[str]) -> dict[str, tuple[float, float]]:
    found: dict[str, tuple[float, float]] = {}
    for task_id in task_ids:
        path = Path(f"{overlap}.{task_id}")
        rows = path.read_text(encoding="utf-8").splitlines()
        start = float(next(line.split()[1] for line in rows if line.startswith("start ")))
        end = float(next(line.split()[1] for line in rows if line.startswith("end ")))
        found[task_id] = (start, end)
    return found


def _overlaps(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


class PolicyTests(unittest.TestCase):
    def test_pick_runnable_covers_capacity_paths_and_resources(self) -> None:
        first = SchedItem("a", "p1", (r"e:\left",), (), "", True, 1)
        second = SchedItem("b", "p2", (r"e:\right",), (), "", True, 1)
        third = SchedItem("c", "p3", (r"e:\other",), (), "", True, 1)
        same_project = SchedItem("d", "p1", (r"e:\else",), (), "", True, 1)
        same_path = SchedItem("e", "p9", (r"e:\left\nested",), (), "", True, 1)
        same_tree = SchedItem("f", "p8", (r"e:\wt-b",), (), r"e:\wt-a", True, 2)
        other_tree = SchedItem("g", "p7", (r"e:\wt-b",), (), r"e:\wt-b", True, 2)
        resource = SchedItem("h", "p6", (r"e:\free",), ("mysql:demo",), "", True, 1)
        active = [SchedItem("a", "p1", (r"e:\left",), ("mysql:demo",), r"e:\wt-a", True, 1)]
        self.assertEqual(pick_runnable([second, third], [], 2), "b")
        self.assertIsNone(pick_runnable([third], [first, second], 2))
        self.assertEqual(pick_runnable([second, third], [first], 2), "b")
        self.assertIsNone(pick_runnable([same_project], active, 2))
        self.assertIsNone(pick_runnable([same_path], active, 2))
        self.assertIsNone(pick_runnable([same_tree], active, 2))
        self.assertEqual(pick_runnable([other_tree], active, 2), "g")
        self.assertIsNone(pick_runnable([resource], active, 2))

    def test_unknown_status_is_not_success(self) -> None:
        self.assertEqual(derive_terminal_status(None), "failed")
        self.assertEqual(derive_terminal_status("mystery"), "failed")
        self.assertEqual(derive_terminal_status("verified"), "verified")
        self.assertEqual(status_from_result_dict({"success": True}), "failed")
        self.assertEqual(status_from_result_dict({"success": True, "stop_reason": "verified"}), "verified")
        self.assertEqual(
            status_from_result_dict({"status": "verified", "stop_reason": "unverified", "success": True}),
            "failed",
        )
        self.assertEqual(status_from_result_dict({"status": "cancelled", "stop_reason": "cancelled"}), "cancelled")

    def test_equivalent_failures_stop_conservatively(self) -> None:
        left = [{"type": "command", "exit_code": 1, "stderr": "Expected 5, but got 4.", "stdout": "", "error": ""}]
        right = [{"type": "command", "exit_code": 1, "stderr": "expected 5 but got 4", "stdout": "", "error": ""}]
        other = [{"type": "command", "exit_code": 1, "stderr": "expected 5 but got 3", "stdout": "", "error": ""}]
        self.assertTrue(failures_equivalent(left, right))
        self.assertFalse(failures_equivalent(left, other))

    def test_budget_blocks_before_process_start(self) -> None:
        workspace = ROOT / "workspaces" / "示例工作区"
        request = AgentRequest(
            task_id="budget",
            round_index=1,
            agent_name="codex",
            role="developer",
            executor="codex",
            prompt="只读",
            workspace=workspace,
            timeout_seconds=5,
            scenario="ok",
            prompt_path=workspace / "prompt.txt",
            last_message_path=workspace / "last.txt",
            allow_real_codex=True,
            allow_real_cursor=False,
            codex_bin=None,
            cursor_bin=None,
            write_paths=[workspace],
        )
        previous = os.environ.get("DUAL_AGENT_ALLOW_CODEX")
        os.environ["DUAL_AGENT_ALLOW_CODEX"] = "1"
        token = push_budget(CallBudget(0, 0))
        try:
            result = CodexExecutor().run(request)
        finally:
            reset_budget(token)
            if previous is None:
                os.environ.pop("DUAL_AGENT_ALLOW_CODEX", None)
            else:
                os.environ["DUAL_AGENT_ALLOW_CODEX"] = previous
        self.assertEqual(result.error_kind, "codex_disabled")  # Independent authorization precedes budget.
        self.assertFalse(result.invoked)


class RegistryTests(unittest.TestCase):
    def test_register_update_disable_and_history(self) -> None:
        with support.TempSpace() as root:
            left = root / "left"
            right = root / "right"
            support.prepare_workspace(left)
            support.prepare_workspace(right)
            registry = root / "projects"
            source = root / "incoming"
            source.mkdir()
            register_project(_project_file(source, "alpha", left, name="甲"), registry)
            register_project(_project_file(source, "beta", right, name="乙"), registry)
            self.assertEqual([item.project_id for item in list_projects(registry)], ["alpha", "beta"])
            shown = show_project("alpha", registry)
            self.assertEqual(shown.name, "甲")
            self.assertTrue(shown.workspace.endswith("left") or "left" in shown.workspace.lower() or shown.workspace.endswith("left"))
            updated = source / "alpha-new.json"
            dump_json(updated, {"project_id": "alpha", "name": "甲二", "workspace": str(left), "allowed_roots": [str(left)], "participants": shown.to_dict()["participants"]})
            update_project("alpha", updated, registry)
            self.assertEqual(show_project("alpha", registry).name, "甲二")
            disable_project("beta", registry)
            with self.assertRaises(ConfigError):
                build_project_task("beta", {"title": "x", "goal": "y"}, registry)
            with self.assertRaises(ConfigError):
                register_project(_project_file(source, "alpha", left), registry)
            bad = root / "bad.json"
            dump_json(bad, {"project_id": "bad"})
            with self.assertRaises(ConfigError):
                register_project(bad, registry)
            forbidden = root / "修图样本"
            forbidden.mkdir()
            blocked = source / "photo.json"
            dump_json(
                blocked,
                {
                    "project_id": "photo",
                    "name": "不该注册",
                    "workspace": str(forbidden),
                    "allowed_roots": [str(forbidden)],
                    "participants": [{"id": "codex", "executor": "mock", "role": "developer"}],
                },
            )
            with self.assertRaises(ConfigError):
                register_project(blocked, registry)
            task = build_project_task(
                "alpha",
                {
                    "task_id": "alpha-one",
                    "title": "单项目",
                    "goal": "只属于甲",
                    "mock_plan": ["ok"],
                    "flow": ["codex"],
                    "runs_dir": str(root / "runs"),
                },
                registry,
            )
            result = Coordinator().run(task)
            history = project_history("alpha", root / "runs")
            status = project_status("alpha", registry, root / "runs")
        self.assertEqual(result.project_id, "alpha")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["task_id"], "alpha-one")
        self.assertEqual(history[0]["status"], "failed")
        self.assertFalse(history[0]["success"])
        self.assertEqual(status["latest"]["task_id"], "alpha-one")
        self.assertFalse(show_project("beta", registry).enabled)

    def test_projects_do_not_share_context_unless_allowed(self) -> None:
        with support.TempSpace() as root:
            left = root / "left"
            right = root / "right"
            support.prepare_workspace(left)
            support.prepare_workspace(right)
            first = _developer(root, "ctxA", left, ["ok"], project_id="alpha", goal="唯一标记甲-只在项目甲")
            second = _developer(root, "ctxB", right, ["ok"], project_id="beta", goal="唯一标记乙-只在项目乙")
            registry = root / "projects"
            incoming = root / "incoming"
            incoming.mkdir()
            register_project(_project_file(incoming, "alpha", left), registry)
            with self.assertRaises(ConfigError):
                build_project_task(
                    "alpha",
                    {"title": "共享", "goal": "不应允许", "share_context": True, "shared_note": "不要泄露"},
                    registry,
                )
            batch = run_batch([first, second], batch_id="context", max_concurrent=2)
        prompts = []
        for item in batch.tasks:
            assert item.result is not None
            prompts.append((item.result.run_dir / "rounds" / "001" / "prompt.txt").read_text(encoding="utf-8"))
        self.assertIn("唯一标记甲", prompts[0])
        self.assertNotIn("唯一标记乙", prompts[0])
        self.assertIn("唯一标记乙", prompts[1])
        self.assertNotIn("唯一标记甲", prompts[1])
        self.assertIn("不读取其他项目", prompts[0])


class SchedulerTests(unittest.TestCase):
    def _overlap_env(self, path: Path):
        previous = os.environ.get("DUAL_AGENT_OVERLAP_FILE")
        os.environ["DUAL_AGENT_OVERLAP_FILE"] = str(path)
        return previous

    def _restore_env(self, previous: str | None) -> None:
        if previous is None:
            os.environ.pop("DUAL_AGENT_OVERLAP_FILE", None)
        else:
            os.environ["DUAL_AGENT_OVERLAP_FILE"] = previous

    def test_two_projects_overlap_and_logs_stay_separate(self) -> None:
        with support.TempSpace() as root:
            left = root / "left"
            right = root / "right"
            support.prepare_workspace(left)
            support.prepare_workspace(right)
            (left / "说明.txt").write_text("项目甲中文\n", encoding="utf-8", newline="\n")
            (right / "说明.txt").write_text("项目乙中文\n", encoding="utf-8", newline="\n")
            overlap = root / "overlap"
            previous = self._overlap_env(overlap)
            try:
                batch = run_batch(
                    [
                        _developer(root, "projA", left, ["slow_ok"], project_id="alpha", goal="中文目标甲"),
                        _developer(root, "projB", right, ["slow_ok"], project_id="beta", goal="中文目标乙"),
                    ],
                    batch_id="overlap",
                    max_concurrent=2,
                )
            finally:
                self._restore_env(previous)
            spans = _intervals(overlap, ["projA", "projB"])
        self.assertTrue(_overlaps(spans["projA"], spans["projB"]))
        self.assertTrue(batch.overlapped)
        self.assertEqual(batch.peak_running, 2)
        self.assertEqual(batch.call_mode, "模拟调用")
        for item in batch.tasks:
            assert item.result is not None
            self.assertEqual(item.result.stop_reason, "flow_complete")
            self.assertEqual(item.state, "failed")
            self.assertFalse(item.result.success)
            text = (item.result.run_dir / "messages.jsonl").read_text(encoding="utf-8")
            self.assertIn(item.result.task_id, text)
            self.assertEqual(item.result.messages[0].content_sha256, content_hash(item.result.messages[0].content))
            self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in item.result.messages[0].content))
        goals = [item.result.goal for item in batch.tasks if item.result is not None]
        self.assertEqual(set(goals), {"中文目标甲", "中文目标乙"})
        self.assertTrue(batch.report_path.is_file())
        report = batch.report_path.read_text(encoding="utf-8")
        self.assertIn("模拟调用", report)
        self.assertIn("时间重叠：是", report)

    def test_same_project_and_same_workspace_are_serial(self) -> None:
        with support.TempSpace() as root:
            one = root / "one"
            two = root / "two"
            shared = root / "shared"
            support.prepare_workspace(one)
            support.prepare_workspace(two)
            support.prepare_workspace(shared)
            overlap = root / "overlap"
            previous = self._overlap_env(overlap)
            try:
                same_project = run_batch(
                    [
                        _developer(root, "sameA", one, ["slow_ok"], project_id="alpha"),
                        _developer(root, "sameB", two, ["slow_ok"], project_id="alpha"),
                    ],
                    batch_id="same-project",
                )
                same_workspace = run_batch(
                    [
                        _developer(root, "wsA", shared, ["slow_ok"], project_id="alpha"),
                        _developer(root, "wsB", shared, ["slow_ok"], project_id="beta"),
                    ],
                    batch_id="same-workspace",
                )
            finally:
                self._restore_env(previous)
        project_spans = _intervals(overlap, ["sameA", "sameB"])
        workspace_spans = _intervals(overlap, ["wsA", "wsB"])
        self.assertFalse(_overlaps(project_spans["sameA"], project_spans["sameB"]))
        self.assertFalse(_overlaps(workspace_spans["wsA"], workspace_spans["wsB"]))
        self.assertFalse(same_project.overlapped)
        self.assertFalse(same_workspace.overlapped)
        self.assertTrue(all(item.result is not None and item.result.stop_reason == "flow_complete" for item in same_project.tasks))
        self.assertTrue(all(item.result is not None and item.result.stop_reason == "flow_complete" for item in same_workspace.tasks))

    def test_queue_keeps_peak_at_two(self) -> None:
        with support.TempSpace() as root:
            workspaces = []
            tasks = []
            for name in ("q1", "q2", "q3"):
                workspace = root / name
                support.prepare_workspace(workspace)
                workspaces.append(workspace)
                tasks.append(_developer(root, name, workspace, ["slow_ok"], project_id=name))
            overlap = root / "overlap"
            previous = self._overlap_env(overlap)
            try:
                batch = run_batch(tasks, batch_id="queue", max_concurrent=2)
            finally:
                self._restore_env(previous)
            spans = list(_intervals(overlap, ["q1", "q2", "q3"]).values())
        self.assertEqual(batch.peak_running, 2)
        self.assertTrue(batch.overlapped)
        self.assertEqual(len(spans), 3)
        self.assertTrue(any(_overlaps(spans[i], spans[j]) for i in range(3) for j in range(i + 1, 3)))
        events = []
        for start, end in spans:
            events.append((start, 1))
            events.append((end, -1))
        events.sort(key=lambda item: (item[0], item[1]))
        current = best = 0
        for _moment, delta in events:
            current += delta
            best = max(best, current)
        self.assertLessEqual(best, 2)
        with self.assertRaises(ConfigError):
            run_batch(tasks[:1], max_concurrent=3)

    def test_one_failure_does_not_stop_the_other(self) -> None:
        with support.TempSpace() as root:
            left = root / "left"
            right = root / "right"
            support.prepare_workspace(left)
            support.prepare_workspace(right)
            overlap = root / "overlap"
            previous = self._overlap_env(overlap)
            try:
                batch = run_batch(
                    [
                        _developer(root, "bad", left, ["nonzero"], project_id="alpha"),
                        _developer(root, "good", right, ["slow_ok"], project_id="beta"),
                    ],
                    batch_id="isolate",
                )
                quota = run_batch(
                    [
                        _developer(root, "quotaTask", left, ["quota"], project_id="alpha"),
                        _developer(root, "netTask", right, ["network"], project_id="beta"),
                    ],
                    batch_id="quota-net",
                )
                broken = run_batch(
                    [
                        _developer(root, "protocol", left, ["bad_json"], project_id="alpha"),
                        _developer(root, "alive", right, ["ok"], project_id="beta"),
                    ],
                    batch_id="protocol",
                )
            finally:
                self._restore_env(previous)
        by_id = {item.config.task_id: item for item in batch.tasks}
        self.assertEqual(by_id["bad"].result.stop_reason, "nonzero")
        self.assertEqual(by_id["good"].result.stop_reason, "flow_complete")
        self.assertEqual(by_id["bad"].state, "failed")
        self.assertNotEqual(by_id["good"].state, "cancelled")
        self.assertTrue(
            _overlaps(
                (by_id["bad"].started_perf, by_id["bad"].finished_perf),
                (by_id["good"].started_perf, by_id["good"].finished_perf),
            )
        )
        self.assertTrue(Path(f"{overlap}.good").is_file())
        reasons = {item.result.stop_reason for item in quota.tasks if item.result is not None}
        self.assertEqual(reasons, {"quota", "network"})
        self.assertTrue(all(item.result is not None and item.result.retried is False and item.result.rounds_executed == 1 for item in quota.tasks))
        protocol = {item.config.task_id: item.result.stop_reason for item in broken.tasks if item.result is not None}
        self.assertEqual(protocol["protocol"], "invalid_protocol")
        self.assertEqual(protocol["alive"], "flow_complete")

    def test_resource_lock_serializes_without_connecting(self) -> None:
        with support.TempSpace() as root:
            left = root / "left"
            right = root / "right"
            support.prepare_workspace(left)
            support.prepare_workspace(right)
            overlap = root / "overlap"
            previous = self._overlap_env(overlap)
            try:
                batch = run_batch(
                    [
                        _developer(root, "resA", left, ["slow_ok"], project_id="alpha", resource_locks=["mysql:demo"]),
                        _developer(root, "resB", right, ["slow_ok"], project_id="beta", resource_locks=["mysql:demo"]),
                    ],
                    batch_id="resource",
                )
            finally:
                self._restore_env(previous)
            spans = _intervals(overlap, ["resA", "resB"])
        self.assertFalse(_overlaps(spans["resA"], spans["resB"]))
        self.assertFalse(batch.overlapped)
        self.assertTrue(all(item.result is not None and item.result.lock_released for item in batch.tasks))

    def test_timeout_does_not_take_the_other_task_or_its_lock(self) -> None:
        with support.TempSpace() as root:
            left = root / "left"
            right = root / "right"
            support.prepare_workspace(left)
            support.prepare_workspace(right)
            batch = run_batch(
                [
                    _developer(root, "sleeper", left, ["sleep"], project_id="alpha", timeout_seconds=1),
                    _developer(root, "peer", right, ["ok"], project_id="beta"),
                ],
                batch_id="timeouts",
            )
        by_id = {item.config.task_id: item for item in batch.tasks}
        self.assertEqual(by_id["sleeper"].result.stop_reason, "timeout")
        self.assertTrue(by_id["sleeper"].result.processes_cleared)
        self.assertTrue(by_id["sleeper"].result.lock_released)
        self.assertEqual(by_id["peer"].result.stop_reason, "flow_complete")
        self.assertLess(by_id["sleeper"].result.duration_seconds, 12)

    def test_exception_in_one_task_is_failed_and_not_verified(self) -> None:
        with support.TempSpace() as root:
            left = root / "left"
            right = root / "right"
            support.prepare_workspace(left)
            support.prepare_workspace(right)
            real = Coordinator.run

            def wrapped(self, config):
                if config.task_id == "boom":
                    raise RuntimeError("boom")
                return real(self, config)

            with patch("dual_agent.scheduler.Coordinator.run", wrapped):
                batch = run_batch(
                    [
                        _developer(root, "boom", left, ["ok"], project_id="alpha"),
                        _developer(root, "steady", right, ["ok"], project_id="beta"),
                    ],
                    batch_id="exception",
                )
        by_id = {item.config.task_id: item for item in batch.tasks}
        self.assertEqual(by_id["boom"].state, "failed")
        self.assertIsNone(by_id["boom"].result)
        self.assertIn("boom", by_id["boom"].error)
        self.assertEqual(by_id["steady"].result.stop_reason, "flow_complete")
        self.assertNotEqual(by_id["steady"].state, "verified")

    def test_cancel_queued_task(self) -> None:
        with support.TempSpace() as root:
            tasks = []
            for name in ("c1", "c2", "c3"):
                workspace = root / name
                support.prepare_workspace(workspace)
                tasks.append(_developer(root, name, workspace, ["sleep"], project_id=name, timeout_seconds=4))
            holder: dict[str, object] = {}

            def run() -> None:
                holder["batch"] = run_batch(tasks, batch_id="cancel", max_concurrent=2)

            thread = threading.Thread(target=run)
            thread.start()
            queued = ""
            deadline = time.time() + 8
            while time.time() < deadline and not queued:
                for path in (root / "runs" / "schedule").glob("sch-*.json"):
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("state") == "queued":
                        queued = str(data["schedule_id"])
                        break
                time.sleep(0.05)
            self.assertTrue(queued, "没有观察到排队任务")
            message = cancel_schedule(queued, root / "runs")
            thread.join(20)
            batch = holder["batch"]
        self.assertIn("取消", message)
        cancelled = [item for item in batch.tasks if item.schedule_id == queued]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].state, "cancelled")
        self.assertIsNone(cancelled[0].result)
        self.assertEqual(sum(item.state == "cancelled" for item in batch.tasks), 1)

    def test_verified_and_repair_status_flow(self) -> None:
        with support.TempSpace() as root:
            workspace = root / "workspace"
            support.prepare_workspace(workspace)
            (workspace / "说明.txt").write_text("测试工作区：中文\n", encoding="utf-8", newline="\n")
            verified = support.make_task(
                root,
                task_id="goes-verified",
                workspace=str(workspace),
                write_paths=[str(workspace)],
                project_id="alpha",
                participants=[
                    {"id": "codex", "executor": "mock", "role": "developer"},
                    {"id": "acc", "executor": "mock", "role": "acceptor"},
                ],
                flow=["codex", "acc"],
                mock_plan=["ok", "accept"],
                verification=[{"type": "file_contains", "path": str(workspace / "说明.txt"), "text": "测试工作区"}],
            )
            batch = run_batch([verified], batch_id="verified-flow", max_concurrent=1)
            phases = batch.tasks[0].phases
            result = batch.tasks[0].result
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.stop_reason, "verified")
        self.assertEqual(batch.tasks[0].state, "verified")
        self.assertIn("running", phases)
        self.assertIn("reviewing", phases)
        self.assertTrue(result.success)


class WorktreeTests(unittest.TestCase):
    def test_worktree_keeps_original_head_and_rejects_existing_path(self) -> None:
        with support.TempSpace() as root:
            repo = root / "repo"
            head = _init_repo(repo)
            destination = root / "wt" / "job"
            config = support.make_task(
                root,
                task_id="wtjob",
                workspace=str(repo),
                allowed_roots=[str(repo), str(root)],
                write_paths=[str(repo)],
                participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                flow=["codex"],
                mock_plan=["write_value"],
                schedule_id="job",
                worktree={"enabled": True, "base_ref": "HEAD", "root": str(root / "wt"), "path": str(destination)},
            )
            result = Coordinator().run(config)
            after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result.stop_reason, "flow_complete", result.error_message)
        self.assertEqual(after, head)
        self.assertEqual((repo / "keep.txt").read_text(encoding="utf-8"), "baseline\n")
        self.assertFalse((repo / "value.txt").exists())
        self.assertEqual((destination / "value.txt").read_text(encoding="utf-8"), "5\n")
        self.assertTrue((result.run_dir / "worktree.diff").is_file())
        self.assertTrue((result.run_dir / "worktree.json").is_file())
        with self.assertRaises(WorktreeError):
            prepare_worktree_config(
                support.make_task(
                    root,
                    task_id="wtjob2",
                    workspace=str(repo),
                    allowed_roots=[str(repo), str(root)],
                    schedule_id="job2",
                    worktree={"enabled": True, "base_ref": "HEAD", "path": str(destination)},
                )
            )

    def test_different_worktrees_overlap_and_same_worktree_does_not(self) -> None:
        with support.TempSpace() as root:
            repo = root / "repo"
            _init_repo(repo)
            overlap = root / "overlap"
            previous = os.environ.get("DUAL_AGENT_OVERLAP_FILE")
            os.environ["DUAL_AGENT_OVERLAP_FILE"] = str(overlap)
            try:
                parallel = run_batch(
                    [
                        support.make_task(
                            root,
                            task_id="treeA",
                            project_id="alpha",
                            workspace=str(repo),
                            allowed_roots=[str(repo), str(root)],
                            write_paths=[str(repo)],
                            participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                            flow=["codex"],
                            mock_plan=["slow_ok"],
                            timeout_seconds=8,
                            worktree={"enabled": True, "base_ref": "HEAD", "root": str(root / "trees")},
                        ),
                        support.make_task(
                            root,
                            task_id="treeB",
                            project_id="beta",
                            workspace=str(repo),
                            allowed_roots=[str(repo), str(root)],
                            write_paths=[str(repo)],
                            participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                            flow=["codex"],
                            mock_plan=["slow_ok"],
                            timeout_seconds=8,
                            worktree={"enabled": True, "base_ref": "HEAD", "root": str(root / "trees")},
                        ),
                    ],
                    batch_id="trees",
                )
                shared = root / "same-tree"
                serial = run_batch(
                    [
                        support.make_task(
                            root,
                            task_id="same1",
                            project_id="alpha",
                            workspace=str(repo),
                            allowed_roots=[str(repo), str(root)],
                            write_paths=[str(repo)],
                            participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                            flow=["codex"],
                            mock_plan=["slow_ok"],
                            timeout_seconds=8,
                            max_write_tasks=2,
                            worktree={"enabled": True, "base_ref": "HEAD", "path": str(shared)},
                        ),
                        support.make_task(
                            root,
                            task_id="same2",
                            project_id="beta",
                            workspace=str(repo),
                            allowed_roots=[str(repo), str(root)],
                            write_paths=[str(repo)],
                            participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                            flow=["codex"],
                            mock_plan=["slow_ok"],
                            timeout_seconds=8,
                            max_write_tasks=2,
                            worktree={"enabled": True, "base_ref": "HEAD", "path": str(shared)},
                        ),
                    ],
                    batch_id="same-tree",
                )
            finally:
                if previous is None:
                    os.environ.pop("DUAL_AGENT_OVERLAP_FILE", None)
                else:
                    os.environ["DUAL_AGENT_OVERLAP_FILE"] = previous
            spans = _intervals(overlap, ["treeA", "treeB"])
        self.assertTrue(_overlaps(spans["treeA"], spans["treeB"]))
        self.assertTrue(parallel.overlapped)
        self.assertEqual(_git(repo, "rev-parse", "HEAD").returncode, 0)
        self.assertEqual((repo / "keep.txt").read_text(encoding="utf-8"), "baseline\n")
        reasons = {item.config.task_id: item.result.stop_reason if item.result else item.state for item in serial.tasks}
        self.assertIn("flow_complete", reasons.values())
        self.assertIn("invalid_path", reasons.values())
        self.assertFalse(serial.overlapped)

    def test_junction_worktree_is_rejected(self) -> None:
        if os.name != "nt":
            self.skipTest("仅 Windows")
        with support.TempSpace() as root:
            repo = root / "repo"
            _init_repo(repo)
            link = root / "link"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link), r"C:\Windows"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr.decode("utf-8", "replace"))
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="junctionwt",
                    workspace=str(repo),
                    allowed_roots=[str(repo), str(root)],
                    participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                    flow=["codex"],
                    mock_plan=["ok"],
                    schedule_id="junctionwt",
                    worktree={"enabled": True, "base_ref": "HEAD", "path": str(link)},
                )
            )
        self.assertEqual(result.stop_reason, "invalid_path", result.error_message)
        self.assertTrue("越过" in result.error_message or "重解析" in result.error_message, result.error_message)
        self.assertEqual(result.rounds_executed, 0)
        self.assertFalse(result.success)


class CliTests(unittest.TestCase):
    def test_project_cli_list_and_disabled_task(self) -> None:
        with support.TempSpace() as root:
            workspace = root / "workspace"
            support.prepare_workspace(workspace)
            registry = root / "projects"
            incoming = root / "incoming"
            incoming.mkdir()
            path = _project_file(incoming, "cli-demo", workspace, name="命令行项目")
            self.assertEqual(main(["project", "register", "--file", str(path), "--root", str(registry)]), 0)
            self.assertEqual(main(["project", "list", "--root", str(registry)]), 0)
            self.assertEqual(main(["project", "disable", "cli-demo", "--root", str(registry)]), 0)
            code = main(
                [
                    "project",
                    "task",
                    "cli-demo",
                    "--title",
                    "不会运行",
                    "--goal",
                    "禁用后拒绝",
                    "--root",
                    str(registry),
                ]
            )
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
