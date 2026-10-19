"""交接、多轮、中文和角色测试。全部通过真实协调器，Agent 使用模拟执行器。"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support
from dual_agent import PROJECT_ROOT
from dual_agent.coordinator import Coordinator, verify_saved_run
from dual_agent.models import IntegrityError, content_hash, load_task

GOAL = "验证中文完整性：双Agent协作，甲乙丙，编号①，引号“通过”。"


class FlowTests(unittest.TestCase):
    def test_normal_handoff(self) -> None:
        with support.TempSpace() as root:
            result = Coordinator().run(support.make_task(root, task_id="handoff"))
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "flow_complete")
        self.assertIn("不是任务成功", result.error_message)
        self.assertEqual(result.mode_label, "模拟")
        self.assertFalse(result.retried)
        self.assertEqual([item.sender for item in result.messages], ["codex", "cursor"])
        self.assertEqual(result.messages[1].parent_id, result.messages[0].id)
        self.assertEqual(result.messages[0].receiver, "cursor")
        events = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("run_started", events)
        self.assertIn("run_finished", events)
        self.assertIn("模拟", (result.report_path).read_text(encoding="utf-8"))

    def test_multi_round_roles_and_accept(self) -> None:
        with support.TempSpace() as root:
            evidence = root / "workspace" / "说明.txt"
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="roles",
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "developer"},
                        {"id": "cursor", "executor": "mock", "role": "reviewer"},
                        {"id": "diag", "executor": "mock", "role": "diagnoser"},
                        {"id": "acc", "executor": "mock", "role": "acceptor"},
                    ],
                    flow=["codex", "cursor", "diag", "acc"],
                    mock_plan=["ok", "ok", "ok", "accept"],
                    max_rounds=8,
                    verification=[{"type": "file_contains", "path": str(evidence), "text": "测试工作区"}],
                )
            )
        self.assertTrue(result.success)
        self.assertEqual(result.stop_reason, "verified")
        self.assertTrue(result.verification[0]["ok"])
        self.assertEqual(
            [item.role for item in result.messages],
            ["developer", "reviewer", "diagnoser", "acceptor"],
        )
        self.assertEqual([item.round_index for item in result.messages], [1, 2, 3, 4])
        for previous, current in zip(result.messages, result.messages[1:]):
            self.assertEqual(current.parent_id, previous.id)
        self.assertLess(result.rounds_executed, 8)

    def test_utf8_integrity_across_handoff(self) -> None:
        with support.TempSpace() as root:
            result = Coordinator().run(support.make_task(root, task_id="utf8"))
            report = result.report_path.read_text(encoding="utf-8")
            self.assertIn(GOAL, report)
            self.assertIn(GOAL, result.messages[0].content)
            self.assertIn("已接收目标：", result.messages[0].content)
            digest = hashlib.sha256(result.messages[0].content.encode("utf-8")).hexdigest()
            self.assertEqual(result.messages[0].content_sha256, digest)
            reloaded = verify_saved_run(result.run_dir)
            self.assertEqual(reloaded[0].content, result.messages[0].content)
            prompt = json.loads(
                (result.run_dir / "rounds" / "002" / "request.json").read_text(encoding="utf-8")
            )["prompt"]
            self.assertIn(result.messages[0].content, prompt)
            self.assertIn(GOAL, prompt)
            raw = (result.run_dir / "messages.jsonl").read_bytes()
            self.assertIn("甲乙丙".encode("utf-8"), raw)
            stderr = (result.run_dir / "rounds" / "001" / "stderr.txt").read_text(encoding="utf-8")
            self.assertNotIn("不是合法 UTF-8", stderr)

            log_path = result.run_dir / "messages.jsonl"
            tampered = log_path.read_text(encoding="utf-8").replace("甲", "丁", 1)
            log_path.write_text(tampered, encoding="utf-8", newline="\n")
            with self.assertRaises(IntegrityError):
                verify_saved_run(result.run_dir)
            self.assertEqual(content_hash(result.messages[0].content), digest)

    def test_max_rounds_stops_cycle(self) -> None:
        with support.TempSpace() as root:
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="maxrounds",
                    cycle=True,
                    max_rounds=3,
                    mock_plan=["ok", "ok", "ok"],
                )
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "max_rounds")
        self.assertEqual([item.role for item in result.messages], ["developer", "reviewer", "developer"])
        self.assertEqual(len(list((result.run_dir / "rounds").iterdir())), 3)
        self.assertIn("达到最大协作轮次", result.report_path.read_text(encoding="utf-8"))

    def test_sample_config_and_relative_workspace(self) -> None:
        with support.TempSpace() as root:
            config = load_task(PROJECT_ROOT / "config" / "模拟交接.json")
            config.runs_dir = str(root / "runs")
            result = Coordinator().run(config)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.stop_reason, "verified")
        self.assertEqual(result.mode_label, "模拟")
        self.assertEqual(result.rounds_executed, 3)
        self.assertIn("示例工作区", result.workspace)


if __name__ == "__main__":
    unittest.main()
