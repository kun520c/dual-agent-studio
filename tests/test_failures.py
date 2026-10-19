"""故障测试。进程是模拟的，停止、现场保留和分类都走真实协调器。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support
from dual_agent.coordinator import Coordinator
from dual_agent.safety import classify_output


def _one(root: Path, scenario: str, **overrides: object):
    data = {
        "task_id": scenario,
        "participants": [{"id": "codex", "executor": "mock", "role": "developer"}],
        "flow": ["codex"],
        "mock_plan": [scenario],
        "max_rounds": 3,
    }
    data.update(overrides)
    return Coordinator().run(support.make_task(root, **data))


class FailureTests(unittest.TestCase):
    def test_classify_quota_before_network_words(self) -> None:
        kind = classify_output(
            stdout="",
            stderr="insufficient_quota and tls handshake failed",
            exit_code=1,
            timed_out=False,
            garbled=False,
            answer="",
        )
        self.assertEqual(kind, "quota")
        ok = classify_output(
            stdout="中文结果",
            stderr="",
            exit_code=0,
            timed_out=False,
            garbled=False,
            answer="中文结果",
        )
        self.assertEqual(ok, "ok")

    def test_empty_reply(self) -> None:
        with support.TempSpace() as root:
            result = _one(root, "empty")
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "empty")
        self.assertEqual(result.rounds_executed, 1)
        stdout = (result.run_dir / "rounds" / "001" / "stdout.txt").read_text(encoding="utf-8")
        self.assertEqual(stdout.strip(), "")
        self.assertFalse((result.run_dir / "rounds" / "002").exists())

    def test_nonzero_exit(self) -> None:
        with support.TempSpace() as root:
            result = _one(root, "nonzero")
        self.assertEqual(result.stop_reason, "nonzero")
        self.assertEqual(result.messages[0].exit_code, 2)
        self.assertEqual(result.rounds_executed, 1)
        self.assertIn("模拟进程失败", (result.run_dir / "rounds" / "001" / "stderr.txt").read_text(encoding="utf-8"))

    def test_timeout(self) -> None:
        with support.TempSpace() as root:
            result = _one(root, "sleep", timeout_seconds=1)
        self.assertEqual(result.stop_reason, "timeout")
        self.assertIsNone(result.messages[0].exit_code)
        self.assertLess(result.messages[0].duration_seconds, 10)
        self.assertFalse(result.retried)
        self.assertIn("不会重试", result.report_path.read_text(encoding="utf-8"))

    def test_quota_does_not_retry(self) -> None:
        with support.TempSpace() as root:
            result = _one(root, "quota")
        self.assertEqual(result.stop_reason, "quota")
        self.assertFalse(result.retried)
        self.assertEqual(result.rounds_executed, 1)
        stderr = (result.run_dir / "rounds" / "001" / "stderr.txt").read_text(encoding="utf-8")
        self.assertIn("insufficient_quota", stderr)
        meta = json.loads((result.run_dir / "rounds" / "001" / "meta.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["retried"])
        self.assertEqual(meta["attempt"], 1)
        report = result.report_path.read_text(encoding="utf-8")
        self.assertIn("没有重试", report)
        self.assertIn("额度不足", report)

    def test_network_or_tls(self) -> None:
        with support.TempSpace() as root:
            result = _one(root, "network")
        self.assertEqual(result.stop_reason, "network")
        stderr = (result.run_dir / "rounds" / "001" / "stderr.txt").read_text(encoding="utf-8")
        self.assertIn("certificate verify failed", stderr)
        self.assertFalse((result.run_dir / "rounds" / "002").exists())

    def test_garbled_output(self) -> None:
        with support.TempSpace() as root:
            result = _one(root, "garbled")
        self.assertEqual(result.stop_reason, "garbled")
        stdout = (result.run_dir / "rounds" / "001" / "stdout.txt").read_text(encoding="utf-8")
        self.assertIn("\ufffd", stdout)
        self.assertIn("乱码", result.report_path.read_text(encoding="utf-8"))

    def test_failure_preserves_scene(self) -> None:
        with support.TempSpace() as root:
            result = _one(root, "quota", task_id="scene")
            run_dir = result.run_dir
            required = [
                run_dir / "报告.md",
                run_dir / "result.json",
                run_dir / "messages.jsonl",
                run_dir / "events.jsonl",
                run_dir / "task.json",
                run_dir / "rounds" / "001" / "stdout.txt",
                run_dir / "rounds" / "001" / "stderr.txt",
                run_dir / "rounds" / "001" / "answer.txt",
                run_dir / "rounds" / "001" / "meta.json",
                run_dir / "rounds" / "001" / "request.json",
            ]
            missing = [str(path) for path in required if not path.is_file()]
            self.assertEqual(missing, [])
            self.assertTrue(run_dir.exists())
            saved = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["stop_reason"], "quota")
            self.assertFalse(saved["retried"])


if __name__ == "__main__":
    unittest.main()
