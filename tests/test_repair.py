"""功能失败可以在同一任务里反馈并修复。安全失败仍然立即停止。"""

from __future__ import annotations

import hashlib
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
from dual_agent.budget import CallBudget, current_budget, push_budget, reset_budget
from dual_agent.coordinator import Coordinator
from dual_agent.executors import MockExecutor
from dual_agent.models import MessageLog, content_hash

CHECK = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "check_value.py"


def _repair(root: Path, plan: list[str], **overrides: object):
    workspace = root / "workspace"
    support.prepare_workspace(workspace)
    (workspace / "value.txt").write_text("0\n", encoding="utf-8")
    data: dict[str, object] = {
        "task_id": "repair",
        "workspace": str(workspace),
        "write_paths": [str(workspace)],
        "participants": [
            {"id": "codex", "executor": "mock", "role": "developer"},
            {"id": "cursor", "executor": "mock", "role": "reviewer"},
            {"id": "acc", "executor": "mock", "role": "acceptor"},
        ],
        "flow": ["codex", "cursor", "acc"],
        "mock_plan": plan,
        "max_repair_rounds": 1,
        "verification": [
            {
                "type": "command",
                "argv": [sys.executable, str(CHECK), str(workspace / "value.txt"), "5"],
                "expect_exit_code": 0,
                "timeout_seconds": 10,
            },
            {
                "type": "allowed_changes",
                "allowed_paths": ["value.txt"],
                "require_paths": ["value.txt"],
            },
        ],
    }
    data.update(overrides)
    return Coordinator().run(support.make_task(root, **data))


def _preset(root: Path, plan: list[str], starting_value: str = "0\n", **overrides: object):
    workspace = root / "workspace"
    support.prepare_workspace(workspace)
    (workspace / "value.txt").write_text(starting_value, encoding="utf-8")
    data: dict[str, object] = {
        "task_id": "preset",
        "workspace": str(workspace),
        "write_paths": [str(workspace)],
        "participants": [
            {"id": "codex", "executor": "mock", "role": "developer"},
            {"id": "cursor", "executor": "mock", "role": "reviewer"},
            {"id": "acc", "executor": "mock", "role": "acceptor"},
        ],
        "flow": ["codex", "cursor", "acc"],
        "mock_plan": plan,
        "max_repair_rounds": 1,
        "baseline_verify": True,
        "verification": [
            {
                "type": "command",
                "argv": [sys.executable, str(CHECK), str(workspace / "value.txt"), "5"],
                "expect_exit_code": 0,
                "timeout_seconds": 10,
            },
            {
                "type": "allowed_changes",
                "allowed_paths": ["value.txt"],
                "require_paths": ["value.txt"],
            },
        ],
    }
    data.update(overrides)
    return Coordinator().run(support.make_task(root, **data))


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


