"""图形控制台与协调器的集成测试。Agent 调用只走模拟执行器。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from dual_agent.gui.adapter import ConsoleAdapter, _real_call_limits
from dual_agent.gui.outcome import (
    CANCELLED_LABEL,
    FAILED_LABEL,
    FLOW_LABEL,
    REVIEW_LABEL,
    VERIFIED_LABEL,
    presentation,
)
from dual_agent.gui.window import MainWindow
from dual_agent.models import ConfigError, parse_task

_APP = QApplication.instance() or QApplication([])
_WATCH = [ROOT / "projects" / "order-amount.json", ROOT / "projects" / "inventory.json"]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_BEFORE = {path: _digest(path) for path in _WATCH if path.is_file()}


def tearDownModule() -> None:
    for path, digest in _BEFORE.items():
        current = _digest(path)
        if current != digest:
            raise AssertionError(f"测试改动了已有项目配置：{path}")


class OutcomeTests(unittest.TestCase):
    def test_stop_reason_is_not_collapsed_to_success_or_failure(self) -> None:
        readonly = {
            "participants": [
                {"id": "codex", "role": "diagnoser"},
                {"id": "cursor", "role": "reviewer"},
            ],
            "flow": ["codex", "cursor"],
        }
        developer = {"participants": [{"id": "codex", "role": "developer"}], "flow": ["codex"]}
        review = presentation("failed", "flow_complete", success=False, task=readonly)
        self.assertEqual(review["label"], REVIEW_LABEL)
        self.assertFalse(review["passed"])
        finished = presentation("failed", "flow_complete", success=False, task=developer)
        self.assertEqual(finished["label"], FLOW_LABEL)
        self.assertFalse(finished["passed"])
        verified = presentation("verified", "verified", success=True, task=developer)
        self.assertEqual(verified["label"], VERIFIED_LABEL)
        self.assertTrue(verified["passed"])
        failed = presentation("failed", "invalid_protocol", success=False)
        self.assertEqual(failed["label"], FAILED_LABEL)
        self.assertFalse(failed["passed"])
        cancelled = presentation("cancelled", "cancelled", success=False)
        self.assertEqual(cancelled["label"], CANCELLED_LABEL)
        self.assertFalse(cancelled["passed"])
        lied = presentation("verified", "flow_complete", success=True, task=developer)
        self.assertFalse(lied["passed"])
        self.assertNotEqual(lied["label"], VERIFIED_LABEL)

    def test_real_call_budget_follows_flow_steps(self) -> None:
        develop = parse_task(
            {
                "task_id": "budget-dev",
                "title": "预算",
                "goal": "只检查额度计算",
                "workspace": "E:/unused",
                "participants": [
                    {"id": "codex", "executor": "codex", "role": "developer"},
                    {"id": "cursor", "executor": "cursor", "role": "reviewer"},
                    {"id": "acceptor", "executor": "cursor", "role": "acceptor"},
                ],
                "flow": ["codex", "cursor", "acceptor"],
                "max_model_calls": 8,
                "max_repair_rounds": 0,
                "write_paths": [],
            },
            source="budget",
        )
        self.assertEqual(_real_call_limits([develop]), (1, 2))
        review = parse_task(
            {
                "task_id": "budget-review",
                "title": "审查",
                "goal": "只检查额度计算",
                "workspace": "E:/unused",
                "participants": [
                    {"id": "codex", "executor": "codex", "role": "diagnoser"},
                    {"id": "cursor", "executor": "cursor", "role": "reviewer"},
                ],
                "flow": ["codex", "cursor"],
                "max_model_calls": 8,
                "max_repair_rounds": 1,
                "write_paths": [],
            },
            source="budget",
        )
        self.assertEqual(_real_call_limits([review]), (1, 1))


class GuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.space = support.TempSpace()
        self.root = self.space.path
        self.workspace = self.root / "workspace"
        support.prepare_workspace(self.workspace)
        self.projects = self.root / "projects"
        self.runs = self.root / "runs"
        self.adapter = ConsoleAdapter(self.projects, self.runs)
        self.window = MainWindow(self.adapter)
        self.window.interactive = False
        self.window.show()
        QTest.qWait(50)

    def tearDown(self) -> None:
        self._stop_processes()
        self.window.close()
        QTest.qWait(30)

    def _stop_processes(self) -> None:
        for item in list(self.adapter.processes):
            proc = item["popen"]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            handle = item.get("log_handle")
            if handle is not None and not handle.closed:
                handle.close()

    def wait_until(self, predicate, timeout: float = 25) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            QTest.qWait(200)
            if predicate():
                return True
        return False

    def _project_payload(self, project_id: str, workspace: Path, **extra: object) -> dict:
        data: dict[str, object] = {
            "project_id": project_id,
            "name": "示例项目",
            "workspace": str(workspace),
            "allowed_roots": [str(workspace)],
            "enabled": True,
            "participants": [
                {"id": "codex", "executor": "mock", "role": "developer"},
                {"id": "cursor", "executor": "mock", "role": "reviewer"},
                {"id": "acceptor", "executor": "mock", "role": "acceptor"},
            ],
            "verification": [{"type": "file_exists", "path": str(workspace / "说明.txt")}],
            "max_repair_rounds": 1,
            "max_model_calls": 8,
            "max_run_seconds": 120,
            "timeout_seconds": 60,
            "max_rounds": 6,
            "resource_locks": [],
            "worktree": {"enabled": False, "base_ref": "HEAD", "root": "", "path": ""},
            "description": "图形界面测试项目",
            "concurrency": {"max_write_tasks": 1, "allow_shared_context": False},
        }
        data.update(extra)
        return data

    def _register(self, project_id: str = "demo", workspace: Path | None = None, **extra: object) -> None:
        target = workspace or self.workspace
        if not (target / "说明.txt").is_file():
            support.prepare_workspace(target)
        self.window.save_project_payload(self._project_payload(project_id, target, **extra), None)
        self.assertTrue(self.wait_until(lambda: self.window.select_project(project_id)))
        self.assertTrue(self.wait_until(lambda: self.window.role_list.count() >= 1))

    def test_dialog_registers_edits_and_disables_project(self) -> None:
        self.window.add_project()
        dialog = self.window._test_dialog
        dialog.id_edit.setText("demo")
        dialog.name_edit.setText("界面项目")
        dialog.workspace_edit.setText(str(self.workspace))
        self.assertIn("不是操作系统沙箱", dialog.worktree_hint.text())
        self.assertTrue(dialog.try_save())
        self.assertTrue(self.wait_until(lambda: self.window.select_project("demo")))
        self.window.edit_selected()
        editor = self.window._test_dialog
        editor.name_edit.setText("界面项目二")
        self.assertTrue(editor.try_save())
        self.assertTrue(self.wait_until(lambda: (self.window.current_project() or {}).get("name") == "界面项目二"))
        self.window.disable_button.click()
        self.assertTrue(self.wait_until(lambda: (self.window.current_project() or {}).get("enabled") is False))
        blocked = self.root / "修图样本"
        blocked.mkdir()
        payload = self._project_payload("photo", blocked)
        with self.assertRaises(ConfigError):
            self.window.save_project_payload(payload, None)
        self.assertNotIn("photo", self.window._projects)

    def test_mock_flow_shows_real_result_without_shell(self) -> None:
        self._register()
        goal = '请保留中文说明，不要执行 cmd /c whoami & calc.exe "引号"'
        previous = os.environ.get("DUAL_AGENT_ALLOW_CODEX")
        os.environ["DUAL_AGENT_ALLOW_CODEX"] = "1"
        try:
            self.window.title_edit.setText("验收示例")
            self.window.goal_edit.setPlainText(goal)
            self.window.mock_edit.setText("ok,ok,accept")
            self.window.write_read.setChecked(True)
            self.window.start_button.click()
            self.assertTrue(
                self.wait_until(
                    lambda: "当前阶段：可信验证通过" in self.window.phase_label.text()
                    and "验收通过" in self.window.transcript()
                    and "结论：可信验证通过" in self.window.report_view.toPlainText(),
                    40,
                ),
                self.window.phase_label.text() + "\n" + self.window.transcript() + "\n" + self.window.report_view.toPlainText()[:500],
            )
        finally:
            if previous is None:
                os.environ.pop("DUAL_AGENT_ALLOW_CODEX", None)
            else:
                os.environ["DUAL_AGENT_ALLOW_CODEX"] = previous
        argv = self.adapter.last_argv
        self.assertNotIn(goal, argv)
        self.assertNotIn("whoami", " ".join(argv))
        self.assertFalse(any(part == "shell" for part in argv))
        self.assertIsNone(self.adapter.last_child_env.get("DUAL_AGENT_ALLOW_CODEX"))
        batch = json.loads(self.adapter.last_batch_path.read_text(encoding="utf-8"))
        self.assertEqual(batch["tasks"][0]["goal"], goal)
        self.assertFalse(batch["tasks"][0]["allow_real_codex"])
        self.assertFalse(batch["tasks"][0]["allow_real_cursor"])
        self.assertIn("file_exists", self.window.verify_view.toPlainText())
        self.assertIn("进程退出码", self.window.verify_view.toPlainText())
        self.assertIn("不是操作系统沙箱", self.window.worktree_hint.text())
        self.assertIn("不是操作系统沙箱", self.window.permission_view.toPlainText())
        self.assertIn("模拟", self.window.permission_view.toPlainText())

    def test_protocol_error_is_shown_as_failure(self) -> None:
        self._register(
            participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
            verification=[],
        )
        self.window.goal_edit.setPlainText("制造一个协议错误")
        self.window.mock_edit.setText("bad_json")
        self.window.start_button.click()
        self.assertTrue(
            self.wait_until(
                lambda: "执行或验证发生失败" in self.window.phase_label.text()
                and "JSON" in (self.window.transcript() + self.window.log_view.toPlainText() + self.window.report_view.toPlainText()),
                30,
            ),
            self.window.phase_label.text() + "\n" + self.window.log_view.toPlainText()[:800],
        )
        self.assertIn("当前阶段：执行或验证发生失败", self.window.phase_label.text())
        self.assertNotIn("可信验证通过", self.window.phase_label.text())
        self.assertNotIn("审查完成，未进行功能验收", self.window.phase_label.text())
        self.assertIn("结论：未通过", self.window.report_view.toPlainText())

    def test_readonly_review_is_not_shown_as_failure_or_verified(self) -> None:
        self._register(
            participants=[
                {"id": "codex", "executor": "mock", "role": "diagnoser"},
                {"id": "cursor", "executor": "mock", "role": "reviewer"},
            ],
            verification=[],
        )
        self.window.goal_edit.setPlainText("只读说明工作区里的文件，不要修改")
        self.window.mock_edit.setText("ok,ok")
        self.window.start_button.click()
        self.assertTrue(
            self.wait_until(lambda: "审查完成，未进行功能验收" in self.window.phase_label.text(), 30),
            self.window.phase_label.text() + "\n" + self.window.report_view.toPlainText()[:500],
        )
        self.assertNotIn("可信验证通过", self.window.phase_label.text())
        self.assertNotIn("执行或验证发生失败", self.window.phase_label.text())
        self.assertIn("结论：未进行功能验收", self.window.report_view.toPlainText())

    def test_cancel_running_step_stops_before_the_next_role(self) -> None:
        self._register(
            participants=[
                {"id": "codex", "executor": "mock", "role": "developer"},
                {"id": "cursor", "executor": "mock", "role": "reviewer"},
            ],
            verification=[],
        )
        self.window.goal_edit.setPlainText("第一步稍慢，然后取消")
        self.window.mock_edit.setText("slow_ok,ok")
        self.window.start_button.click()
        self.assertTrue(self.wait_until(lambda: self.window.schedule_state(self._only_task()) == "running", 20))
        task_id = self._only_task()
        self.window.select_task_id(task_id)
        self.window.cancel_button.click()
        self.assertTrue(
            self.wait_until(lambda: self.window.schedule_state(task_id) == "cancelled", 20),
            self._debug_state(),
        )
        self.assertIn("用户取消", self.window.phase_label.text())
        self.assertNotIn("可信验证通过", self.window.phase_label.text())

    def test_nonzero_exit_is_failure(self) -> None:
        self._register(participants=[{"id": "codex", "executor": "mock", "role": "developer"}], verification=[])
        self.window.goal_edit.setPlainText("进程异常退出")
        self.window.mock_edit.setText("nonzero")
        self.window.start_button.click()
        self.assertTrue(
            self.wait_until(lambda: "执行或验证发生失败" in self.window.phase_label.text(), 30),
            self.window.phase_label.text(),
        )
        self.assertNotIn("可信验证通过", self.window.report_view.toPlainText())
        self.assertTrue(
            self.wait_until(lambda: self.adapter.processes[0]["popen"].poll() is not None, 15),
            "失败状态已经显示，但协调器进程尚未退出",
        )

    def test_real_switches_record_flow_budget_without_starting_a_model(self) -> None:
        self._register(
            participants=[
                {"id": "codex", "executor": "codex", "role": "developer"},
                {"id": "cursor", "executor": "cursor", "role": "reviewer"},
                {"id": "acceptor", "executor": "cursor", "role": "acceptor"},
            ],
            verification=[],
            max_model_calls=8,
            max_repair_rounds=0,
        )
        self.window.calls_spin.setValue(8)
        self.window.repair_spin.setValue(0)
        self.window.write_controlled.setChecked(True)
        self.window.codex_switch.setChecked(True)
        self.window.cursor_switch.setChecked(True)
        self.window.env_switch.setChecked(True)
        self.window.goal_edit.setPlainText("不要真的调用模型")

        class _Fake:
            pid = 424242

            def poll(self) -> int:
                return 0

        with patch("dual_agent.gui.adapter.subprocess.Popen", return_value=_Fake()):
            self.window.start_button.click()
        batch = json.loads(self.adapter.last_batch_path.read_text(encoding="utf-8"))
        self.assertEqual(batch["codex_call_limit"], 1)
        self.assertEqual(batch["cursor_call_limit"], 2)
        self.assertEqual(self.adapter.last_child_env.get("DUAL_AGENT_ALLOW_CODEX"), "1")
        joined = " ".join(self.adapter.last_argv)
        self.assertNotIn("不要真的调用模型", joined)
        self.assertIn("dual_agent", joined)

    def test_second_start_does_not_create_another_batch(self) -> None:
        self._register(participants=[{"id": "codex", "executor": "mock", "role": "developer"}], verification=[])
        self.window.goal_edit.setPlainText("只提交一次")
        self.window.mock_edit.setText("sleep")
        self.window.start_button.click()
        self.assertTrue(
            self.wait_until(
                lambda: len(self.adapter.processes) == 1 and self.adapter.processes[0]["popen"].poll() is None,
                15,
            )
        )
        self.window.goal_edit.setPlainText("第二次不应该启动")
        self.window.start_current()
        self.assertIn("已有协调器进程", self.window.banner.text())
        self.assertEqual(len(self.adapter.processes), 1)

    def test_restart_rediscovers_task_and_blocks_a_new_batch(self) -> None:
        self._register(participants=[{"id": "codex", "executor": "mock", "role": "developer"}], verification=[])
        self.window.goal_edit.setPlainText("关闭后仍应继续")
        self.window.mock_edit.setText("sleep")
        self.window.start_button.click()
        self.assertTrue(
            self.wait_until(lambda: self.window.schedule_state(self._only_task()) == "running", 20),
            self._debug_state(),
        )
        task_id = self._only_task()
        process = self.adapter.processes[0]["popen"]
        self.window.close()
        revived = ConsoleAdapter(self.projects, self.runs)
        other = MainWindow(revived)
        other.interactive = False
        other.show()
        try:
            self.assertTrue(
                self.wait_until(lambda: other.schedule_state(task_id) == "running", 15),
                other.task_list_text(),
            )
            other.start_current()
            self.assertIn("已有协调器进程", other.banner.text())
            self.assertEqual(revived.processes, [])
            self.assertIsNone(process.poll())
            self.assertNotEqual(other.schedule_state(task_id), "verified")
        finally:
            other.close()

    def test_cancel_queued_task_keeps_running_task(self) -> None:
        self._register()
        self.window.goal_edit.setPlainText("先占用写入锁")
        self.window.mock_edit.setText("sleep")
        self.window.queue_button.click()
        self.assertEqual(len(self.window.pending), 1)
        first = self.window.pending[0]["task_id"]
        self.window.goal_edit.setPlainText("这个任务应该继续排队")
        self.window.mock_edit.setText("ok")
        self.window.queue_button.click()
        second = self.window.pending[1]["task_id"]
        self.window.concurrent_spin.setValue(2)
        self.window.start_button.click()
        self.assertTrue(
            self.wait_until(
                lambda: self.window.schedule_state(first) == "running" and self.window.schedule_state(second) == "queued",
                20,
            ),
            self._debug_state(),
        )
        self.assertTrue(self.window.select_task_id(second))
        self.window.cancel_button.click()
        self.assertTrue(
            self.wait_until(lambda: self.window.schedule_state(second) == "cancelled", 15),
            str([(row.get("task_id"), row.get("state"), row.get("detail")) for row in self.window.latest_schedules]),
        )
        self.assertIn(self.window.schedule_state(first), {"queued", "running", "verifying", "reviewing", "repairing"})
        self.assertNotEqual(self.window.schedule_state(second), "verified")

    def test_close_does_not_mark_success_or_kill_process(self) -> None:
        self._register(participants=[{"id": "codex", "executor": "mock", "role": "developer"}], verification=[])
        self.window.goal_edit.setPlainText("关闭窗口时仍在运行")
        self.window.mock_edit.setText("sleep")
        self.window.start_button.click()
        self.assertTrue(
            self.wait_until(lambda: self.window.schedule_state(self._only_task()) in {"queued", "running"}, 20),
            str(self.window.latest_schedules),
        )
        task_id = self._only_task()
        process = self.adapter.processes[0]["popen"]
        self.window.close()
        self.assertIsNone(process.poll())
        state = self.window.adapter.schedules()
        matched = [row for row in state if row.get("task_id") == task_id]
        self.assertTrue(matched)
        self.assertNotEqual(matched[0].get("state"), "verified")
        self.assertIsNot(matched[0].get("success"), True)
        result_files = list(self.runs.glob(f"{task_id}/**/result.json"))
        for path in result_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNot(data.get("success"), True)

    def test_two_projects_can_run_together(self) -> None:
        left = self.root / "left"
        right = self.root / "right"
        support.prepare_workspace(left)
        support.prepare_workspace(right)
        developer = [{"id": "codex", "executor": "mock", "role": "developer"}]
        self._register("alpha", left, name="甲", participants=developer, verification=[])
        self.window.goal_edit.setPlainText("甲的任务")
        self.window.mock_edit.setText("slow_ok")
        self.window.queue_button.click()
        self._register("beta", right, name="乙", participants=developer, verification=[])
        self.window.goal_edit.setPlainText("乙的任务")
        self.window.mock_edit.setText("slow_ok")
        self.window.queue_button.click()
        self.assertEqual(len(self.window.pending), 2)
        ids = [item["task_id"] for item in self.window.pending]
        self.window.concurrent_spin.setValue(2)
        self.window.start_button.click()
        self.assertTrue(
            self.wait_until(
                lambda: (not self.adapter.has_live_process())
                and self.window.schedule_state(ids[0]) == "failed"
                and self.window.schedule_state(ids[1]) == "failed",
                40,
            ),
            str([(row.get("task_id"), row.get("state")) for row in self.window.latest_schedules]),
        )
        batch = json.loads(self.adapter.last_batch_path.read_text(encoding="utf-8"))
        summary = self.runs / "batches" / batch["batch_id"] / "batch.json"
        self.assertTrue(summary.is_file(), str(summary))
        data = json.loads(summary.read_text(encoding="utf-8"))
        self.assertEqual(data["peak_running"], 2)
        self.assertTrue(data["overlapped"])
        self.assertEqual(data["codex_calls"], 0)
        self.assertEqual(data["cursor_calls"], 0)
        goals = {"甲的任务": ids[0], "乙的任务": ids[1]}
        for goal, task_id in goals.items():
            row = next(item for item in self.window.latest_schedules if item.get("task_id") == task_id)
            text = Path(str(row["run_dir"])).joinpath("messages.jsonl").read_text(encoding="utf-8")
            self.assertIn(goal, text)
            other = "乙的任务" if goal == "甲的任务" else "甲的任务"
            self.assertNotIn(other, text)

    def test_controlled_write_requires_both_switches(self) -> None:
        self._register()
        self.window.goal_edit.setPlainText("不要真的写入")
        self.window.write_controlled.setChecked(True)
        self.window.codex_switch.setChecked(True)
        self.window.env_switch.setChecked(False)
        self.window.start_button.click()
        self.assertIn("没有启动", self.window.banner.text())
        self.assertEqual(self.adapter.processes, [])
        self.assertIn(str(self.workspace), self.window.scope_label.text())

    def test_theme_and_splitter(self) -> None:
        self.window.toggle_theme()
        self.assertEqual(self.window.theme_name, "light")
        self.window.toggle_right()
        self.assertEqual(self.window.splitter.sizes()[2], 0)
        self.window.toggle_right()
        self.assertGreater(self.window.splitter.sizes()[2], 0)
        self.window.toggle_theme()
        self.assertEqual(self.window.theme_name, "dark")

    def test_self_check_exits_cleanly(self) -> None:
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PYTHONUTF8"] = "1"
        env.pop("DUAL_AGENT_ALLOW_CODEX", None)
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "dual_agent.gui", "--self-check"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, (completed.stderr or "")[-2000:] + (completed.stdout or "")[-500:])

    def _debug_state(self) -> str:
        logs = []
        for item in self.adapter.processes:
            code = item["popen"].poll()
            text = ""
            path = item.get("log_path")
            if path and Path(path).is_file():
                text = Path(path).read_text(encoding="utf-8", errors="replace")[-1500:]
            logs.append(f"pid={item['popen'].pid} exit={code} log={text}")
        batch_text = ""
        if self.adapter.last_batch_path and self.adapter.last_batch_path.is_file():
            batch_text = self.adapter.last_batch_path.read_text(encoding="utf-8")
        tree = []
        if self.runs.exists():
            for path in self.runs.rglob("*"):
                if path.is_file() and "schedule" in path.parts and path.suffix == ".json":
                    tree.append(path.name + "=" + path.read_text(encoding="utf-8", errors="replace")[:500])
        direct = []
        try:
            direct = [(row.get("task_id"), row.get("state")) for row in self.adapter.schedules()]
        except Exception as exc:
            direct = [f"schedules-error {exc}"]
        return "\n".join(
            [
                self.window.banner.text(),
                f"cached={[(row.get('task_id'), row.get('state')) for row in self.window.latest_schedules]}",
                f"direct={direct}",
                str(self.adapter.last_argv),
                batch_text,
                "files:",
                *tree[:80],
                *logs,
            ]
        )

    def _only_task(self) -> str:
        for row in self.window.latest_schedules:
            if row.get("task_id"):
                return str(row["task_id"])
        batch = self.adapter.last_batch_path
        if batch and batch.is_file():
            data = json.loads(batch.read_text(encoding="utf-8"))
            return str(data["tasks"][0]["task_id"])
        return ""


if __name__ == "__main__":
    unittest.main()
