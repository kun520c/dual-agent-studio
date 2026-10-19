"""Read-only rehearsal checklist. It never starts a CLI or installs a signer."""
from __future__ import annotations
import json
from pathlib import Path

from dual_agent import PROJECT_ROOT
from dual_agent.coordinator import default_lock_dir
from dual_agent.execution_gate import digest, exact_path
from dual_agent.locks import default_resource_lock_dir, pid_alive
from dual_agent.safety import is_reparse_point, paths_overlap
from dual_agent.status import read_status

from .task_api import ACTIVE, BusinessError, task_request


def inspect(policy, raw):
    value = task_request(raw)
    execution = value.get('executionMode', 'mock')
    items = []

    def add(code, title, status, detail):
        items.append({'code': code, 'title': title, 'status': status, 'detail': detail[:500]})

    try:
        config = policy.build(value, 'studio-validation')
    except BusinessError as exc:
        add('authorization', '签名授权与配置', 'BLOCKED', f'{exc.code}：{exc}')
        add('cli', 'CLI 程序与摘要', 'NOT CHECKED', '授权未通过。未读取未签名路径，未启动任何 CLI。')
        add('workspace', '专属测试仓库', 'NOT CHECKED', '未使用未签名内容作为工作区。')
        add('gate', '真实执行门控', 'PASS', '预检没有放行执行。真实模型调用 0。')
        return _report(execution, items)
    if execution == 'mock':
        _mock(policy, config, add)
    else:
        _cli(policy, config, add)
    add('gate', '真实执行门控', 'PASS', '预检只读校验，不能放行真实 CLI，也不能安装签名公钥。')
    return _report(execution, items)


def _report(execution, items):
    return {'ready': all(item['status'] != 'BLOCKED' for item in items), 'mode': execution,
            'realModelCalls': 0, 'items': items}


def _mock(policy, config, add):
    add('authorization', '签名授权与配置', 'PASS', 'Mock 不读取真实授权，也不会启动 Codex 或 Cursor。')
    add('cli', 'CLI 程序与摘要', 'PASS', '当前来源是 Mock。真实模型调用 0。')
    _workspace_dir(config.workspace, '隔离模拟工作区', add)
    _budget(config, add)
    _verification(config, add, signed=False)
    _locks(policy, config, add)


def _cli(policy, config, add):
    authority = config.execution_authority
    payload = authority.payload
    _signature(policy, authority, add)
    _source_repo(config, add)
    _worktree(config, add)
    _budget(config, add)
    _verification(config, add, signed=True)
    _locks(policy, config, add)
    if config.execution_mode == 'offline_cli':
        script = PROJECT_ROOT / 'desktop-validation/tests/fake_cli_v05.py'
        files = payload.get('files') or {}
        if str(script) not in files or digest(script) != files[str(script)]:
            add('cli', 'CLI 程序与摘要', 'BLOCKED', '离线替身未固定在签名摘要中。未启动任何程序。')
        else:
            add('cli', 'CLI 程序与摘要', 'PASS', '离线授权只会重定向到已签名替身，不会启动本机 Codex/Cursor。真实模型调用 0。')
        return
    _real_prefix(config, add)


def _signature(policy, authority, add):
    payload = authority.payload
    used = 0
    ledger = policy.runs / 'studio-host' / 'requests'
    if ledger.is_dir():
        for path in ledger.glob('*.json'):
            try:
                row = json.loads(path.read_text(encoding='utf8'))
            except (OSError, ValueError):
                continue
            if row.get('authorization') == authority.identity:
                used += 1
    remaining = payload.get('maxTasks', 1) - used
    if remaining <= 0:
        add('signature', '签名授权状态', 'BLOCKED', '已达到该签名授权的任务次数上限。预检没有消耗新的次数。')
        return
    add('signature', '签名授权状态', 'PASS',
        f"签名者 {payload.get('signer')}，模式 {payload.get('mode')}，剩余任务 {remaining}/{payload.get('maxTasks')}。预检没有新增签名者。")


def _workspace_dir(raw, title, add):
    try:
        path = exact_path(raw)
    except Exception as exc:
        add('workspace', title, 'BLOCKED', str(exc))
        return None
    if not path.is_dir() or is_reparse_point(path):
        add('workspace', title, 'BLOCKED', '工作区不存在、不是目录，或是重解析点。')
        return None
    add('workspace', title, 'PASS', f'目录可检查：{path.name}。本次未创建 Worktree。')
    return path


def _source_repo(config, add):
    path = _workspace_dir(config.workspace, '专属测试仓库', add)
    if path is None:
        return
    git = path / '.git'
    if not git.exists() or is_reparse_point(git):
        add('repository', 'Git 仓库', 'BLOCKED', '专属副本不是可检查的 Git 仓库。没有执行 git 命令。')
        return
    add('repository', 'Git 仓库', 'PASS', '发现普通 .git 标记。没有执行 git 命令，也没有创建 Worktree。')


def _worktree(config, add):
    try:
        root = exact_path(str(config.worktree.root))
        repo = exact_path(config.workspace)
    except Exception as exc:
        add('worktree', 'Worktree 范围', 'BLOCKED', str(exc))
        return
    if root.exists() and (not root.is_dir() or is_reparse_point(root)):
        add('worktree', 'Worktree 范围', 'BLOCKED', 'Worktree 根不是普通目录。')
        return
    if root == repo or root.is_relative_to(repo) or repo.is_relative_to(root):
        add('worktree', 'Worktree 范围', 'BLOCKED', 'Worktree 与源仓库重叠。')
        return
    state = '目录已存在' if root.exists() else '执行时创建'
    add('worktree', 'Worktree 范围', 'PASS', f'授权根{state}。目录检查不是操作系统级隔离。本次未创建 Worktree。')


