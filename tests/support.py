"""测试共用的临时目录和任务配置。"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dual_agent.models import TaskConfig, parse_task  # noqa: E402

_LIVE: list[Path] = []


def _cleanup_live() -> None:
    for path in _LIVE:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_live)


class TempSpace:
    """退出 with 后仍保留目录，方便断言报告；进程结束时统一删除。"""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="dual-agent-test-"))
        _LIVE.append(self.path)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_args: object) -> None:
        return None


def prepare_workspace(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "说明.txt").write_text("测试工作区：中文\n", encoding="utf-8", newline="\n")


def make_task(root: Path, **overrides: object) -> TaskConfig:
    workspace = root / "workspace"
    prepare_workspace(workspace)
    data: dict[str, object] = {
        "task_id": "test",
        "title": "测试任务",
        "goal": "验证中文完整性：双Agent协作，甲乙丙，编号①，引号“通过”。",
        "workspace": str(workspace),
        "max_rounds": 6,
        "timeout_seconds": 30,
        "participants": [
            {"id": "codex", "executor": "mock", "role": "developer"},
            {"id": "cursor", "executor": "mock", "role": "reviewer"},
        ],
        "flow": ["codex", "cursor"],
        "cycle": False,
        "mock_plan": ["ok", "ok"],
        "write_paths": [str(workspace)],
        "runs_dir": str(root / "runs"),
        "allow_real_codex": False,
        "allow_real_cursor": False,
    }
    data.update(overrides)
    return parse_task(data, source="test")
