"""供人阅读的中文执行报告。"""

from __future__ import annotations

from dual_agent import __version__
from dual_agent.models import RunResult
from dual_agent.roles import ROLES

STOP_REASON_ZH = {
    "flow_complete": "流程已结束但未验收",
    "verified": "验收并验证通过",
    "unverified": "缺少独立验证",
    "invalid_protocol": "交接协议不合法",
    "cursor_developer_blocked": "真实 Cursor 开发者已被阻断",
    "codex_write_scope_blocked": "Codex 写入范围无法收窄",
    "readonly_grant_blocked": "只读授权拒绝了写入沙箱",
    "accepted": "仅有模型验收声明",
    "rejected": "验收未通过",
    "blocked": "验收者判定阻塞",
    "max_rounds": "达到最大协作轮次",
    "max_repair": "达到最大修复次数",
    "repeated_failure": "相同验证错误重复出现",
    "budget": "达到调用次数或总时长上限",
    "empty": "Agent 空回复",
    "nonzero": "进程非零退出",
    "timeout": "执行超时",
    "quota": "额度不足",
    "network": "网络或 TLS 错误",
    "garbled": "输出乱码或非 UTF-8",
    "invalid_path": "任务路径不合法",
    "write_conflict": "文件写入冲突",
    "resource_conflict": "共享资源锁被占用",
    "cancelled": "任务已取消",
    "baseline_clean": "预置失败并未出现",
    "codex_disabled": "真实 Codex 调用已被禁用",
    "cursor_disabled": "真实 Cursor 调用未被允许",
    "authorization_denied": "Python 独立执行授权校验失败",
    "unexpected": "意外退出或异常",
    "integrity": "消息完整性校验失败",
    "unsafe_command": "命令越过安全边界",
    "ok": "正常",
}

_CLOSING = {
    "flow_complete": "调度流程已经走完，但这不是任务成功。还缺少合法验收结果和独立验证证据。",
    "verified": "验收者给出了合法 accept，并且协调器独立验证已通过。",
    "unverified": "模型的 accept 没有配套的独立验证，不能判定任务成功。",
    "invalid_protocol": "某一轮的最终回答不符合交接协议，协作已停止并保留现场。",
    "cursor_developer_blocked": "真实 Cursor 开发者被阻断。write_paths 目前只是调度锁，不能限制 Cursor 的实际写入。",
    "codex_write_scope_blocked": "Codex 的 workspace-write 覆盖整个工作区，不能假装只允许 write_paths 里的子目录。",
    "readonly_grant_blocked": "该工作区只有精确路径的只读授权。真实 Codex 不能使用 workspace-write，本次没有调用。",
    "accepted": "只有模型验收声明，没有把它当作任务成功。",
    "rejected": "验收者未接受结果，协作已停止。",
    "blocked": "验收者判定任务阻塞，协作已停止。",
    "max_rounds": "已达到最大协作轮次，后续步骤没有继续。",
    "max_repair": "功能验证在用完允许的修复次数后仍然失败。任务没有标记为通过。",
    "repeated_failure": "修复后出现了与上一轮相同的验证错误，协作已停止，没有继续调用模型。",
    "budget": "已达到配置的模型调用次数或总时长上限，后续步骤没有继续。",
    "empty": "某一轮没有产生可交接的回答，协作已停止。",
    "nonzero": "某一轮进程返回了非零退出码，协作已停止。",
    "timeout": "某一轮执行超时，进程已终止，并且没有重试。",
    "quota": "检测到额度不足或速率限制。协调器没有重试，也没有尝试绕过限制。",
    "network": "检测到 TLS 或网络错误。协调器已停止，没有自动重试。",
    "garbled": "某一轮输出无法按 UTF-8 完整解码，已停止，避免把乱码继续交给下一个 Agent。",
    "invalid_path": "任务路径未通过安全检查，没有任何 Agent 被启动。",
    "write_conflict": "另一个任务正在写入重叠路径，本任务没有启动 Agent。",
    "resource_conflict": "声明的共享资源锁被其他任务占用。协调器没有连接该资源，也没有抢占别人的锁。",
    "cancelled": "任务在排队或轮次之间被取消。已经发起的模型调用不会重试。",
    "baseline_clean": "可信测试在任何模型调用之前已经通过。协调器没有进入修复，也没有把这次运行记为通过。",
    "codex_disabled": "真实 Codex 缺少 Python 独立签名授权，没有发起调用。",
    "authorization_denied": "项目、执行范围、授权签名或程序摘要校验失败；没有开放或降级真实执行。",
    "cursor_disabled": "流程走到 Cursor，但配置没有允许真实调用。",
    "unexpected": "出现未预期的异常或进程无法启动。现场已保留。",
    "integrity": "磁盘上的消息摘要与正文不一致，协作已停止。",
    "unsafe_command": "预定命令越过了安全边界，没有执行。",
}


