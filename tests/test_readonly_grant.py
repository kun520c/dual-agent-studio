"""精确路径只读授权。测试使用临时目录，不读取用户的正式项目。"""

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
from dual_agent.coordinator import Coordinator
from dual_agent.executors import AgentRequest, CodexExecutor, CursorExecutor
from dual_agent.grants import READONLY_GRANT_PATHS, assess, reject_registration
from dual_agent.models import ConfigError, dump_json, parse_task
from dual_agent.projects import register_project
from dual_agent.readiness import check_workspace, render_readiness_report
from dual_agent.safety import normalize_path


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
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
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    (repo / "src").mkdir()
    (repo / "src" / "Demo.java").write_text("public class Demo {}\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "src/Demo.java")
    _git(repo, "commit", "-m", "baseline")


def _readonly_task(repo: Path, **extra: object) -> dict:
    data: dict = {
        "task_id": "grant-read",
        "title": "只读试点",
        "goal": "审查模拟面试项目的检索链路，不要修改文件。",
        "workspace": str(repo),
        "participants": [
            {"id": "codex", "executor": "mock", "role": "诊断者"},
            {"id": "cursor", "executor": "cursor", "role": "审查者"},
        ],
        "flow": ["codex", "cursor"],
        "write_paths": [],
        "allowed_roots": [str(repo)],
    }
    data.update(extra)
    return data


def _request(role: str, workspace: Path, *, allow_codex: bool = False) -> AgentRequest:
    return AgentRequest(
        task_id="grant-cmd",
        round_index=1,
        agent_name=role,
        role=role,
        executor="codex" if role != "reviewer" else "cursor",
        prompt="只读",
        workspace=workspace,
        timeout_seconds=5,
        scenario="ok",
        prompt_path=workspace / "prompt.txt",
        last_message_path=workspace / "last.txt",
        allow_real_codex=allow_codex,
        allow_real_cursor=True,
        codex_bin="missing-codex.exe",
        cursor_bin=None,
        write_paths=[workspace],
    )


class ReadOnlyGrantTests(unittest.TestCase):
    def test_public_default_has_no_author_path_grants(self) -> None:
        self.assertEqual(READONLY_GRANT_PATHS, ())

    def test_forbidden_names_and_copies_are_not_scanned(self) -> None:
        with support.TempSpace() as root:
            copy = root / "ai-interview-system"
            copy.mkdir()
            renamed = root / "renamed-copy"
            renamed.mkdir()
            with patch("dual_agent.readiness.os.walk", side_effect=AssertionError("walk")), patch(
                "dual_agent.readiness.subprocess.run", side_effect=AssertionError("git")
            ):
                named = check_workspace(str(copy), _readonly_task(copy))
                disguised = check_workspace(str(renamed), _readonly_task(renamed, goal="请看模拟面试项目"))
                photo = check_workspace(r"C:\example\修图\child")
        self.assertEqual(named[0].status, "BLOCKED")
        self.assertEqual(disguised[0].status, "BLOCKED")
        self.assertEqual(photo[0].status, "BLOCKED")
        self.assertIn("没有搜索", named[0].detail)
        self.assertIn("没有搜索", disguised[0].detail)
        self.assertIn("没有搜索", photo[0].detail)

    def test_exact_grant_allows_readonly_and_refuses_write_or_developer(self) -> None:
        with support.TempSpace() as root:
            repo = root / "sample"
            _init_repo(repo)
            (repo / "application-local.yaml").write_text(
                "password: unit-test-secret-value\n",
                encoding="utf-8",
                newline="\n",
            )
            (repo / ".env").write_text("TOKEN=unit-test-secret-value\n", encoding="utf-8", newline="\n")
            readonly = _readonly_task(repo)
            writable = _readonly_task(repo, write_paths=[str(repo)])
            developer = _readonly_task(
                repo,
                participants=[{"id": "codex", "executor": "codex", "role": "developer"}],
                flow=["codex"],
            )
            clean = _readonly_task(repo, goal="只读检查", title="只读")
            with patch("dual_agent.grants.READONLY_GRANT_PATHS", (str(repo),)):
                allowed = check_workspace(str(repo), readonly)
                report = render_readiness_report(str(repo), allowed)
                with patch("dual_agent.readiness.os.walk", side_effect=AssertionError("walk")):
                    blocked_write = check_workspace(str(repo), writable)
                    blocked_dev = check_workspace(str(repo), developer)
                    child = check_workspace(str(repo / "src"), clean)
        statuses = {item.code: item.status for item in allowed}
        self.assertEqual(statuses["readonly_grant"], "PASS")
        self.assertEqual(statuses["worktree"], "BLOCKED")
        self.assertNotIn("unit-test-secret-value", report)
        self.assertEqual(blocked_write[0].status, "BLOCKED")
        self.assertIn("write_paths", blocked_write[0].detail)
        self.assertEqual(blocked_dev[0].status, "BLOCKED")
        self.assertIn("开发者", blocked_dev[0].detail)
        self.assertEqual(child[0].status, "BLOCKED")
        self.assertIn("没有搜索", child[0].detail)

    def test_normalized_same_directory_matches_but_junction_does_not(self) -> None:
        if os.name != "nt":
            self.skipTest("仅 Windows")
        with support.TempSpace() as root:
            repo = root / "sample"
            _init_repo(repo)
            (repo / "child").mkdir()
            alias = root / "alias"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(alias), str(repo)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            link_as_grant = root / "linked-grant"
            created_grant = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link_as_grant), str(repo)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(created_grant.returncode, 0, created_grant.stderr)
            dotted = repo / "child" / ".."
            plain = _readonly_task(repo, goal="只读检查", title="只读", workspace=str(alias))
            with patch("dual_agent.grants.READONLY_GRANT_PATHS", (str(repo),)):
                same = assess(str(dotted), _readonly_task(repo, goal="只读检查", title="只读"))
                with patch("dual_agent.readiness.os.walk", side_effect=AssertionError("walk")):
                    junction = check_workspace(str(alias), plain)
            with patch("dual_agent.grants.READONLY_GRANT_PATHS", (str(link_as_grant),)):
                with patch("dual_agent.readiness.os.walk", side_effect=AssertionError("walk")):
                    grant_is_link = check_workspace(str(link_as_grant), _readonly_task(link_as_grant))
        self.assertTrue(same.grant)
        self.assertEqual(junction[0].status, "BLOCKED")
        self.assertIn("没有搜索", junction[0].detail)
        self.assertEqual(grant_is_link[0].status, "BLOCKED")
        self.assertIn("重解析点", grant_is_link[0].detail)

    def test_registration_rejects_grant_name_and_junction_target(self) -> None:
        if os.name != "nt":
            self.skipTest("仅 Windows")
        with support.TempSpace() as root:
            repo = root / "sample"
            repo.mkdir()
            alias = root / "alias"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(alias), str(repo)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            with self.assertRaises(ConfigError):
                reject_registration(r"C:\example\ai-interview-system")
            with patch("dual_agent.grants.READONLY_GRANT_PATHS", (str(repo),)):
                with self.assertRaises(ConfigError):
                    reject_registration(str(alias))
            registry = root / "projects"
            registry.mkdir()
            payload = {
                "project_id": "photo",
                "name": "不该注册",
                "workspace": str(root / "修图样本"),
                "allowed_roots": [str(root / "修图样本")],
                "participants": [{"id": "codex", "executor": "mock", "role": "developer"}],
            }
            (root / "修图样本").mkdir()
            path = root / "photo.json"
            dump_json(path, payload)
            with self.assertRaises(ConfigError):
                register_project(path, registry)

    def test_coordinator_readonly_grant_is_not_verified_and_blocks_writes(self) -> None:
        with support.TempSpace() as root:
            repo = root / "sample"
            _init_repo(repo)
            secret = repo / "application-local.yaml"
            secret.write_text("password: unit-test-secret-value\n", encoding="utf-8", newline="\n")
            before = secret.read_bytes()
            readonly = parse_task(
                {
                    **_readonly_task(repo),
                    "participants": [
                        {"id": "codex", "executor": "mock", "role": "diagnoser"},
                        {"id": "cursor", "executor": "mock", "role": "reviewer"},
                    ],
                    "flow": ["codex", "cursor"],
                    "mock_plan": ["ok", "ok"],
                    "runs_dir": str(root / "runs"),
                    "max_repair_rounds": 0,
                    "max_model_calls": 2,
                    "codex_call_limit": 1,
                    "cursor_call_limit": 1,
                },
                source="grant-test",
            )
            developer = parse_task(
                {
                    **_readonly_task(repo),
                    "task_id": "grant-dev",
                    "participants": [{"id": "codex", "executor": "mock", "role": "developer"}],
                    "flow": ["codex"],
                    "mock_plan": ["ok"],
                    "runs_dir": str(root / "runs"),
                    "worktree": {"enabled": True, "base_ref": "HEAD"},
                },
                source="grant-dev",
            )
            with patch("dual_agent.grants.READONLY_GRANT_PATHS", (str(repo),)):
                with patch("dual_agent.coordinator.prepare_worktree_config", side_effect=AssertionError("worktree")):
                    denied = Coordinator().run(developer)
                completed = Coordinator().run(readonly)
            snapshot = json.loads((completed.run_dir / "workspace_snapshot.json").read_text(encoding="utf-8"))
            diff = (completed.run_dir / "rounds" / "001" / "changes.diff").read_text(encoding="utf-8")
        self.assertEqual(denied.stop_reason, "invalid_path")
        self.assertEqual(denied.rounds_executed, 0)
        self.assertFalse(denied.success)
        self.assertEqual(completed.stop_reason, "flow_complete")
        self.assertFalse(completed.success)
        self.assertNotEqual(completed.status, "verified")
        self.assertEqual(completed.rounds_executed, 2)
        self.assertNotIn("application-local.yaml", snapshot)
        self.assertNotIn("unit-test-secret-value", diff)
        self.assertEqual(secret.read_bytes(), before)

    def test_real_codex_write_sandbox_and_cursor_developer_stay_blocked(self) -> None:
        with support.TempSpace() as root:
            repo = root / "sample"
            repo.mkdir()
            with patch("dual_agent.grants.READONLY_GRANT_PATHS", (str(repo),)):
                command = CodexExecutor().build_command(_request("diagnoser", repo))
                with patch.dict(os.environ, {"DUAL_AGENT_ALLOW_CODEX": "1"}, clear=False):
                    with patch("dual_agent.winproc.subprocess.Popen", side_effect=AssertionError("不应启动 Codex")):
                        blocked = CodexExecutor().run(_request("developer", repo, allow_codex=True))
                with patch("dual_agent.winproc.subprocess.Popen", side_effect=AssertionError("不应启动 Cursor")):
                    cursor = CursorExecutor().run(_request("developer", repo))
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(blocked.error_kind, "codex_disabled")
        self.assertFalse(blocked.invoked)
        self.assertEqual(cursor.error_kind, "cursor_developer_blocked")
        self.assertFalse(cursor.invoked)

    def test_coordinator_still_blocks_other_formal_projects_before_scanning(self) -> None:
        with support.TempSpace() as root:
            config = parse_task(
                {
                    "task_id": "blocked-photo",
                    "title": "不该运行",
                    "goal": "不要进入目录",
                    "workspace": str(root / "修图样本"),
                    "participants": [{"id": "codex", "executor": "mock", "role": "diagnoser"}],
                    "flow": ["codex"],
                    "write_paths": [],
                    "runs_dir": str(root / "runs"),
                },
                source="blocked",
            )
            with patch("dual_agent.coordinator.os.walk", side_effect=AssertionError("walk")):
                result = Coordinator().run(config)
        self.assertEqual(result.stop_reason, "invalid_path")
        self.assertEqual(result.rounds_executed, 0)
        self.assertIn("没有搜索", result.error_message)


if __name__ == "__main__":
    unittest.main()
