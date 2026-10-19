"""命令行入口。"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from dual_agent import PROJECT_ROOT, __version__
from dual_agent.coordinator import Coordinator, default_runs_dir, run_parallel
from dual_agent.executors import cursor_version_command, resolve_codex_bin
from dual_agent.models import ConfigError, RunResult, load_task, parse_task
from dual_agent.projects import (
    build_project_task,
    disable_project,
    enable_project,
    list_projects,
    project_history,
    project_status,
    register_project,
    show_project,
    update_project,
)
from dual_agent.readiness import check_workspace, render_readiness_report, write_readiness_report
from dual_agent.scheduler import cancel_schedule, run_batch
from dual_agent.status import read_status


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def exit_code(result: RunResult) -> int:
    if result.success:
        return 0
    if result.stop_reason in {"flow_complete", "unverified", "max_rounds", "baseline_clean"}:
        return 4
    if result.stop_reason in {"invalid_path", "write_conflict", "resource_conflict"}:
        return 3
    if result.stop_reason == "cancelled":
        return 2
    if result.stop_reason in {"codex_disabled", "cursor_disabled"}:
        return 5
    return 2


def print_result(result: RunResult) -> None:
    print(f"任务：{result.task_id}")
    print(f"结论：{'通过' if result.success else '未通过'}")
    print(f"停止原因：{result.stop_reason_zh}")
    print(f"调用性质：{result.mode_label}")
    print(f"报告：{result.report_path}")


def _captured_version(command: list[str], cwd: str | None = None) -> str:
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 30,
        "check": False,
        "cwd": cwd,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    try:
        completed = subprocess.run(command, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"无法获取版本：{exc}"
    text = decode_text(completed.stdout) + "\n" + decode_text(completed.stderr)
    compact = " ".join(text.split())
    return compact or "（无输出）"


def decode_text(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def collect_doctor() -> dict[str, str | bool]:
    cursor_cmd = cursor_version_command(None)
    cursor_cwd = None
    if len(cursor_cmd) >= 2 and cursor_cmd[1].endswith("index.js"):
        cursor_cwd = str(Path(cursor_cmd[1]).parent)
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "codex": _captured_version([resolve_codex_bin(None), "--version"]),
        "cursor": _captured_version(cursor_cmd, cursor_cwd),
        "utf8_sample": "中文UTF-8检查：协作",
        "codex_real_calls": os.environ.get("DUAL_AGENT_ALLOW_CODEX") == "1",
    }


def format_doctor(info: dict[str, str | bool]) -> str:
    codex_calls = "已打开" if info["codex_real_calls"] else "关闭"
    lines = [
        f"Python：{info['python']}",
        f"系统：{info['platform']}",
        f"Codex CLI：{info['codex']}",
        f"Cursor CLI：{info['cursor']}",
        f"编码样本：{info['utf8_sample']}",
        f"真实 Codex 调用：{codex_calls}（还需要配置 allow_real_codex=true）",
        "Cursor Ask 模式可以做只读问答，但它不是操作系统沙箱。",
        "本机不使用 --sandbox enabled，也不使用 --yolo 或全权限开关。",
        "doctor 只读取版本，不会把提示词发给模型。",
    ]
    return "\n".join(lines)


def build_probe_config():
    workspace = PROJECT_ROOT / "workspaces" / "示例工作区"
    return parse_task(
        {
            "task_id": "cursor-probe",
            "title": "真实 Cursor CLI 集成探针",
            "goal": "请只回复这一句中文，不要使用工具：集成探针成功。",
            "workspace": str(workspace),
            "max_rounds": 1,
            "timeout_seconds": 90,
            "participants": [{"id": "cursor", "executor": "cursor", "role": "diagnoser"}],
            "flow": ["cursor"],
            "cycle": False,
            "write_paths": [],
            "allow_real_codex": False,
            "allow_real_cursor": True,
            "description": "一次性只读探针。Ask 模式不是操作系统沙箱。",
        },
        source="probe-cursor",
    )


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    import argparse

    parser = argparse.ArgumentParser(
        prog="dual_agent",
        description="Windows 本地 Codex 与 Cursor 双 Agent 协调器",
    )
    parser.add_argument("--version", action="version", version=f"dual-agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="按任务配置运行一次协作")
    run_parser.add_argument("--config", "-c", required=True, help="任务 JSON 配置路径")

    sub.add_parser("doctor", help="检查本机环境，不调用模型")

    parallel_parser = sub.add_parser("parallel", help="并行运行多个互不重叠的任务")
    parallel_parser.add_argument("--config", action="append", required=True, help="可重复传入多个配置")

    project_parser = sub.add_parser("project", help="注册和查看项目")
    project_sub = project_parser.add_subparsers(dest="project_command", required=True)
    register_parser = project_sub.add_parser("register", help="注册新项目")
    register_parser.add_argument("--file", "-f", required=True)
    register_parser.add_argument("--root", help="项目注册目录，默认是 projects")
    list_parser = project_sub.add_parser("list", help="查看全部项目")
    list_parser.add_argument("--root")
    show_parser = project_sub.add_parser("show", help="查看项目详情")
    show_parser.add_argument("project_id")
    show_parser.add_argument("--root")
    update_parser = project_sub.add_parser("update", help="修改项目配置")
    update_parser.add_argument("project_id")
    update_parser.add_argument("--file", "-f", required=True)
    update_parser.add_argument("--root")
    disable_parser = project_sub.add_parser("disable", help="禁用项目")
    disable_parser.add_argument("project_id")
    disable_parser.add_argument("--root")
    enable_parser = project_sub.add_parser("enable", help="重新启用项目")
    enable_parser.add_argument("project_id")
    enable_parser.add_argument("--root")
    task_parser = project_sub.add_parser("task", help="为项目创建并运行一个任务")
    task_parser.add_argument("project_id")
    task_parser.add_argument("--title", required=True)
    task_parser.add_argument("--goal", required=True)
    task_parser.add_argument("--task-id")
    task_parser.add_argument("--config", help="可选任务覆盖 JSON")
    task_parser.add_argument("--root")
    history_parser = project_sub.add_parser("history", help="按项目查看历史报告")
    history_parser.add_argument("project_id")
    history_parser.add_argument("--root")
    history_parser.add_argument("--runs-dir")
    status_parser = project_sub.add_parser("status", help="查看项目最近状态")
    status_parser.add_argument("project_id")
    status_parser.add_argument("--root")
    status_parser.add_argument("--runs-dir")
    report_parser = project_sub.add_parser("report", help="查看项目最近一次验收报告，不删除历史")
    report_parser.add_argument("project_id")
    report_parser.add_argument("--root")
    report_parser.add_argument("--runs-dir")

    batch_parser = sub.add_parser("batch", help="按批次并行运行多个项目任务")
    batch_parser.add_argument("--file", "-f", required=True)
    batch_parser.add_argument("--root")

    schedule_parser = sub.add_parser("schedule", help="查看或取消调度任务")
    schedule_sub = schedule_parser.add_subparsers(dest="schedule_command", required=True)
    schedule_list = schedule_sub.add_parser("list", help="查看调度状态")
    schedule_list.add_argument("--runs-dir")
    schedule_show = schedule_sub.add_parser("show", help="查看一个调度任务")
    schedule_show.add_argument("schedule_id")
    schedule_show.add_argument("--runs-dir")
    schedule_cancel = schedule_sub.add_parser("cancel", help="取消排队任务或请求停止正在运行的任务")
    schedule_cancel.add_argument("schedule_id")
    schedule_cancel.add_argument("--runs-dir")

    ready_parser = sub.add_parser("readiness", help="正式项目接入前检查。不调用模型，不修改、不注册目标项目")
    ready_parser.add_argument("--workspace", required=True, help="要检查的工作区。不会访问被拒绝的正式项目路径")
    ready_parser.add_argument("--config", help="可选任务 JSON，只读取验证、预算和锁声明")
    ready_parser.add_argument("--report", help="报告输出路径。默认写到本协调器的 runs/readiness")

    probe_parser = sub.add_parser("probe-cursor", help="对真实 Cursor CLI 做一次只读问答")
    probe_parser.add_argument(
        "--i-understand",
        action="store_true",
        help="确认这次调用会把一句短提示发给 Cursor，且失败后不会重试",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(format_doctor(collect_doctor()))
            return 0
        if args.command == "run":
            config = load_task(Path(args.config))
            result = Coordinator().run(config)
            print_result(result)
            return exit_code(result)
        if args.command == "parallel":
            configs = [load_task(Path(item)) for item in args.config]
            results = run_parallel(configs)
            for result in results:
                print_result(result)
                print("")
            return max((exit_code(result) for result in results), default=0)
        if args.command == "project":
            return _project_command(args)
        if args.command == "batch":
            return _batch_command(args)
        if args.command == "schedule":
            return _schedule_command(args)
        if args.command == "readiness":
            return _readiness_command(args)
        if args.command == "probe-cursor":
            if not args.i_understand:
                print("该命令会真实调用一次 Cursor CLI。请追加 --i-understand。失败后不会重试。")
                return 3
            print("开始一次真实 Cursor Ask 调用。如果出现额度或网络错误，将记录原因并停止。")
            result = Coordinator().run(build_probe_config())
            print_result(result)
            if result.messages and result.messages[0].content:
                print("回答摘录：")
                print(result.messages[0].content[:1000])
            return exit_code(result)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 3
    print("未知命令")
    return 2


def _root_of(args) -> Path | None:
    value = getattr(args, "root", None)
    return Path(value) if value else None


def _runs_of(args) -> Path | None:
    value = getattr(args, "runs_dir", None)
    return Path(value) if value else None


def _project_command(args) -> int:
    root = _root_of(args)
    command = args.project_command
    if command == "register":
        project = register_project(Path(args.file), root)
        print(f"已注册：{project.project_id} {project.name}")
        return 0
    if command == "list":
        found = list_projects(root)
        if not found:
            print("没有已注册项目")
            return 0
        for project in found:
            state = "启用" if project.enabled else "禁用"
            print(f"{project.project_id}\t{state}\t{project.name}\t{project.workspace}")
        return 0
    if command == "show":
        print(json.dumps(show_project(args.project_id, root).to_dict(), ensure_ascii=False, indent=2))
        return 0
    if command == "update":
        project = update_project(args.project_id, Path(args.file), root)
        print(f"已更新：{project.project_id}")
        return 0
    if command == "disable":
        disable_project(args.project_id, root)
        print(f"已禁用：{args.project_id}")
        return 0
    if command == "enable":
        enable_project(args.project_id, root)
        print(f"已启用：{args.project_id}")
        return 0
    if command == "history":
        rows = project_history(args.project_id, _runs_of(args))
        if not rows:
            print("没有历史运行")
            return 0
        for row in rows:
            print(
                f"{row['status']}\t{row['task_id']}\t{row['stop_reason']}\t{row['finished_at']}\t{row['report_path']}"
            )
            if row.get("error_message"):
                print(f"失败原因：{row['error_message']}")
        return 0
    if command == "status":
        payload = project_status(args.project_id, root, _runs_of(args))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        latest = payload.get("latest") or {}
        if isinstance(latest, dict) and latest.get("error_message"):
            print(f"失败原因：{latest['error_message']}")
        return 0
    if command == "report":
        rows = project_history(args.project_id, _runs_of(args))
        if not rows:
            print("没有历史运行")
            return 0
        latest = rows[0]
        report_path = Path(str(latest.get("report_path") or ""))
        print(f"状态：{latest.get('status')}")
        print(f"停止原因：{latest.get('stop_reason')}")
        print(f"报告：{report_path}")
        if not report_path.is_file():
            print("验收报告文件不存在。历史记录已保留。")
            return 3
        print(report_path.read_text(encoding="utf-8"))
        return 0
    if command == "task":
        spec: dict = {"title": args.title, "goal": args.goal}
        if args.task_id:
            spec["task_id"] = args.task_id
        if args.config:
            loaded = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict):
                print("任务覆盖文件必须是对象")
                return 3
            loaded.pop("title", None)
            loaded.pop("goal", None)
            spec.update(loaded)
            spec["title"] = args.title
            spec["goal"] = args.goal
        config = build_project_task(args.project_id, spec, root)
        result = Coordinator().run(config)
        print_result(result)
        return exit_code(result)
    print("未知项目命令")
    return 2


def _batch_command(args) -> int:
    path = Path(args.file)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"无法读取批次：{exc}")
        return 3
    if not isinstance(data, dict):
        print("批次文件必须是对象")
        return 3
    items = data.get("tasks")
    if not isinstance(items, list) or not items:
        print("批次缺少 tasks")
        return 3
    root = _root_of(args)
    names: dict[str, str] = {}
    configs = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not item.get("project_id"):
            print(f"第 {index} 个任务缺少 project_id")
            return 3
        spec = dict(item)
        project_id = str(spec.pop("project_id"))
        if data.get("runs_dir") and "runs_dir" not in spec:
            spec["runs_dir"] = data["runs_dir"]
        project = show_project(project_id, root)
        names[project.project_id] = project.name
        configs.append(build_project_task(project_id, spec, root))
    limit = data.get("max_concurrent_tasks", 2)
    batch = run_batch(
        configs,
        batch_id=data.get("batch_id"),
        max_concurrent=limit,
        codex_call_limit=data.get("codex_call_limit"),
        cursor_call_limit=data.get("cursor_call_limit"),
        project_names=names,
    )
    print(f"批次：{batch.batch_id}")
    print(f"调用性质：{batch.call_mode}")
    print(f"峰值并发：{batch.peak_running}")
    print(f"时间重叠：{'是' if batch.overlapped else '否'}")
    print(f"Codex 调用：{batch.codex_calls}")
    print(f"Cursor 调用：{batch.cursor_calls}")
    print(f"汇总报告：{batch.report_path}")
    codes: list[int] = []
    for item in batch.tasks:
        if item.result is not None:
            print_result(item.result)
            print("")
            codes.append(exit_code(item.result))
        else:
            print(f"任务：{item.config.task_id}")
            print(f"结论：{'通过' if item.state == 'verified' else '未通过'}")
            print(f"状态：{item.state}")
            if item.error:
                print(f"失败原因：{item.error}")
            print("")
            codes.append(0 if item.state == "verified" else 2)
    return max(codes, default=0)


def _schedule_command(args) -> int:
    runs = _runs_of(args) or default_runs_dir()
    if args.schedule_command == "list":
        directory = runs / "schedule"
        if not directory.exists():
            print("没有调度记录")
            return 0
        found = False
        for path in sorted(directory.glob("sch-*.json")):
            data = read_status(path)
            if not data:
                continue
            found = True
            print(
                f"{data.get('schedule_id')}\t{data.get('state')}\t{data.get('project_id')}\t{data.get('task_id')}"
            )
        if not found:
            print("没有调度记录")
        return 0
    if args.schedule_command == "show":
        path = runs / "schedule" / f"{args.schedule_id}.json"
        data = read_status(path)
        if not data:
            print(f"找不到调度任务：{args.schedule_id}")
            return 3
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if data.get("detail"):
            print(f"说明：{data['detail']}")
        return 0
    if args.schedule_command == "cancel":
        print(cancel_schedule(args.schedule_id, runs))
        return 0
    print("未知调度命令")
    return 2


def _readiness_command(args) -> int:
    task = None
    if args.config:
        path = Path(args.config)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"无法读取检查配置：{exc}")
            return 3
        if not isinstance(loaded, dict):
            print("检查配置必须是对象")
            return 3
        task = loaded
    items = check_workspace(args.workspace, task)
    text = render_readiness_report(args.workspace, items)
    destination = Path(args.report) if args.report else None
    report_path = write_readiness_report(args.workspace, text, destination)
    blocked = sum(1 for item in items if item.status == "BLOCKED")
    passed = sum(1 for item in items if item.status == "PASS")
    skipped = sum(1 for item in items if item.status == "NOT CHECKED")
    print(f"报告：{report_path}")
    print(f"PASS：{passed}")
    print(f"BLOCKED：{blocked}")
    print(f"NOT CHECKED：{skipped}")
    print("目录检查通过不等于操作系统级隔离。")
    return 3 if blocked else 0