def _verification_lines(item: dict[str, object]) -> list[str]:
    kind = str(item.get("type") or "验证")
    status = "通过" if item.get("ok") else "未通过"
    if item.get("superseded"):
        status += "，已被后续修复取代"
    if kind == "command":
        script = str(item.get("trusted_script") or "")
        title = script.replace("\\", "/").rsplit("/", 1)[-1] or "可信命令"
        argv = item.get("argv") or []
        command = " ".join(str(part) for part in argv) if isinstance(argv, list) else str(argv)
        if item.get("baseline"):
            attempt_text = "（预置失败，模型调用之前）"
        else:
            attempt = item.get("attempt")
            attempt_text = f"（第 {attempt} 次）" if attempt else ""
        lines = [f"- command `{title}`{attempt_text}：{status}"]
        lines.append(f"  - 命令：`{command or '（未记录）'}`")
        lines.append(f"  - 退出码：{item.get('exit_code')}（预定 {item.get('expect_exit_code')}）")
        diff_path = item.get("diff_path")
        if diff_path:
            lines.append(f"  - 源码差异：`{diff_path}`")
        lines.append("  - 证据：`verification.json`")
        return lines
    if kind == "allowed_changes":
        changed = item.get("changed") or []
        label = "、".join(str(path) for path in changed) if isinstance(changed, list) and changed else "无文件变化"
        lines = [f"- allowed_changes `{label}`：{status}"]
        illegal = item.get("illegal") or []
        if isinstance(illegal, list) and illegal:
            lines.append("  - 越界文件：" + "、".join(str(path) for path in illegal))
        lines.append("  - 证据：`verification.json`")
        return lines
    path = str(item.get("path") or "").strip() or kind
    return [f"- {kind} `{path}`：{status}", "  - 证据：`verification.json`"]


def reason_zh(reason: str) -> str:
    return STOP_REASON_ZH.get(reason, reason)


def _fence(text: str) -> str:
    body = text.replace("```", "'''")
    return f"```text\n{body}\n```"


