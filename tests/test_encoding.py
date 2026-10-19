"""编码分类回归。使用保存的现场和模拟进程，不调用真实模型。"""

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
from dual_agent.executors import AgentRequest, AnswerUnreadable, extract_codex_answer
from dual_agent.models import parse_protocol
from dual_agent.safety import classify_output, decode_process_bytes

HISTORY = (
    PROJECT_ROOT
    / "runs"
    / "codex-readonly-probe"
    / "20261011-183211-994605"
    / "rounds"
    / "001"
)
TOOL_LINE = json.dumps(
    {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "aggregated_output": "工具回显\ufffd不是最终回答",
            "text": "这条工具文本不能冒充最终回答",
        },
    },
    ensure_ascii=False,
)
VALID_ANSWER = (
    '{"summary":"只读探针中文完整","details":"已读取任务说明.txt",'
    '"decision":"continue","files_touched":[],"risks":[]}'
)
SKILL_STDERR = (
    "ERROR codex_core::session::session: failed to load skill SKILL.md: "
    "missing YAML frontmatter delimited by ---\n"
    'ERROR rmcp::transport::worker: UnexpectedServerResponse("HTTP 502: ")\n'
)


def _request(path: Path) -> AgentRequest:
    workspace = path.parent
    return AgentRequest(
        task_id="encoding",
        round_index=1,
        agent_name="codex",
        role="diagnoser",
        executor="codex",
        prompt="只读",
        workspace=workspace,
        timeout_seconds=5,
        scenario="ok",
        prompt_path=workspace / "prompt.txt",
        last_message_path=path,
        allow_real_codex=False,
        allow_real_cursor=False,
        codex_bin=None,
        cursor_bin=None,
    )


class EncodingTests(unittest.TestCase):
    def test_tool_replacement_character_does_not_fail_valid_answer(self) -> None:
        kind = classify_output(
            stdout=TOOL_LINE,
            stderr=SKILL_STDERR,
            exit_code=0,
            timed_out=False,
            garbled=False,
            answer=VALID_ANSWER,
        )
        self.assertEqual(kind, "ok")
        parsed = parse_protocol(VALID_ANSWER, "diagnoser")
        self.assertIn("只读探针中文完整", parsed["summary"])

    def test_invalid_utf8_bytes_still_block(self) -> None:
        text, failed = decode_process_bytes(b"\xff\xfe\xfa")
        self.assertTrue(failed)
        self.assertIn("\ufffd", text)
        kind = classify_output(
            stdout=text,
            stderr="",
            exit_code=0,
            timed_out=False,
            garbled=True,
            answer=VALID_ANSWER,
        )
        self.assertEqual(kind, "garbled")

    def test_damaged_final_answer_blocks_and_keeps_scene(self) -> None:
        with support.TempSpace() as root:
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="damaged",
                    participants=[
                        {"id": "codex", "executor": "mock", "role": "diagnoser"},
                        {"id": "cursor", "executor": "mock", "role": "reviewer"},
                    ],
                    flow=["codex", "cursor"],
                    mock_plan=["damaged_answer", "ok"],
                    write_paths=[],
                )
            )
        self.assertEqual(result.stop_reason, "garbled")
        self.assertFalse(result.success)
        self.assertEqual(result.rounds_executed, 1)
        self.assertIn("锟斤拷", result.messages[0].content)
        self.assertTrue((result.run_dir / "报告.md").is_file())
        self.assertTrue((result.run_dir / "rounds" / "001" / "stdout.txt").is_file())
        self.assertFalse((result.run_dir / "rounds" / "002").exists())

    def test_invalid_final_json_blocks(self) -> None:
        with support.TempSpace() as root:
            result = Coordinator().run(
                support.make_task(
                    root,
                    task_id="badjson",
                    participants=[{"id": "codex", "executor": "mock", "role": "diagnoser"}],
                    flow=["codex"],
                    mock_plan=["bad_json"],
                    write_paths=[],
                )
            )
        self.assertEqual(result.stop_reason, "invalid_protocol")
        self.assertTrue((result.run_dir / "rounds" / "001" / "answer.txt").is_file())

    def test_nonfatal_stderr_is_not_garbled(self) -> None:
        kind = classify_output(
            stdout=TOOL_LINE,
            stderr=SKILL_STDERR,
            exit_code=0,
            timed_out=False,
            garbled=False,
            answer=VALID_ANSWER,
        )
        self.assertEqual(kind, "ok")

    def test_missing_or_blank_last_message_is_not_tool_output(self) -> None:
        with support.TempSpace() as root:
            missing = root / "missing-last.txt"
            blank = root / "blank-last.txt"
            blank.write_text("  \n", encoding="utf-8")
            corrupt = root / "corrupt-last.txt"
            corrupt.write_bytes(b"\xff\xfe")
            self.assertEqual(extract_codex_answer(TOOL_LINE, _request(missing)), "")
            self.assertEqual(extract_codex_answer(TOOL_LINE, _request(blank)), "")
            with self.assertRaises(AnswerUnreadable):
                extract_codex_answer(TOOL_LINE, _request(corrupt))

    def test_saved_probe_replay_allows_handoff_of_valid_answer(self) -> None:
        if not HISTORY.is_dir():
            self.skipTest("没有保存的 Codex 探针现场")
        stdout = (HISTORY / "stdout.txt").read_text(encoding="utf-8")
        stderr = (HISTORY / "stderr.txt").read_text(encoding="utf-8")
        answer = (HISTORY / "answer.txt").read_text(encoding="utf-8")
        last = (HISTORY / "last_message.txt").read_text(encoding="utf-8")
        # 保存的是协调器落盘后的 UTF-8 文本，不是管道原始字节。
        self.assertIn("\ufffd", stdout)
        self.assertNotIn("\ufffd", answer)
        self.assertEqual(answer.strip(), last.strip())
        kind = classify_output(
            stdout=stdout,
            stderr=stderr,
            exit_code=0,
            timed_out=False,
            garbled=False,
            answer=answer,
        )
        self.assertEqual(kind, "ok")
        parsed = parse_protocol(answer, "diagnoser")
        self.assertEqual(parsed["decision"], "continue")
        self.assertIn("只读探针中文完整", parsed["summary"])
        self.assertIn("HTTP 502", stderr)
        self.assertIn("failed to load skill", stderr)


if __name__ == "__main__":
    unittest.main()
