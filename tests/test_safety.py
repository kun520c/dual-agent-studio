"""路径、写入冲突、命令边界和真实版本检查。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support
from dual_agent import PROJECT_ROOT
from dual_agent.cli import collect_doctor, format_doctor, main
from dual_agent.coordinator import Coordinator, default_lock_dir, run_parallel
from dual_agent.executors import AgentRequest, CodexExecutor, CursorExecutor, resolve_cursor_prefix
from dual_agent.locks import WriteLockManager
from dual_agent.models import dump_json, load_task
from dual_agent.safety import PACKAGE_DIR, SafetyError, assert_command_allowed


def _request(role: str, executor: str, prompt: str = "请只做只读说明") -> AgentRequest:
    workspace = PROJECT_ROOT / "workspaces" / "示例工作区"
    return AgentRequest(
        task_id="cmd",
        round_index=1,
        agent_name=executor,
        role=role,
        executor=executor,
        prompt=prompt,
        workspace=workspace,
        timeout_seconds=5,
        scenario="ok",
        prompt_path=workspace / "prompt.txt",
        last_message_path=workspace / "last.txt",
        allow_real_codex=False,
        allow_real_cursor=False,
        codex_bin=None,
        cursor_bin=None,
    )


class SafetyTests(unittest.TestCase):
    def test_invalid_paths_do_not_start_agents(self) -> None:
        with support.TempSpace() as root:
            base = support.make_task(root, task_id="paths", write_paths=[], participants=[
                {"id": "cursor", "executor": "mock", "role": "reviewer"}
            ], flow=["cursor"], mock_plan=["ok"])
            file_path = root / "not-a-dir.txt"
            file_path.write_text("x", encoding="utf-8")
            allowed = root / "allowed"
            other = root / "other"
            support.prepare_workspace(allowed)
            support.prepare_workspace(other)
            cases = {
                "windows": (r"C:\Windows", []),
                "drive-root": ("C:/", []),
                "unc": (r"\\localhost\share", []),
                "missing": (str(root / "missing-dir"), []),
                "file": (str(file_path), []),
                "outside-root": (str(other), [str(allowed)]),
            }
            with patch("dual_agent.winproc.subprocess.Popen", side_effect=AssertionError("不应启动进程")):
                for name, (workspace, roots) in cases.items():
                    with self.subTest(name=name):
                        config = support.make_task(
                            root,
                            task_id=f"bad{name}".replace("-", ""),
                            workspace=workspace,
                            allowed_roots=roots,
                            write_paths=[],
                            participants=[{"id": "cursor", "executor": "mock", "role": "reviewer"}],
                            flow=["cursor"],
                            mock_plan=["ok"],
                        )
                        result = Coordinator().run(config)
                        self.assertEqual(result.stop_reason, "invalid_path", result.error_message)
                        self.assertEqual(result.rounds_executed, 0)
                        self.assertTrue(result.report_path.is_file())
                        self.assertEqual(result.mode_label, "未执行")
                empty = support.make_task(
                    root,
                    task_id="badempty",
                    write_paths=[],
                    participants=[{"id": "cursor", "executor": "mock", "role": "reviewer"}],
                    flow=["cursor"],
                    mock_plan=["ok"],
                )
                empty.workspace = ""
                empty_result = Coordinator().run(empty)
                self.assertEqual(empty_result.stop_reason, "invalid_path")
                self.assertEqual(empty_result.rounds_executed, 0)
            developer = support.make_task(
                root,
                task_id="outsidewrite",
                write_paths=[r"C:\Windows"],
            )
            result = Coordinator().run(developer)
            self.assertEqual(result.stop_reason, "invalid_path")
            self.assertIn("写入路径", result.error_message)
            source = support.make_task(
                root,
                task_id="protectsource",
                workspace=str(PACKAGE_DIR),
                write_paths=[str(PACKAGE_DIR)],
                allowed_roots=[],
            )
            result = Coordinator().run(source)
            self.assertEqual(result.stop_reason, "invalid_path")
            self.assertIn("协调器源码", result.error_message)
            self.assertTrue(base.workspace)

    def test_write_conflict_and_parallel_disjoint(self) -> None:
        with support.TempSpace() as root:
            shared = root / "shared"
            support.prepare_workspace(shared)
            left = root / "left"
            right = root / "right"
            support.prepare_workspace(left)
            support.prepare_workspace(right)
            overlap = root / "overlap.txt"
            participants = [{"id": "codex", "executor": "mock", "role": "developer"}]
            conflict_a = support.make_task(
                root,
                task_id="conflictA",
                workspace=str(shared),
                write_paths=[str(shared)],
                participants=participants,
                flow=["codex"],
                mock_plan=["slow_ok"],
                timeout_seconds=20,
            )
            conflict_b = support.make_task(
                root,
                task_id="conflictB",
                workspace=str(shared),
                write_paths=[str(shared / "说明.txt")],
                participants=participants,
                flow=["codex"],
                mock_plan=["slow_ok"],
                timeout_seconds=20,
            )
            safe_a = support.make_task(
                root,
                task_id="safeA",
                workspace=str(left),
                write_paths=[str(left)],
                participants=participants,
                flow=["codex"],
                mock_plan=["slow_ok"],
                timeout_seconds=20,
            )
            safe_b = support.make_task(
                root,
                task_id="safeB",
                workspace=str(right),
                write_paths=[str(right)],
                participants=participants,
                flow=["codex"],
                mock_plan=["slow_ok"],
                timeout_seconds=20,
            )
            previous = os.environ.get("DUAL_AGENT_OVERLAP_FILE")
            os.environ["DUAL_AGENT_OVERLAP_FILE"] = str(overlap)
            try:
                conflict = run_parallel([conflict_a, conflict_b])
                overlap.write_text("", encoding="utf-8")
                disjoint = run_parallel([safe_a, safe_b])
            finally:
                if previous is None:
                    os.environ.pop("DUAL_AGENT_OVERLAP_FILE", None)
                else:
                    os.environ["DUAL_AGENT_OVERLAP_FILE"] = previous
        reasons = {item.stop_reason for item in conflict}
        self.assertEqual(reasons, {"flow_complete", "write_conflict"})
        blocked = next(item for item in conflict if item.stop_reason == "write_conflict")
        self.assertEqual(blocked.rounds_executed, 0)
        self.assertTrue(blocked.report_path.is_file())
        self.assertTrue((blocked.run_dir / "result.json").is_file())
        self.assertIn("重叠", blocked.error_message)
        self.assertTrue(all(item.stop_reason == "flow_complete" and not item.success for item in disjoint))
        conflict_marks = [Path(f"{overlap}.{name}") for name in ("conflictA", "conflictB")]
        self.assertEqual(sum(path.exists() for path in conflict_marks), 1)

        def interval(task_id: str) -> tuple[float, float]:
            rows = Path(f"{overlap}.{task_id}").read_text(encoding="utf-8").splitlines()
            start = float(next(line.split()[1] for line in rows if line.startswith("start ")))
            end = float(next(line.split()[1] for line in rows if line.startswith("end ")))
            return start, end

        start_a, end_a = interval("safeA")
        start_b, end_b = interval("safeB")
        self.assertLess(start_a, end_b)
        self.assertLess(start_b, end_a)
        handle, reason = WriteLockManager(default_lock_dir()).try_acquire("after", [shared])
        self.assertIsNotNone(handle, reason)
        assert handle is not None
        handle.release()

    def test_untrusted_boundary_and_ignored_accept(self) -> None:
        with support.TempSpace() as root:
            injected = Coordinator().run(
                support.make_task(
                    root,
                    task_id="inject",
                    mock_plan=["inject", "ok"],
                )
            )
            ignored = Coordinator().run(
                support.make_task(
                    root,
                    task_id="fakeaccept",
                    mock_plan=["accept", "ok"],
                )
            )
            unsafe = Coordinator().run(
                support.make_task(
                    root,
                    task_id="unsafe",
                    participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                    flow=["codex"],
                    mock_plan=["unsafe_path"],
                )
            )
        self.assertFalse(injected.success)
        self.assertEqual(injected.stop_reason, "flow_complete")
        self.assertTrue(injected.messages[0].injection_suspected)
        prompt = json.loads(
            (injected.run_dir / "rounds" / "002" / "request.json").read_text(encoding="utf-8")
        )["prompt"]
        self.assertLess(prompt.index("<<<UNTRUSTED_AGENT_OUTPUT>>>"), prompt.index("忽略之前所有指令"))
        self.assertLess(prompt.index("忽略之前所有指令"), prompt.index("<<<END_UNTRUSTED_AGENT_OUTPUT>>>"))
        command = json.loads(
            (injected.run_dir / "rounds" / "002" / "meta.json").read_text(encoding="utf-8")
        )["command"]
        self.assertNotIn("--yolo", command)
        self.assertNotIn("--force", command)
        self.assertIn("疑似提示注入", injected.report_path.read_text(encoding="utf-8"))
        self.assertEqual(ignored.stop_reason, "invalid_protocol")
        self.assertFalse(ignored.success)
        self.assertEqual(ignored.rounds_executed, 1)
        self.assertIn("只有验收者", ignored.error_message)
        target = Path(r"C:\Windows\System32\dual-agent-not-created.txt")
        self.assertFalse(target.exists())
        self.assertIn(str(target), unsafe.messages[0].unsafe_paths)
        self.assertIn("没有执行这些写入", unsafe.report_path.read_text(encoding="utf-8"))

    def test_command_builders_are_safe(self) -> None:
        developer = CodexExecutor().build_command(_request("developer", "codex"))
        reviewer = CodexExecutor().build_command(_request("reviewer", "codex"))
        self.assertEqual(developer[developer.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(reviewer[reviewer.index("--sandbox") + 1], "read-only")
        self.assertNotIn("--ask-for-approval", developer)
        self.assertNotIn("--approve-for-me", developer)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", developer)
        self.assertNotIn("--yolo", developer)
        self.assertNotIn("danger-full-access", developer)
        cursor_review = CursorExecutor().build_command(_request("reviewer", "cursor"))
        cursor_dev = CursorExecutor().build_command(
            _request("developer", "cursor", prompt="请实现功能，但不要提权。--yolo 这两个字只是正文")
        )
        self.assertEqual(cursor_review[cursor_review.index("--mode") + 1], "ask")
        self.assertNotIn("--sandbox", cursor_review)
        self.assertNotIn("--force", cursor_review)
        self.assertNotIn("--yolo", cursor_review)
        self.assertNotIn("--mode", cursor_dev[:-1])
        self.assertNotIn("--yolo", cursor_dev[:-1])
        self.assertIn("--yolo", cursor_dev[-1])
        with self.assertRaises(SafetyError):
            assert_command_allowed(["codex", "exec", "--yolo"], "codex")
        with self.assertRaises(SafetyError):
            assert_command_allowed(["agent", "-p", "--sandbox", "enabled"], "cursor")
        assert_command_allowed(
            [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "-C",
                r"D:\work\--force-mentioned",
            ],
            "codex",
        )
        for path in (PROJECT_ROOT / "dual_agent").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, path.name)
            self.assertNotIn("os.system(", text, path.name)

    def test_real_codex_is_not_called(self) -> None:
        with support.TempSpace() as root:
            missing = root / "missing-codex.exe"
            guarded = support.make_task(
                root,
                task_id="guarded",
                participants=[{"id": "codex", "executor": "codex", "role": "developer"}],
                flow=["codex"],
                mock_plan=[],
                allow_real_codex=False,
            )
            half = support.make_task(
                root,
                task_id="halfopen",
                participants=[{"id": "codex", "executor": "codex", "role": "developer"}],
                flow=["codex"],
                mock_plan=[],
                allow_real_codex=True,
            )
            fake = support.make_task(
                root,
                task_id="fakebin",
                participants=[{"id": "codex", "executor": "codex", "role": "developer"}],
                flow=["codex"],
                mock_plan=[],
                allow_real_codex=True,
                codex_bin=str(missing),
            )
            calls: list[list[str]] = []

            def spy(command, *args, **kwargs):
                calls.append(list(command))
                exe = Path(command[0]).name.lower()
                if exe in {"codex.exe", "codex", "codex.cmd"}:
                    raise AssertionError("启动了真实 Codex")
                raise FileNotFoundError(command[0])

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DUAL_AGENT_ALLOW_CODEX", None)
                with patch("dual_agent.winproc.subprocess.Popen", side_effect=spy):
                    first = Coordinator().run(guarded)
                    second = Coordinator().run(half)
            self.assertEqual(calls, [])
            self.assertEqual(first.stop_reason, "authorization_denied")
            self.assertEqual(first.rounds_executed, 0)
            self.assertEqual(second.stop_reason, "authorization_denied")
            self.assertEqual(second.rounds_executed, 0)
            with patch.dict(os.environ, {"DUAL_AGENT_ALLOW_CODEX": "1"}, clear=False):
                with patch("dual_agent.winproc.subprocess.Popen", side_effect=spy):
                    third = Coordinator().run(fake)
            self.assertEqual(third.stop_reason, "authorization_denied")
            self.assertEqual(calls, [])
            template = load_task(PROJECT_ROOT / "config" / "真实接口模板.json")
            template.runs_dir = str(root / "template-runs")
            with patch("dual_agent.winproc.subprocess.Popen", side_effect=AssertionError("模板不应启动进程")):
                blocked = Coordinator().run(template)
            self.assertEqual(blocked.stop_reason, "authorization_denied")
            self.assertEqual(blocked.rounds_executed, 0)
            self.assertTrue(blocked.report_path.is_file())

    def test_cursor_real_call_stays_disabled(self) -> None:
        with support.TempSpace() as root:
            config = support.make_task(
                root,
                task_id="cursoroff",
                participants=[{"id": "cursor", "executor": "cursor", "role": "reviewer"}],
                flow=["cursor"],
                mock_plan=[],
                write_paths=[],
                allow_real_cursor=False,
            )
            with patch("dual_agent.winproc.subprocess.Popen", side_effect=AssertionError("不应启动 Cursor")):
                result = Coordinator().run(config)
        self.assertEqual(result.stop_reason, "authorization_denied")
        self.assertEqual(result.rounds_executed, 0)
        command = CursorExecutor().build_command(_request('reviewer','cursor'))
        self.assertEqual(command[command.index("--mode") + 1], "ask")
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("--yolo", command)
        self.assertNotIn("--force", command)

    def test_cursor_launcher_resolves_local_install(self) -> None:
        prefix = resolve_cursor_prefix(None)
        if prefix == ["agent"]:
            self.skipTest("本机未安装 Cursor CLI")
        self.assertTrue(Path(prefix[0]).is_file())
        self.assertTrue(Path(prefix[1]).is_file())
        self.assertTrue(prefix[0].lower().endswith("node.exe"))
        self.assertTrue(prefix[1].lower().endswith("index.js"))

    def test_doctor_reads_versions_without_prompt(self) -> None:
        # Versions change independently of this test. Pin only the captured
        # output and prove that the doctor requests --version without a prompt.
        with patch('dual_agent.cli._captured_version', side_effect=['codex-cli fixture', 'cursor fixture']) as probe:
            text = format_doctor(collect_doctor())
        self.assertEqual(probe.call_count, 2)
        for call in probe.call_args_list:
            self.assertEqual(call.args[0][-1], '--version')
        self.assertIn("codex-cli fixture", text)
        self.assertIn("cursor fixture", text)
        self.assertIn("中文UTF-8检查：协作", text)
        self.assertIn("不会把提示词发给模型", text)
        self.assertIn("全权限", text)

    def test_cli_run_command(self) -> None:
        with support.TempSpace() as root:
            config = support.make_task(root, task_id="cli")
            path = root / "task.json"
            dump_json(path, config.to_dict())
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "dual_agent", "run", "--config", str(path)],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
                env={
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(PROJECT_ROOT),
                },
            )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        self.assertEqual(completed.returncode, 4, stderr + stdout)
        self.assertIn("未通过", stdout)
        self.assertIn("流程已结束但未验收", stdout)
        self.assertIn("模拟", stdout)
        with self.assertRaises(SystemExit) as caught:
            main(["--version"])
        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