def render_report(result: RunResult) -> str:
    conclusion = "通过" if result.success else "未通过"
    lines = [
        "# 双 Agent 执行报告",
        "",
        f"- 结论：{conclusion}",
        f"- 停止原因：{result.stop_reason_zh}",
        f"- 调用性质：{result.mode_label}",
        "- 是否重试：否",
        f"- 协调器版本：{__version__}",
        f"- 开始时间：{result.started_at}",
        f"- 结束时间：{result.finished_at}",
        f"- 总耗时：{result.duration_seconds:.3f} 秒",
        "",
        _CLOSING.get(result.stop_reason, "协作已结束。"),
        "",
        "## 任务",
        "",
        f"- 任务编号：{result.task_id}",
        f"- 标题：{result.title}",
        f"- 目标：{result.goal}",
        f"- 工作区：{result.workspace}",
        f"- 项目：{result.project_id or '（无）'}",
        f"- 状态：{result.status or result.stop_reason_zh}",
        f"- 修复次数：{result.repairs_used}",
        f"- Codex 调用：{result.codex_calls}",
        f"- Cursor 调用：{result.cursor_calls}",
        _token_line(result),
        f"- Worktree：{result.worktree_path or '（未使用）'}",
        f"- 已执行轮次：{result.rounds_executed}",
        f"- 运行目录：{result.run_dir}",
        f"- 子进程已清理：{'是' if result.processes_cleared else '否'}",
        f"- 写入锁已释放：{'是' if result.lock_released else '否'}",
        "",
    ]
    if result.verification:
        lines.extend(["## 独立验证", ""])
        lines.append("每轮源码差异保存在对应的 `rounds/*/changes.diff`。汇总证据在 `verification.json`。")
        if any(item.get("baseline") for item in result.verification):
            lines.append("预置失败的命令输出和当时的源码保存在 `baseline/`。那一次验证发生在模型调用之前，不是伪造的 Agent 回合。")
        lines.append("")
        for item in result.verification:
            lines.extend(_verification_lines(item))
        lines.append("")
    if result.error_message:
        lines.extend(["## 错误", "", result.error_message, ""])
    if result.warnings:
        lines.extend(["## 警告", ""])
        lines.extend(f"- {item}" for item in result.warnings)
        lines.append("")
    lines.extend(
        [
            "## 轮次",
            "",
            "| 轮次 | 参与者 | 角色 | 执行器 | 是否真正调用 | 退出码 | 分类 | 耗时（秒） |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not result.messages:
        lines.append("| - | - | - | - | 否 | - | - | - |")
    for message in result.messages:
        role = ROLES.get(message.role)
        role_title = role.title if role else message.role
        exit_code = "-" if message.exit_code is None else str(message.exit_code)
        invoked = "是" if message.invoked else "否"
        lines.append(
            "| {round} | {sender} | {role} | {executor} | {invoked} | {code} | {kind} | {duration:.3f} |".format(
                round=message.round_index,
                sender=message.sender,
                role=role_title,
                executor=message.executor,
                invoked=invoked,
                code=exit_code,
                kind=reason_zh(message.error_kind),
                duration=message.duration_seconds,
            )
        )
    lines.append("")
    for message in result.messages:
        role = ROLES.get(message.role)
        role_title = role.title if role else message.role
        lines.extend(
            [
                f"### 第 {message.round_index} 轮 · {message.sender} · {role_title}",
                "",
                f"- 调用性质：{message.mode_label}",
                f"- 交接给：{message.receiver or '（无）'}",
                f"- 父消息：{message.parent_id or '（无）'}",
                f"- 正文摘要：`{message.content_sha256}`",
                f"- 判定：{message.decision or '（无）'}",
                f"- 疑似提示注入：{'是' if message.injection_suspected else '否'}",
            ]
        )
        if message.unsafe_paths:
            lines.append("- 工作区外路径：" + "、".join(message.unsafe_paths))
        if message.error_message:
            lines.extend(["", "错误说明：", "", _fence(message.error_message)])
        lines.extend(["", "最终回答：", "", _fence(message.content or "（空）"), ""])
    lines.extend(["## 消息完整性", ""])
    if not result.messages:
        lines.append("没有已落盘的交接消息。")
    else:
        lines.append("每条消息的 SHA-256 都按 UTF-8 正文计算。下一轮开始前会从磁盘重新读取并校验。")
        for message in result.messages:
            lines.append(f"- 第 {message.round_index} 轮 `{message.id}`：`{message.content_sha256}`")
    lines.extend(
        [
            "",
            "## 现场文件",
            "",
            "- `task.json`：本次任务配置",
            "- `result.json`：机器可读结果",
            "- `messages.jsonl`：交接消息，一行一条",
            "- `events.jsonl`：运行事件",
            "- `rounds/`：每一轮的提示词、标准输出、错误输出、退出信息",
            "- `verification.json`：协调器自己执行的验证，不是模型声明",
            "",
            "## 安全边界",
            "",
            "- 结论为“通过”只表示验收者的合法 accept 加上独立验证都成功。流程结束、模型自称完成，都不是任务成功。",
            "- `write_paths` 只决定调度锁覆盖哪些路径，不会变成 Codex 或 Cursor 的操作系统写入白名单。",
            "- 真实 Cursor 开发者在文件隔离完成前一律不启动。Ask 模式只用于审查、诊断和验收，也不是操作系统沙箱。",
            "- 协调器没有使用 `--yolo`、`--force`、`--approve-for-me` 或 `--dangerously-bypass-approvals-and-sandbox`。",
            "- `codex exec` 0.161.0 不接受 `--ask-for-approval`。真实调用只使用该版本帮助里存在的沙箱参数。",
            "- 写入冲突使用协调器统一锁注册表。任务配置里的独立 lock_dir 会被拒绝。",
            "- 另一个 Agent 的输出放在 `UNTRUSTED_AGENT_OUTPUT` 中。协调器不会把这些文字当成命令执行。",
            "- 日志会脱敏常见密钥样式。不要把真实密钥写进任务目标。",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(result: RunResult) -> None:
    result.report_path.write_text(render_report(result), encoding="utf-8", newline="\n")


def _token_line(result: RunResult) -> str:
    if "真实" in result.mode_label:
        return (
            f"- Token：输入 {result.input_tokens}，输出 {result.output_tokens}"
            "（只统计每轮输出里最后一条 usage；没有 usage 时为 0）"
        )
    return "- Token：不适用（本次不是真实模型调用，不把 0 当成模型用量）"


def render_batch_report(data: dict[str, object]) -> str:
    """批次汇总。模型验收和协调器验证分开写。"""
    tasks = data.get("tasks") or []
    if not isinstance(tasks, list):
        tasks = []
    lines = [
        "# 多项目批次报告",
        "",
        f"- 批次 ID：{data.get('batch_id')}",
        f"- 调用性质：{data.get('call_mode')}",
        f"- 并发上限：{data.get('max_concurrent')}",
        f"- 实际峰值并发：{data.get('peak_running')}",
        f"- 是否出现时间重叠：{'是' if data.get('overlapped') else '否'}",
        f"- 开始：{data.get('started_at')}",
        f"- 结束：{data.get('finished_at')}",
        f"- 总耗时：{data.get('duration_seconds')} 秒",
        f"- Codex 调用：{data.get('codex_calls')}",
        f"- Cursor 调用：{data.get('cursor_calls')}",
        f"- Token 输入：{data.get('input_tokens')}",
        f"- Token 输出：{data.get('output_tokens')}",
        "",
        "Token 只来自真实 CLI 输出中的 usage 字段。模拟调用记为不适用，不写成模型消耗。",
        "协调器验证是预定命令的退出码；模型 accept 只是验收意见，二者不是同一件事。",
        "",
        "## 任务",
        "",
    ]
    if not tasks:
        lines.append("没有任务。")
    for item in tasks:
        if not isinstance(item, dict):
            continue
        lines.extend(
            [
                f"### {item.get('project_name') or item.get('project_id') or '未命名项目'} / {item.get('task_id')}",
                "",
                f"- 调度 ID：{item.get('schedule_id')}",
                f"- 状态：{item.get('state')}",
                f"- 停止原因：{item.get('stop_reason') or '（无）'}",
                f"- 调用性质：{item.get('mode_label') or '未执行'}",
                f"- 开始：{item.get('started_at') or '（未开始）'}",
                f"- 结束：{item.get('finished_at') or '（未结束）'}",
                f"- 工作区：{item.get('workspace') or '（无）'}",
                f"- Codex 调用：{item.get('codex_calls', 0)}",
                f"- Cursor 调用：{item.get('cursor_calls', 0)}",
                f"- Token 输入/输出：{item.get('input_tokens', 0)} / {item.get('output_tokens', 0)}",
                f"- 修复次数：{item.get('repairs_used', 0)}",
                f"- 协调器验证：{item.get('verification_text') or '（无）'}",
                f"- 模型验收：{item.get('acceptance') or '（无）'}",
                f"- Git diff：{item.get('diff_paths') or '（无）'}",
                f"- 报告：{item.get('report_path') or '（无）'}",
                f"- 错误：{item.get('error_message') or '（无）'}",
                "",
            ]
        )
    issues = data.get("issues") or []
    lines.extend(["## 尚未解决", ""])
    if isinstance(issues, list) and issues:
        lines.extend(f"- {item}" for item in issues)
    else:
        lines.append("无。")
    lines.extend(
        [
            "",
            "## 并发",
            "",
            str(data.get("concurrency_note") or "无额外说明。"),
            "",
        ]
    )
    return "\n".join(lines) + "\n"