def _budget(config, add):
    flow = len(config.flow)
    repairs = config.max_repair_rounds
    calls = config.max_model_calls
    detail = (f'上限 {calls} 次、{config.max_rounds} 轮、修复 {repairs} 次、'
              f'总时长 {config.max_run_seconds} 秒、单步 {config.timeout_seconds} 秒。固定流程至少 {flow} 次。')
    if calls < flow:
        add('budget', '预算、超时和调用次数', 'BLOCKED', detail + '调用次数不足以走完开发、审查和验收。宿主仍会把更小的额度当作运行中的预算停止，但预检不把它当作可联调配置。')
        return
    if repairs and calls < flow + repairs:
        detail += f' 若用满 {repairs} 次修复，验收前需要 {flow + repairs} 次调用，当前额度可能在验收前耗尽。'
    add('budget', '预算、超时和调用次数', 'PASS', detail)


def _verification(config, add, signed):
    commands = [item for item in config.verification if item.type == 'command']
    scopes = [item for item in config.verification if item.type == 'allowed_changes']
    if not commands or not scopes:
        add('verification', '可信测试命令', 'BLOCKED', '缺少可信命令或改动范围检查。预检未执行测试。')
        return
    names = [Path(item.argv[0]).name for item in commands if item.argv]
    suffix = '程序摘要已由签名覆盖。' if signed else '使用项目已登记预设。'
    add('verification', '可信测试命令', 'PASS', f"已配置 {'、'.join(names) or '可信命令'}。{suffix}预检未执行测试。")


def _locks(policy, config, add):
    try:
        repo = exact_path(config.workspace)
        root = exact_path(str(config.worktree.root)) if config.worktree.root else repo
    except Exception as exc:
        add('locks', 'Worktree 与任务锁', 'BLOCKED', str(exc))
        return
    owners = []
    for folder, kind in ((default_lock_dir(), 'paths'), (default_resource_lock_dir(), 'resources')):
        if not folder.is_dir():
            continue
        for path in folder.glob('*.lock'):
            try:
                data = json.loads(path.read_text(encoding='utf8'))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            pid = data.get('pid')
            if isinstance(pid, int) and not pid_alive(pid):
                continue
            if kind == 'paths' and any(paths_overlap(repo, Path(item)) or paths_overlap(root, Path(item)) for item in data.get('paths') or [] if isinstance(item, str)):
                owners.append(str(data.get('task_id') or '未知任务'))
            if kind == 'resources':
                held = {str(item).casefold() for item in data.get('resources') or []}
                if held & {name.casefold() for name in config.resource_locks}:
                    owners.append(str(data.get('task_id') or '未知任务'))
    active = []
    schedule = policy.runs / 'schedule'
    if schedule.is_dir():
        for path in schedule.glob('sch-studio-*.json'):
            row = read_status(path)
            if row.get('project_id') == config.project_id and row.get('state') in ACTIVE:
                active.append(str(row.get('task_id') or row.get('schedule_id')))
    if owners or active:
        add('locks', 'Worktree 与任务锁', 'PASS',
            '注意：' + ('写入或资源锁被 ' + '、'.join(owners) + ' 占用。' if owners else '')
            + ('同项目仍有活动调度 ' + '、'.join(active) + '。' if active else '')
            + '提交后会等待或重新排队；宿主退出不会自动重跑。预检没有获取锁。')
        return
    add('locks', 'Worktree 与任务锁', 'PASS', '没有覆盖该范围的活动写入锁。锁要到执行时才获取，预检没有加锁。')


def _real_prefix(config, add):
    from dual_agent.executors import AgentRequest, CodexExecutor, CursorExecutor
    payload = config.execution_authority.payload
    workspace = Path(config.workspace)
    problems = []
    for name, role, builder in (
        ('codex', 'developer', CodexExecutor().build_command),
        ('cursor', 'reviewer', CursorExecutor().build_command),
    ):
        prefix = list(payload['cli'][name])
        request = AgentRequest(
            task_id='studio-validation', round_index=1, agent_name='preflight', role=role, executor=name,
            prompt='预检不发送此提示', workspace=workspace, timeout_seconds=config.timeout_seconds, scenario='ok',
            prompt_path=workspace / 'prompt.txt', last_message_path=workspace / 'last_message.txt',
            allow_real_codex=True, allow_real_cursor=True, codex_bin=config.codex_bin, cursor_bin=config.cursor_bin,
            write_paths=[workspace], execution_authority=config.execution_authority, execution_mode='real',
        )
        try:
            command = builder(request)
        except Exception as exc:
            problems.append(f'{name} 启动形状无效：{exc}')
            continue
        if command[:len(prefix)] != prefix:
            problems.append(f'{name} 启动命令与签名前缀不一致')
            continue
        for part in prefix:
            path = exact_path(part)
            if not path.is_file():
                problems.append(f'{name} 批准程序不存在：{path.name}')
            elif digest(path) != _digest_for(payload, part):
                problems.append(f'{name} 程序摘要与签名不一致：{path.name}')
    if problems:
        add('cli', 'CLI 程序与摘要', 'BLOCKED', '；'.join(problems) + '。未启动程序。')
        return
    names = [Path(part).name for name in ('codex', 'cursor') for part in payload['cli'][name]]
    add('cli', 'CLI 程序与摘要', 'PASS', '启动前缀与签名一致：' + '、'.join(names) + '。未启动程序。')


def _digest_for(payload, raw):
    target = exact_path(raw)
    for key, value in payload['files'].items():
        if exact_path(key) == target:
            return value
    return ''