class RepairLoopTests(unittest.TestCase):
    def test_first_failure_then_fix_is_verified(self) -> None:
        with support.TempSpace() as root:
            result = _repair(root, ["write_wrong", "review_note", "write_fix", "accept"])
            prompt = (result.run_dir / "rounds" / "003" / "prompt.txt").read_text(encoding="utf-8")
            report = result.report_path.read_text(encoding="utf-8")
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.stop_reason, "verified")
        self.assertEqual([message.role for message in result.messages], ["developer", "reviewer", "developer", "acceptor"])
        self.assertEqual(result.messages[2].parent_id, result.messages[1].id)
        self.assertIn("审查反馈：返回值仍然不是预定结果。", prompt)
        self.assertIn(result.messages[1].content, prompt)
        self.assertIn("expected 5 but got 4", prompt)
        self.assertIn("退出码：1", prompt)
        self.assertIn("已完成第 1 轮", prompt)
        reviewer_prompt = (result.run_dir / "rounds" / "002" / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("expected 5 but got 4", reviewer_prompt)
        self.assertIn("退出码：1", reviewer_prompt)
        events = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("repairing", events)
        self.assertIn("verifying", events)
        self.assertIn("源码差异", prompt)
        commands = [item for item in result.verification if item["type"] == "command"]
        self.assertEqual(commands[0]["exit_code"], 1)
        self.assertTrue(commands[0].get("superseded"))
        self.assertEqual(commands[-1]["exit_code"], 0)
        self.assertFalse(commands[-1].get("superseded"))
        self.assertIn("4", (result.run_dir / "rounds" / "001" / "changes.diff").read_text(encoding="utf-8"))
        self.assertIn("5", (result.run_dir / "rounds" / "003" / "changes.diff").read_text(encoding="utf-8"))
        self.assertNotIn("allowed_changes ``", report)
        self.assertNotIn("command ``", report)
        self.assertIn("check_value.py", report)
        self.assertIn("退出码：1", report)
        self.assertIn("退出码：0", report)
        self.assertIn("verification.json", report)
        loaded = MessageLog(result.run_dir / "messages.jsonl").load()
        self.assertEqual(len(loaded), 4)
        for message in loaded:
            self.assertEqual(message.content_sha256, content_hash(message.content))
            self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in message.content))

    def test_first_success_does_not_add_a_repair_call(self) -> None:
        with support.TempSpace() as root:
            result = _repair(root, ["write_fix", "review_note", "accept"])
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.stop_reason, "verified")
        self.assertEqual(result.rounds_executed, 3)
        self.assertEqual(result.repairs_used, 0)
        self.assertFalse((result.run_dir / "rounds" / "004").exists())

    def test_reviewer_rejection_reaches_developer(self) -> None:
        with support.TempSpace() as root:
            result = _repair(root, ["write_wrong", "review_reject", "write_fix", "accept"])
            prompt = (result.run_dir / "rounds" / "003" / "prompt.txt").read_text(encoding="utf-8")
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.messages[1].decision, "reject")
        self.assertIn("拒绝说明：当前结果与可信测试不一致。", prompt)
        self.assertIn("expected 5 but got 4", prompt)
        self.assertEqual(result.messages[2].role, "developer")

    def test_max_repair_stops_without_verified(self) -> None:
        with support.TempSpace() as root:
            result = _repair(root, ["write_wrong", "review_note", "write_other", "accept"])
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "max_repair")
        self.assertEqual(result.rounds_executed, 3)
        self.assertNotIn("accept", [message.decision for message in result.messages])
        commands = [item for item in result.verification if item["type"] == "command"]
        self.assertEqual(commands[-1]["exit_code"], 1)
        self.assertIn("got 3", str(commands[-1]["stderr"]))

    def test_repeated_failure_stops_before_another_repair(self) -> None:
        with support.TempSpace() as root:
            result = _repair(
                root,
                ["write_wrong", "review_note", "write_wrong", "accept"],
                max_repair_rounds=2,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "repeated_failure")
        self.assertEqual(result.rounds_executed, 3)
        self.assertFalse((result.run_dir / "rounds" / "004").exists())

    def test_failed_fix_cannot_become_verified(self) -> None:
        with support.TempSpace() as root:
            result = _repair(
                root,
                ["write_wrong", "review_note", "write_other", "accept"],
                max_model_calls=4,
            )
        self.assertFalse(result.success)
        self.assertNotEqual(result.stop_reason, "verified")
        self.assertTrue(result.retried is False)

    def test_illegal_repair_stops_immediately(self) -> None:
        with support.TempSpace() as root:
            result = _repair(root, ["write_wrong", "review_note", "write_extra", "accept"])
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        self.assertEqual(result.rounds_executed, 3)
        self.assertIn("other.txt", result.error_message)
        self.assertFalse((result.run_dir / "rounds" / "004").exists())

    def test_quota_and_network_are_not_retried(self) -> None:
        with support.TempSpace() as root:
            quota = _repair(root, ["write_wrong", "review_note", "quota", "accept"], task_id="repairquota")
            network = _repair(root, ["write_wrong", "review_note", "network", "accept"], task_id="repairnet")
        self.assertEqual(quota.stop_reason, "quota")
        self.assertEqual(network.stop_reason, "network")
        self.assertEqual(quota.rounds_executed, 3)
        self.assertEqual(network.rounds_executed, 3)
        self.assertFalse(quota.retried)
        self.assertFalse(network.retried)
        self.assertFalse((quota.run_dir / "rounds" / "004").exists())
        self.assertFalse((network.run_dir / "rounds" / "004").exists())

    def test_model_call_budget_blocks_accept(self) -> None:
        with support.TempSpace() as root:
            result = _repair(
                root,
                ["write_wrong", "review_note", "write_fix", "accept"],
                max_model_calls=3,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "budget")
        self.assertEqual(result.rounds_executed, 3)

    def test_run_time_budget_stops_the_task(self) -> None:
        with support.TempSpace() as root:
            result = _repair(
                root,
                ["slow_ok", "review_note", "accept"],
                max_run_seconds=0.2,
                verification=[
                    {
                        "type": "allowed_changes",
                        "allowed_paths": ["value.txt"],
                    }
                ],
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "budget")
        self.assertEqual(result.rounds_executed, 1)
        self.assertTrue(result.processes_cleared)
        self.assertTrue(result.lock_released)

    def test_lock_stays_held_until_repair_finishes(self) -> None:
        with support.TempSpace() as root:
            overlap = root / "overlap"
            previous = os.environ.get("DUAL_AGENT_OVERLAP_FILE")
            os.environ["DUAL_AGENT_OVERLAP_FILE"] = str(overlap)
            holder: dict[str, object] = {}

            def run_holder() -> None:
                holder["result"] = _repair(
                    root,
                    ["write_wrong", "slow_ok", "write_fix", "accept"],
                    task_id="repairhold",
                )

            thread = threading.Thread(target=run_holder)
            thread.start()
            deadline = time.time() + 8
            while time.time() < deadline and not list(root.glob("overlap.*")):
                time.sleep(0.05)
            intruder = Coordinator().run(
                support.make_task(
                    root,
                    task_id="repairintruder",
                    workspace=str(root / "workspace"),
                    write_paths=[str(root / "workspace")],
                    participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                    flow=["codex"],
                    mock_plan=["ok"],
                )
            )
            thread.join(20)
            if previous is None:
                os.environ.pop("DUAL_AGENT_OVERLAP_FILE", None)
            else:
                os.environ["DUAL_AGENT_OVERLAP_FILE"] = previous
        self.assertEqual(intruder.stop_reason, "write_conflict")
        self.assertEqual(intruder.rounds_executed, 0)
        held = holder["result"]
        self.assertTrue(held.success, held.error_message)
        self.assertTrue(held.lock_released)

    def test_preset_failure_reviews_then_repairs_without_fake_history(self) -> None:
        with support.TempSpace() as root:
            result = _preset(root, ["review_note", "write_fix", "accept"])
            reviewer = (result.run_dir / "rounds" / "001" / "prompt.txt").read_text(encoding="utf-8")
            developer = (result.run_dir / "rounds" / "002" / "prompt.txt").read_text(encoding="utf-8")
            acceptor = (result.run_dir / "rounds" / "003" / "prompt.txt").read_text(encoding="utf-8")
            baseline = (result.run_dir / "baseline_verification.json").read_text(encoding="utf-8")
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.stop_reason, "verified")
        self.assertEqual(result.repairs_used, 1)
        self.assertEqual([message.role for message in result.messages], ["reviewer", "developer", "acceptor"])
        self.assertIsNone(result.messages[0].parent_id)
        self.assertEqual(result.messages[1].parent_id, result.messages[0].id)
        self.assertIn("预置失败", reviewer)
        self.assertIn("退出码：1", reviewer)
        self.assertIn("expected 5 but got 0", reviewer)
        self.assertIn("审查反馈：返回值仍然不是预定结果。", developer)
        self.assertIn("expected 5 but got 0", developer)
        self.assertIn("当前预定文件的现有内容", developer)
        self.assertIn("退出码：0", acceptor)
        self.assertNotIn("历史 Agent", result.messages[0].content)
        self.assertIn("baseline", baseline)
        commands = [item for item in result.verification if item["type"] == "command"]
        self.assertEqual(commands[0]["exit_code"], 1)
        self.assertTrue(commands[0].get("baseline"))
        self.assertTrue(commands[0].get("superseded"))
        self.assertEqual(commands[-1]["exit_code"], 0)
        self.assertFalse((result.run_dir / "rounds" / "004").exists())
        loaded = MessageLog(result.run_dir / "messages.jsonl").load()
        self.assertEqual(len(loaded), 3)

    def test_preset_failure_stops_when_baseline_already_passes(self) -> None:
        with support.TempSpace() as root:
            result = _preset(root, ["accept"], starting_value="5\n")
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "baseline_clean")
        self.assertEqual(result.rounds_executed, 0)
        self.assertFalse((result.run_dir / "rounds").exists())

    def test_preset_tampered_script_does_not_call_models(self) -> None:
        with support.TempSpace() as root:
            script = root / "oracle.py"
            script.write_text(CHECK.read_text(encoding="utf-8"), encoding="utf-8")
            pinned = hashlib.sha256(script.read_bytes()).hexdigest()
            script.write_text(script.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
            workspace = root / "workspace"
            result = _preset(
                root,
                ["review_note", "write_fix", "accept"],
                verification=[
                    {
                        "type": "command",
                        "argv": [sys.executable, str(script), str(workspace / "value.txt"), "5"],
                        "sha256": pinned,
                        "expect_exit_code": 0,
                        "timeout_seconds": 10,
                    }
                ],
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        self.assertEqual(result.rounds_executed, 0)
        self.assertIn("摘要", result.error_message)

    def test_preset_same_failure_stops_without_another_repair(self) -> None:
        with support.TempSpace() as root:
            workspace = root / "workspace"
            result = _preset(
                root,
                ["review_note", "ok", "accept"],
                max_repair_rounds=2,
                verification=[
                    {
                        "type": "command",
                        "argv": [sys.executable, str(CHECK), str(workspace / "value.txt"), "5"],
                        "expect_exit_code": 0,
                        "timeout_seconds": 10,
                    },
                    {"type": "allowed_changes", "allowed_paths": ["value.txt"]},
                ],
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "repeated_failure")
        self.assertEqual([message.role for message in result.messages], ["reviewer", "developer"])
        self.assertFalse((result.run_dir / "rounds" / "003").exists())

    def test_preset_reviewer_first_does_not_call_developer_twice(self) -> None:
        with support.TempSpace() as root:
            result = _preset(
                root,
                ["review_note", "write_fix", "accept"],
                flow=["cursor", "codex", "acc"],
            )
        self.assertTrue(result.success, result.error_message)
        self.assertEqual([message.role for message in result.messages], ["reviewer", "developer", "acceptor"])
        self.assertEqual(result.repairs_used, 1)

    def test_preset_keeps_user_file_and_records_git_diff(self) -> None:
        with support.TempSpace() as root:
            workspace = root / "workspace"
            support.prepare_workspace(workspace)
            (workspace / "value.txt").write_text("0\n", encoding="utf-8", newline="\n")
            self.assertEqual(_git(workspace, "init").returncode, 0)
            self.assertEqual(_git(workspace, "add", "value.txt", "说明.txt").returncode, 0)
            self.assertEqual(_git(workspace, "commit", "-m", "buggy baseline").returncode, 0)
            note = workspace / "user-note.txt"
            note.write_text("keep-me\n", encoding="utf-8", newline="\n")
            result = _preset(root, ["review_note", "write_fix", "accept"])
            diff = (result.run_dir / "rounds" / "002" / "changes.diff").read_text(encoding="utf-8")
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(note.read_text(encoding="utf-8"), "keep-me\n")
        self.assertEqual((workspace / "value.txt").read_text(encoding="utf-8"), "5\n")
        self.assertIn("5", diff)
        self.assertIn("0", (result.run_dir / "baseline" / "预设源码.txt").read_text(encoding="utf-8"))

    def test_preset_lock_stays_held_until_repair_finishes(self) -> None:
        with support.TempSpace() as root:
            overlap = root / "overlap"
            previous = os.environ.get("DUAL_AGENT_OVERLAP_FILE")
            os.environ["DUAL_AGENT_OVERLAP_FILE"] = str(overlap)
            holder: dict[str, object] = {}

            def run_holder() -> None:
                holder["result"] = _preset(
                    root,
                    ["slow_ok", "write_fix", "accept"],
                    task_id="presethold",
                )

            thread = threading.Thread(target=run_holder)
            thread.start()
            deadline = time.time() + 8
            while time.time() < deadline and not list(root.glob("overlap.*")):
                time.sleep(0.05)
            intruder = Coordinator().run(
                support.make_task(
                    root,
                    task_id="presetintruder",
                    workspace=str(root / "workspace"),
                    write_paths=[str(root / "workspace")],
                    participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                    flow=["codex"],
                    mock_plan=["ok"],
                )
            )
            thread.join(20)
            if previous is None:
                os.environ.pop("DUAL_AGENT_OVERLAP_FILE", None)
            else:
                os.environ["DUAL_AGENT_OVERLAP_FILE"] = previous
        self.assertEqual(intruder.stop_reason, "write_conflict")
        self.assertEqual(intruder.rounds_executed, 0)
        held = holder["result"]
        self.assertTrue(held.success, held.error_message)
        self.assertTrue(held.lock_released)

    def test_task_call_budget_is_installed_and_cleared(self) -> None:
        seen: list[tuple[int | None, int | None] | None] = []
        original = MockExecutor.run

        def wrapped(executor: MockExecutor, request: object) -> object:
            budget = current_budget()
            seen.append(None if budget is None else (budget.codex_limit, budget.cursor_limit))
            return original(executor, request)

        with support.TempSpace() as root:
            with patch.object(MockExecutor, "run", wrapped):
                result = _preset(
                    root,
                    ["review_note", "write_fix", "accept"],
                    codex_call_limit=2,
                    cursor_call_limit=2,
                )
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(seen, [(2, 2), (2, 2), (2, 2)])
        self.assertIsNone(current_budget())

    def test_task_call_budget_does_not_replace_an_outer_budget(self) -> None:
        outer = CallBudget(1, 1)
        token = push_budget(outer)
        seen: list[CallBudget | None] = []
        original = MockExecutor.run

        def wrapped(executor: MockExecutor, request: object) -> object:
            seen.append(current_budget())
            return original(executor, request)

        try:
            with support.TempSpace() as root:
                with patch.object(MockExecutor, "run", wrapped):
                    result = _preset(
                        root,
                        ["review_note", "write_fix", "accept"],
                        codex_call_limit=9,
                        cursor_call_limit=9,
                    )
        finally:
            reset_budget(token)
        self.assertTrue(result.success, result.error_message)
        self.assertTrue(seen)
        self.assertIs(seen[0], outer)
        self.assertIsNone(current_budget())


if __name__ == "__main__":
    unittest.main()
