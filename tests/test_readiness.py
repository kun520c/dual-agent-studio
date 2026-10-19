"""接入前检查和验收报告查询。不读取用户的正式项目。"""

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
from dual_agent.cli import main
from dual_agent.readiness import check_workspace, render_readiness_report


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


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if _git(repo, "init").returncode != 0:
        raise AssertionError("git init failed")
    (repo / "src").mkdir()
    (repo / "src" / "Demo.java").write_text("public class Demo {}\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "src/Demo.java")
    if _git(repo, "commit", "-m", "baseline").returncode != 0:
        raise AssertionError("git commit failed")


class ReadinessTests(unittest.TestCase):
    def test_forbidden_project_is_not_scanned(self) -> None:
        with patch("dual_agent.readiness.os.walk", side_effect=AssertionError("walk")), patch(
            "dual_agent.readiness.subprocess.run", side_effect=AssertionError("git")
        ):
            items = check_workspace(r"C:\example\修图\child")
        self.assertEqual(items[0].status, "BLOCKED")
        self.assertIn("没有搜索", items[0].detail)

    def test_report_uses_only_three_statuses_and_denies_os_isolation(self) -> None:
        with support.TempSpace() as root:
            repo = root / "sample"
            _init_repo(repo)
            (repo / "src" / "Store.java").write_text("class Store { String url = \"mysql\"; }\n", encoding="utf-8")
            items = check_workspace(str(repo))
            text = render_readiness_report(str(repo), items)
        statuses = {item.status for item in items}
        self.assertTrue(statuses <= {"PASS", "BLOCKED", "NOT CHECKED"})
        self.assertIn("PASS", statuses)
        self.assertIn("BLOCKED", statuses)
        self.assertIn("NOT CHECKED", statuses)
        self.assertIn("不是操作系统级隔离", text)
        self.assertIn("mysql", text)
        isolation = next(item for item in items if item.code == "os_isolation")
        self.assertEqual(isolation.status, "NOT CHECKED")

    def test_clean_repo_git_passes_and_dirty_tree_is_blocked(self) -> None:
        with support.TempSpace() as root:
            repo = root / "clean"
            _init_repo(repo)
            clean = {item.code: item.status for item in check_workspace(str(repo))}
            (repo / "src" / "Demo.java").write_text("public class Demo { int x; }\n", encoding="utf-8")
            dirty = {item.code: item.status for item in check_workspace(str(repo))}
        self.assertEqual(clean["git"], "PASS")
        self.assertEqual(clean["uncommitted"], "PASS")
        self.assertEqual(clean["worktree"], "PASS")
        self.assertEqual(dirty["uncommitted"], "BLOCKED")

    def test_junction_workspace_stops_before_walking_the_target(self) -> None:
        if os.name != "nt":
            self.skipTest("仅 Windows")
        with support.TempSpace() as root:
            link = root / "link"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link), r"C:\Windows"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            with patch("dual_agent.readiness.os.walk", side_effect=AssertionError("walk")):
                items = check_workspace(str(link))
        self.assertEqual(items[0].code, "symlink")
        self.assertEqual(items[0].status, "BLOCKED")

    def test_project_report_prints_history_without_deleting_it(self) -> None:
        with support.TempSpace() as root:
            run = root / "runs" / "demo" / "20261012"
            run.mkdir(parents=True)
            (run / "task.json").write_text(
                json.dumps({"project_id": "alpha", "task_id": "demo"}),
                encoding="utf-8",
            )
            (run / "result.json").write_text(
                json.dumps(
                    {
                        "status": "verified",
                        "stop_reason": "verified",
                        "success": True,
                        "finished_at": "2026-10-12T00:00:00",
                    }
                ),
                encoding="utf-8",
            )
            report = run / "报告.md"
            report.write_text("# 双 Agent 执行报告\n\n- 结论：通过\n", encoding="utf-8")
            code = main(["project", "report", "alpha", "--runs-dir", str(root / "runs")])
            self.assertTrue(report.is_file())
            self.assertEqual(report.read_text(encoding="utf-8"), "# 双 Agent 执行报告\n\n- 结论：通过\n")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
