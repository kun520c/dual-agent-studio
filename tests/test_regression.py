"""第二阶段回归。模拟进程走真实协调器；Codex 只做离线帮助检查。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support
from dual_agent.coordinator import Coordinator
from dual_agent.locks import pid_alive
from dual_agent.safety import PathRejected, finalize_declared


def _developer(root: Path, **overrides: object):
    data = {
        "participants": [{"id": "codex", "executor": "mock", "role": "developer"}],
        "flow": ["codex"],
        "mock_plan": ["ok"],
    }
    data.update(overrides)
    return Coordinator().run(support.make_task(root, **data))


class RegressionTests(unittest.TestCase):
    def test_flow_complete_and_bare_accept_are_not_success(self) -> None:
        with support.TempSpace() as root:
            finished = _developer(root, task_id="notdone")
            accepted = Coordinator().run(
                support.make_task(
                    root,
                    task_id="bareaccept",
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "developer"},
                        {"id": "acc", "executor": "mock", "role": "acceptor"},
                    ],
                    flow=["codex", "acc"],
                    mock_plan=["ok", "accept"],
                )
            )
            verified = Coordinator().run(
                support.make_task(
                    root,
                    task_id="verified",
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "developer"},
                        {"id": "acc", "executor": "mock", "role": "acceptor"},
                    ],
                    flow=["codex", "acc"],
                    mock_plan=["ok", "accept"],
                    verification=[
                        {
                            "type": "file_contains",
                            "path": str(root / "workspace" / "说明.txt"),
                            "text": "测试工作区",
                        }
                    ],
                )
            )
        self.assertFalse(finished.success)
        self.assertEqual(finished.stop_reason, "flow_complete")
        self.assertFalse(accepted.success)
        self.assertEqual(accepted.stop_reason, "unverified")
        self.assertTrue((accepted.run_dir / "报告.md").is_file())
        self.assertTrue(verified.success)
        self.assertEqual(verified.stop_reason, "verified")
        self.assertTrue((verified.run_dir / "verification.json").is_file())

    def test_invalid_protocol_preserves_scene(self) -> None:
        scenarios = {
            "badjson": "bad_json",
            "missing": "missing_field",
            "baddone": "bad_decision",
            "extra": "extra_field",
            "fenced": "fenced",
        }
        with support.TempSpace() as root:
            for task_id, scenario in scenarios.items():
                with self.subTest(scenario=scenario):
                    result = _developer(root, task_id=task_id, mock_plan=[scenario])
                    self.assertFalse(result.success)
                    self.assertEqual(result.stop_reason, "invalid_protocol")
                    self.assertEqual(result.rounds_executed, 1)
                    round_dir = result.run_dir / "rounds" / "001"
                    for name in ("stdout.txt", "stderr.txt", "answer.txt", "meta.json", "request.json"):
                        self.assertTrue((round_dir / name).is_file(), name)
                    self.assertTrue((result.run_dir / "报告.md").is_file())

    def test_secrets_are_redacted(self) -> None:
        with support.TempSpace() as root:
            result = _developer(root, task_id="leak", mock_plan=["leak"])
        stdout = (result.run_dir / "rounds" / "001" / "stdout.txt").read_text(encoding="utf-8")
        answer = (result.run_dir / "rounds" / "001" / "answer.txt").read_text(encoding="utf-8")
        self.assertNotIn("supersecretvalue", stdout)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", answer)
        self.assertIn("[REDACTED]", stdout)

    def test_private_lock_dir_is_rejected(self) -> None:
        with support.TempSpace() as root:
            first = support.make_task(root, task_id="locka", lock_dir=str(root / "locks-a"))
            second = support.make_task(root, task_id="lockb", lock_dir=str(root / "locks-b"))
            with patch("dual_agent.winproc.subprocess.Popen", side_effect=AssertionError("不应启动")):
                results = [Coordinator().run(first), Coordinator().run(second)]
        for result in results:
            self.assertEqual(result.stop_reason, "invalid_path")
            self.assertIn("独立锁目录", result.error_message)
            self.assertEqual(result.rounds_executed, 0)
            self.assertTrue(result.report_path.is_file())

    def test_junction_escape_is_rejected(self) -> None:
        if os.name != "nt":
            self.skipTest("仅 Windows")
        with support.TempSpace() as root:
            workspace = root / "ws"
            outside = root / "outside"
            support.prepare_workspace(workspace)
            outside.mkdir()
            (outside / "secret.txt").write_text("nope", encoding="utf-8")
            nested = workspace / "a" / "b"
            nested.mkdir(parents=True)
            link = nested / "link"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr.decode("utf-8", "replace"))
            with self.assertRaises(PathRejected):
                finalize_declared(str(link / ".." / "说明.txt"))
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="junction",
                    workspace=str(workspace),
                    write_paths=[str(workspace)],
                    participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                    flow=["codex"],
                    mock_plan=["ok"],
                )
            )
        self.assertEqual(result.stop_reason, "invalid_path", result.error_message)
        self.assertIn("重解析点", result.error_message)
        self.assertEqual(result.rounds_executed, 0)
        self.assertFalse((outside / "secret.txt").read_text(encoding="utf-8") != "nope")

    def test_timeout_clears_descendant_before_lock_release(self) -> None:
        with support.TempSpace() as root:
            marker = root / "ticks.txt"
            previous = os.environ.get("DUAL_AGENT_SPAWN_MARKER")
            os.environ["DUAL_AGENT_SPAWN_MARKER"] = str(marker)
            try:
                result = _developer(root, task_id="spawn", mock_plan=["spawn_sleeper"], timeout_seconds=1)
            finally:
                if previous is None:
                    os.environ.pop("DUAL_AGENT_SPAWN_MARKER", None)
                else:
                    os.environ["DUAL_AGENT_SPAWN_MARKER"] = previous
            self.assertEqual(result.stop_reason, "timeout", result.error_message)
            self.assertLess(result.duration_seconds, 12)
            self.assertTrue(result.processes_cleared)
            self.assertTrue(result.lock_released)
            text = marker.read_text(encoding="utf-8")
            pid = int(text.splitlines()[0].split()[1])
            self.assertFalse(pid_alive(pid))
            time.sleep(0.6)
            self.assertEqual(marker.read_text(encoding="utf-8"), text)
            self.assertTrue((result.run_dir / "rounds" / "001" / "stderr.txt").is_file())

    def test_real_cursor_developer_and_narrow_codex_are_blocked(self) -> None:
        with support.TempSpace() as root:
            workspace = root / "workspace"
            support.prepare_workspace(workspace)
            nested = workspace / "src"
            nested.mkdir()
            cursor_task = support.make_task(
                root,
                task_id="cursordev",
                participants=[{"id": "cursor", "executor": "cursor", "role": "developer"}],
                flow=["cursor"],
                mock_plan=[],
                allow_real_cursor=True,
            )
            codex_task = support.make_task(
                root,
                task_id="narrowcodex",
                participants=[{"id": "codex", "executor": "codex", "role": "developer"}],
                flow=["codex"],
                mock_plan=[],
                write_paths=[str(nested)],
                allow_real_codex=True,
                codex_bin=str(root / "missing-codex.exe"),
            )
            with patch.dict(os.environ, {"DUAL_AGENT_ALLOW_CODEX": "1"}, clear=False):
                with patch("dual_agent.winproc.subprocess.Popen", side_effect=AssertionError("不应启动")):
                    cursor_result = Coordinator().run(cursor_task)
                    codex_result = Coordinator().run(codex_task)
        self.assertEqual(cursor_result.stop_reason, "authorization_denied")
        self.assertEqual(cursor_result.rounds_executed,0)
        self.assertEqual(codex_result.stop_reason, "authorization_denied")
        self.assertEqual(codex_result.rounds_executed,0)
        self.assertTrue(cursor_result.report_path.is_file())
        self.assertTrue(codex_result.report_path.is_file())

    def test_codex_exec_help_matches_builder_flags(self) -> None:
        """离线检查已安装的 Codex CLI。不发送提示词。"""
        exe = shutil.which("codex")
        if not exe:
            self.skipTest("未安装 Codex CLI")
        help_run = subprocess.run(
            [exe, "exec", "--help"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(help_run.returncode, 0)
        text = help_run.stdout.decode("utf-8", errors="replace")
        for flag in ("--json", "--color", "--skip-git-repo-check", "--ephemeral", "--sandbox", "-C", "-o"):
            self.assertIn(flag, text)
        rejected = subprocess.run(
            [exe, "exec", "--ask-for-approval", "never", "--help"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        error = rejected.stderr.decode("utf-8", errors="replace")
        self.assertIn("unexpected argument", error)


if __name__ == "__main__":
    unittest.main()
