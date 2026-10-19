"""命令验证由协调器执行。模型声明通过不能代替退出码。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support
from dual_agent import PROJECT_ROOT
from dual_agent.coordinator import Coordinator

CHECK = PROJECT_ROOT / "tests" / "fixtures" / "check_value.py"


def _flow(root: Path, *, value: str, extra: str | None, verification: list[dict[str, object]]):
    workspace = root / "workspace"
    support.prepare_workspace(workspace)
    (workspace / "value.txt").write_text(value + "\n", encoding="utf-8")
    if extra is not None:
        (workspace / "other.txt").write_text(extra, encoding="utf-8")
    return Coordinator().run(
        support.make_task(
            root,
            task_id="cmdverify",
            workspace=str(workspace),
            write_paths=[str(workspace)],
            participants=[
                {"id": "codex", "executor": "mock", "role": "developer"},
                {"id": "acc", "executor": "mock", "role": "acceptor"},
            ],
            flow=["codex", "acc"],
            mock_plan=["ok", "accept"],
            max_repair_rounds=0,
            verification=verification,
        )
    )


def _checks(root: Path, *, inside_script: bool = False, allowed: list[str] | None = None) -> list[dict[str, object]]:
    script = root / "workspace" / "fake_check.py" if inside_script else CHECK
    if inside_script:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    return [
        {
            "type": "command",
            "argv": [sys.executable, str(script), str(root / "workspace" / "value.txt"), "5"],
            "expect_exit_code": 0,
            "timeout_seconds": 10,
        },
        {"type": "allowed_changes", "allowed_paths": allowed if allowed is not None else ["value.txt"]},
    ]


class CommandVerificationTests(unittest.TestCase):
    def test_model_claim_cannot_override_failing_command(self) -> None:
        with support.TempSpace() as root:
            result = _flow(root, value="0", extra=None, verification=_checks(root))
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        command = next(item for item in result.verification if item["type"] == "command")
        self.assertEqual(command["exit_code"], 1)
        self.assertIn("expected 5 but got 0", str(command["stderr"]))
        self.assertEqual(result.rounds_executed, 1)
        self.assertEqual(result.messages[0].decision, "continue")

    def test_exit_code_and_change_scope_can_verify(self) -> None:
        with support.TempSpace() as root:
            result = _flow(root, value="5", extra=None, verification=_checks(root))
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.stop_reason, "verified")
        command = next(item for item in result.verification if item["type"] == "command")
        self.assertEqual(command["exit_code"], 0)
        self.assertTrue(command["processes_cleared"])

    def test_unexpected_file_change_stops_before_acceptor(self) -> None:
        with support.TempSpace() as root:
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="extrafile",
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "developer"},
                        {"id": "acc", "executor": "mock", "role": "acceptor"},
                    ],
                    flow=["codex", "acc"],
                    mock_plan=["write_extra", "accept"],
                    verification=[{"type": "allowed_changes", "allowed_paths": ["value.txt"]}],
                )
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        self.assertEqual(result.rounds_executed, 1)
        self.assertIn("other.txt", result.error_message)

    def test_script_inside_workspace_is_rejected(self) -> None:
        with support.TempSpace() as root:
            result = _flow(root, value="5", extra=None, verification=_checks(root, inside_script=True))
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        self.assertIn("可写工作区", result.error_message)

    def test_missing_required_edit_stops_before_later_agents(self) -> None:
        with support.TempSpace() as root:
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="noedit",
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "developer"},
                        {"id": "cursor", "executor": "mock", "role": "reviewer"},
                    ],
                    flow=["codex", "cursor"],
                    mock_plan=["ok", "ok"],
                    verification=[
                        {
                            "type": "allowed_changes",
                            "allowed_paths": ["src/Adder.java"],
                            "require_paths": ["src/Adder.java"],
                        }
                    ],
                )
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        self.assertEqual(result.rounds_executed, 1)
        self.assertIn("没有发生修改", result.error_message)

    def test_shell_command_flag_is_rejected(self) -> None:
        with support.TempSpace() as root:
            checks = _checks(root)
            checks[0]["argv"] = ["powershell.exe", "-Command", "exit 0"]
            result = _flow(root, value="5", extra=None, verification=checks)
        self.assertFalse(result.success)
        self.assertIn("-Command", result.error_message)


class AcceptanceOrderTests(unittest.TestCase):
    def _verified_flow(
        self,
        root: Path,
        plan: list[str],
        participants: list[dict[str, str]] | None = None,
        flow: list[str] | None = None,
    ):
        workspace = root / "workspace"
        support.prepare_workspace(workspace)
        (workspace / "value.txt").write_text("0\n", encoding="utf-8")
        return Coordinator().run(
            support.make_task(
                root,
                task_id="acceptorder",
                workspace=str(workspace),
                write_paths=[str(workspace)],
                participants=participants
                or [
                    {"id": "codex", "executor": "mock", "role": "developer"},
                    {"id": "rev", "executor": "mock", "role": "reviewer"},
                    {"id": "acc", "executor": "mock", "role": "acceptor"},
                ],
                flow=flow or ["codex", "rev", "acc"],
                mock_plan=plan,
                verification=[
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
            )
        )

    def test_patch_trusted_test_and_accept_is_verified(self) -> None:
        with support.TempSpace() as root:
            result = self._verified_flow(root, ["write_value", "ok", "accept"])
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.stop_reason, "verified")
        command = next(item for item in result.verification if item["type"] == "command")
        self.assertEqual(command["exit_code"], 0)
        self.assertIn("duration_seconds", command)

    def test_failed_test_plus_accept_is_not_verified(self) -> None:
        with support.TempSpace() as root:
            workspace = root / "workspace"
            support.prepare_workspace(workspace)
            (workspace / "value.txt").write_text("0\n", encoding="utf-8")
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="failaccept",
                    workspace=str(workspace),
                    write_paths=[str(workspace)],
                    max_repair_rounds=0,
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "developer"},
                        {"id": "acc", "executor": "mock", "role": "acceptor"},
                    ],
                    flow=["codex", "acc"],
                    mock_plan=["ok", "accept"],
                    verification=[
                        {
                            "type": "command",
                            "argv": [sys.executable, str(CHECK), str(workspace / "value.txt"), "5"],
                            "expect_exit_code": 0,
                            "timeout_seconds": 10,
                        }
                    ],
                )
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        self.assertEqual(result.rounds_executed, 1)
        self.assertNotEqual(result.messages[0].decision, "accept")
        command = next(item for item in result.verification if item["type"] == "command")
        self.assertEqual(command["exit_code"], 1)

    def test_passing_test_plus_reject_is_not_verified(self) -> None:
        with support.TempSpace() as root:
            result = self._verified_flow(
                root,
                ["write_value", "reject"],
                participants=[
                    {"id": "codex", "executor": "mock", "role": "developer"},
                    {"id": "acc", "executor": "mock", "role": "acceptor"},
                ],
                flow=["codex", "acc"],
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "rejected")
        command = next(item for item in result.verification if item["type"] == "command")
        self.assertEqual(command["exit_code"], 0)

    def test_file_change_after_passing_test_invalidates_evidence(self) -> None:
        with support.TempSpace() as root:
            result = self._verified_flow(root, ["write_value", "spoil_value", "accept"])
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        self.assertIn("证据失效", result.error_message)
        commands = [item for item in result.verification if item["type"] == "command"]
        self.assertEqual(commands[0]["exit_code"], 0)
        self.assertTrue(commands[-1].get("evidence_invalidated"))
        self.assertNotEqual(commands[-1]["exit_code"], 0)

    def test_modified_verification_script_is_rejected(self) -> None:
        with support.TempSpace() as root:
            script = root / "oracle.py"
            script.write_text(CHECK.read_text(encoding="utf-8"), encoding="utf-8")
            pinned = __import__("hashlib").sha256(script.read_bytes()).hexdigest()
            script.write_text(script.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
            workspace = root / "workspace"
            support.prepare_workspace(workspace)
            (workspace / "value.txt").write_text("5\n", encoding="utf-8")
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="tampered",
                    workspace=str(workspace),
                    write_paths=[str(workspace)],
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "developer"},
                        {"id": "acc", "executor": "mock", "role": "acceptor"},
                    ],
                    flow=["codex", "acc"],
                    mock_plan=["ok", "accept"],
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
            )
        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "unverified")
        self.assertEqual(result.rounds_executed, 1)
        self.assertIn("摘要", result.error_message)

    def test_acceptor_prompt_contains_real_verification_evidence(self) -> None:
        with support.TempSpace() as root:
            result = self._verified_flow(root, ["write_value", "ok", "accept"])
            prompt = (result.run_dir / "rounds" / "003" / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("【协调器验证证据】", prompt)
        self.assertIn("退出码：0", prompt)
        self.assertIn("修改范围：value.txt", prompt)
        self.assertLess(prompt.index("<<<END_UNTRUSTED_AGENT_OUTPUT>>>"), prompt.index("【协调器验证证据】"))

    def test_relative_files_touched_stay_inside_workspace(self) -> None:
        with support.TempSpace() as root:
            legal = Coordinator().run(
                support.make_task(
                    root,
                    task_id="relative",
                    participants=[{"id": "codex", "executor": "mock", "role": "developer"}],
                    flow=["codex"],
                    mock_plan=["claim_relative"],
                )
            )
            escaped = Coordinator().run(
                support.make_task(
                    root,
                    task_id="escape",
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "developer"},
                        {"id": "acc", "executor": "mock", "role": "acceptor"},
                    ],
                    flow=["codex", "acc"],
                    mock_plan=["claim_escape", "accept"],
                )
            )
        self.assertEqual(legal.messages[0].claimed_files, ["src/Adder.java"])
        self.assertEqual(legal.messages[0].unsafe_paths, [])
        self.assertNotEqual(legal.stop_reason, "invalid_path")
        self.assertEqual(escaped.stop_reason, "invalid_path")
        self.assertEqual(escaped.rounds_executed, 1)
        self.assertIn("../outside.txt", escaped.messages[0].unsafe_paths)
        self.assertFalse(escaped.success)


if __name__ == "__main__":
    unittest.main()
