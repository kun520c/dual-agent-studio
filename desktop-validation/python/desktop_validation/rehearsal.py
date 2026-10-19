"""Built-in mock rehearsal. Registration of a user repository never grants this.

The sample id, directory, participants, budget and trusted command are fixed in
code. A JSON file, including mock-policy.json, cannot add another workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from dual_agent.models import ConfigError
from dual_agent.projects import load_project_file, register_project, show_project, update_project
from dual_agent.safety import finalize_declared, is_inside, is_reparse_point

SAMPLE_ID = 'studio-rehearsal'
SAMPLE_NAME = '安全演练'
SAMPLE_DESCRIPTION = '内置安全演练。只在应用管理的样本目录运行固定 Mock 模板，结果不是真实模型。'
SCRIPT_SHA256 = 'ae50ed1dbdcc8158515904eb72c1f0d26c820404223f009f4f07aad7be3c176c'
_INITIAL_VALUE = '4\n初始模拟样本\n'
_TRUSTED = Path(__file__).resolve().parents[2] / 'trusted' / 'verify_mock.py'


def _error(code, message):
    from .task_api import BusinessError
    raise BusinessError(code, message)


def sample_workspace(runs: Path) -> Path:
    return Path(runs).parent / 'studio-samples' / SAMPLE_ID / 'workspace'


def _lexical(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def _same(left: str, right: str) -> bool:
    return os.path.normcase(str(finalize_declared(left))) == os.path.normcase(str(finalize_declared(right)))


def assert_plain_dir(boundary: Path, target: Path) -> Path:
    """Reject a junction or symlink anywhere from the app root down to target."""
    boundary = Path(boundary)
    target = Path(target)
    if boundary.exists() and is_reparse_point(boundary):
        _error('PATH_DENIED', '应用根目录是重解析点，拒绝创建演练目录')
    if not target.is_absolute():
        target = boundary / target
    try:
        relative = target.relative_to(boundary)
    except ValueError:
        _error('PATH_DENIED', '演练目录必须位于应用管理目录内')
    if any(part in {'.', '..'} or part != part.strip() for part in relative.parts):
        _error('PATH_DENIED', '演练目录包含非法路径分量')
    current = boundary
    for part in relative.parts:
        current = current / part
        if current.exists() and is_reparse_point(current):
            _error('PATH_DENIED', '演练目录不能通过重解析点指向其他位置')
    if not current.exists() or not current.is_dir():
        _error('PATH_DENIED', '演练目录不存在')
    final = finalize_declared(str(current))
    boundary_final = finalize_declared(str(boundary))
    if not is_inside(final, boundary_final) or final == boundary_final:
        _error('PATH_DENIED', '演练目录越出应用管理目录')
    if _lexical(final) != _lexical(current):
        _error('PATH_DENIED', '演练目录的最终路径与登记路径不一致')
    return final


def _trusted_file() -> Path:
    if not _TRUSTED.is_file() or is_reparse_point(_TRUSTED) or _TRUSTED.is_symlink():
        _error('PRESET_INVALID', '内置可信脚本不存在或不是普通文件')
    if _TRUSTED.stat().st_size > 1024 * 1024:
        _error('PRESET_INVALID', '内置可信脚本超过大小上限')
    digest = hashlib.sha256(_TRUSTED.read_bytes()).hexdigest()
    if digest != SCRIPT_SHA256:
        _error('PRESET_INVALID', '内置可信脚本与程序内固定摘要不一致，演练已阻断')
    final = finalize_declared(str(_TRUSTED))
    if is_reparse_point(final) or _lexical(final) != _lexical(_TRUSTED):
        _error('PRESET_INVALID', '内置可信脚本路径被重定向')
    return final


def _mkdir_plain(boundary: Path, relative: Path) -> Path:
    current = Path(boundary)
    for part in relative.parts:
        current = current / part
        if current.exists() and is_reparse_point(current):
            _error('PATH_DENIED', '演练目录不能通过重解析点指向其他位置')
        if not current.exists():
            current.mkdir()
        if is_reparse_point(current) or not current.is_dir():
            _error('PATH_DENIED', '演练目录不能通过重解析点指向其他位置')
    return assert_plain_dir(boundary, current)


def _expected_argv(workspace: Path, trusted: Path) -> list[str]:
    return [str(finalize_declared(sys.executable)), str(trusted), str(workspace)]


def _matches(project, workspace: Path, trusted: Path) -> None:
    if project.project_id != SAMPLE_ID or project.name != SAMPLE_NAME or project.description != SAMPLE_DESCRIPTION:
        _error('REHEARSAL_MISMATCH', '演练项目偏离内置模板。修改 JSON 不能恢复执行权限，可在应用内重置演练项目。')
    if not _same(project.workspace, str(workspace)) or len(project.allowed_roots) != 1 or not _same(project.allowed_roots[0], str(workspace)):
        _error('PATH_DENIED', '演练项目只能使用应用样本目录，不能指向其他仓库')
    expected_roles = [('rehearsal-dev', 'mock', 'developer'), ('rehearsal-review', 'mock', 'reviewer'), ('rehearsal-accept', 'mock', 'acceptor')]
    actual_roles = [(item.id, item.executor, item.role) for item in project.participants]
    if actual_roles != expected_roles:
        _error('REHEARSAL_MISMATCH', '演练参与者必须是内置 Mock 开发、审查和验收，不能改成真实 CLI')
    if len(project.verification) != 2:
        _error('REHEARSAL_MISMATCH', '演练可信检查必须使用内置的两项预设')
    scope, command = project.verification
    if scope.type != 'allowed_changes' or scope.allowed_paths != ['value.txt'] or scope.require_paths != ['value.txt']:
        _error('REHEARSAL_MISMATCH', '演练改动范围被改动，执行已阻断')
    argv = list(command.argv)
    expected = _expected_argv(workspace, trusted)
    if command.type != 'command' or len(argv) != 3 or command.sha256.lower() != SCRIPT_SHA256:
        _error('REHEARSAL_MISMATCH', '演练可信命令不是内置脚本，执行已阻断')
    if not (_same(argv[0], expected[0]) and _same(argv[1], expected[1]) and _same(argv[2], expected[2])):
        _error('REHEARSAL_MISMATCH', '演练可信命令路径偏离内置脚本，执行已阻断')
    if not _same(command.cwd, str(trusted.parent)) or int(command.expect_exit_code) != 0 or float(command.timeout_seconds) != 10:
        _error('REHEARSAL_MISMATCH', '演练可信命令的工作目录或超时偏离模板')
    if any(getattr(command, name) for name in ('path', 'text')) or any(getattr(scope, name) for name in ('path', 'text', 'sha256', 'argv', 'cwd')):
        _error('REHEARSAL_MISMATCH', '演练验证项含有模板之外的内容')
    if project.worktree.enabled or project.worktree.root or project.worktree.path or project.worktree.base_ref != 'HEAD':
        _error('REHEARSAL_MISMATCH', '演练项目不能启用 Worktree 或改写其路径')
    if project.resource_locks or project.max_write_tasks != 1 or project.allow_shared_context:
        _error('REHEARSAL_MISMATCH', '演练项目的锁和并发设置偏离模板')
    numbers = (
        (project.max_model_calls, 6), (project.max_rounds, 4), (project.max_repair_rounds, 1),
        (float(project.timeout_seconds), 25.0), (float(project.max_run_seconds), 90.0),
    )
    if any(left != right for left, right in numbers):
        _error('REHEARSAL_MISMATCH', '演练预算偏离内置上限')


def load_rehearsal(projects: Path, runs: Path, _policy: Path | None = None):
    """Authorize only the fixed sample. The policy file is intentionally unused."""
    del _policy
    path = Path(projects) / (SAMPLE_ID + '.json')
    if not path.is_file() or path.is_symlink() or is_reparse_point(path):
        _error('PROJECT_DENIED', '尚未创建内置演练项目。只能由应用在样本目录创建，不能登记任意仓库。')
    if path.stat().st_size > 256 * 1024:
        _error('PRESET_INVALID', '演练登记超过大小上限')
    try:
        if not finalize_declared(str(path)).is_relative_to(finalize_declared(str(projects))):
            _error('PATH_DENIED', '演练登记文件越过项目目录')
        project = show_project(SAMPLE_ID, projects)
    except (ConfigError, OSError, ValueError) as exc:
        _error('REHEARSAL_MISMATCH', '演练登记无法读取，执行已阻断。' + str(exc)[:120])
    trusted = _trusted_file()
    workspace = assert_plain_dir(Path(runs).parent, sample_workspace(runs))
    sample = workspace / 'value.txt'
    if not sample.is_file() or sample.is_symlink() or is_reparse_point(sample):
        _error('PATH_DENIED', '演练样本 value.txt 必须是样本目录里的普通文件')
    _matches(project, workspace, trusted)
    if not project.enabled:
        _error('PROJECT_DISABLED', '演练项目已禁用。历史仍可阅读；启用不会把其他仓库加入白名单。')
    return project


def _template(workspace: Path, trusted: Path) -> dict:
    return {
        'project_id': SAMPLE_ID,
        'name': SAMPLE_NAME,
        'workspace': str(workspace),
        'allowed_roots': [str(workspace)],
        'enabled': True,
        'description': SAMPLE_DESCRIPTION,
        'participants': [
            {'id': 'rehearsal-dev', 'executor': 'mock', 'role': 'developer'},
            {'id': 'rehearsal-review', 'executor': 'mock', 'role': 'reviewer'},
            {'id': 'rehearsal-accept', 'executor': 'mock', 'role': 'acceptor'},
        ],
        'verification': [
            {'type': 'allowed_changes', 'allowed_paths': ['value.txt'], 'require_paths': ['value.txt']},
            {
                'type': 'command',
                'argv': _expected_argv(workspace, trusted),
                'cwd': str(trusted.parent),
                'sha256': SCRIPT_SHA256,
                'timeout_seconds': 10,
            },
        ],
        'max_model_calls': 6,
        'max_rounds': 4,
        'max_repair_rounds': 1,
        'timeout_seconds': 25,
        'max_run_seconds': 90,
        'concurrency': {'max_write_tasks': 1, 'allow_shared_context': False},
        'resource_locks': [],
        'worktree': {'enabled': False, 'base_ref': 'HEAD', 'root': '', 'path': ''},
    }


def _git(workspace: Path) -> None:
    flags = 0x08000000 if os.name == 'nt' else 0
    if not (workspace / '.git').exists():
        created = subprocess.run(['git', 'init', '--quiet', str(workspace)], capture_output=True, timeout=15, creationflags=flags)
        if created.returncode:
            _error('REHEARSAL_SETUP', '演练样本的 Git 初始化失败，没有登记项目')
    if (workspace / '.git').exists() and is_reparse_point(workspace / '.git'):
        _error('PATH_DENIED', '演练样本的 .git 不能是重解析点')
    added = subprocess.run(['git', '-C', str(workspace), 'add', '--', 'value.txt'], capture_output=True, timeout=15, creationflags=flags)
    if added.returncode:
        _error('REHEARSAL_SETUP', '演练样本未能登记初始文件，没有扩大执行范围')


def _write_registration(projects: Path, payload: dict, reset: bool) -> None:
    projects.mkdir(parents=True, exist_ok=True)
    target = projects / (SAMPLE_ID + '.json')
    if target.exists() and (target.is_symlink() or is_reparse_point(target)):
        _error('PATH_DENIED', '演练登记文件不是普通文件')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=projects, suffix='.incoming', delete=False) as stream:
        json.dump(payload, stream, ensure_ascii=False)
        incoming = Path(stream.name)
    try:
        load_project_file(incoming)
        if target.exists() and reset:
            update_project(SAMPLE_ID, incoming, projects)
        elif not target.exists():
            register_project(incoming, projects)
        else:
            _error('PROJECT_DUPLICATE', '演练项目已存在')
    finally:
        incoming.unlink(missing_ok=True)


def provision(projects: Path, runs: Path, reset: bool = False, busy=None) -> dict:
    projects = Path(projects)
    runs = Path(runs)
    boundary = runs.parent
    if not boundary.is_dir():
        _error('PATH_DENIED', '应用根目录不存在，不能创建演练样本')
    trusted = _trusted_file()
    root = _mkdir_plain(boundary, Path('studio-samples') / SAMPLE_ID)
    workspace_path = root / 'workspace'
    if workspace_path.exists() and is_reparse_point(workspace_path):
        _error('PATH_DENIED', '演练目录不能通过重解析点指向其他位置')
    if not workspace_path.exists():
        workspace_path.mkdir()
    workspace = assert_plain_dir(boundary, workspace_path)
    existing = projects / (SAMPLE_ID + '.json')
    if existing.is_file() and not reset:
        from .task_api import BusinessError
        try:
            load_rehearsal(projects, runs)
            return {'projectId': SAMPLE_ID, 'created': False, 'reset': False, 'drifted': False, 'workspace': str(workspace), 'policyUntouched': True}
        except BusinessError as exc:
            if exc.code == 'PROJECT_DISABLED':
                return {'projectId': SAMPLE_ID, 'created': False, 'reset': False, 'drifted': False, 'workspace': str(workspace), 'policyUntouched': True}
            if exc.code in {'REHEARSAL_MISMATCH', 'PATH_DENIED', 'PRESET_INVALID'}:
                return {'projectId': SAMPLE_ID, 'created': False, 'reset': False, 'drifted': True, 'workspace': str(workspace), 'policyUntouched': True}
            raise
    if reset and busy and busy():
        _error('PROJECT_BUSY', '演练项目有运行中或排队任务，请结束或取消后再重置')
    value_file = workspace / 'value.txt'
    if value_file.exists() and (value_file.is_symlink() or is_reparse_point(value_file)):
        _error('PATH_DENIED', '演练样本文件不能是重解析点')
    if reset or not value_file.exists():
        value_file.write_text(_INITIAL_VALUE, encoding='utf8')
    _git(workspace)
    _write_registration(projects, _template(workspace, trusted), reset=reset and existing.exists())
    load_rehearsal(projects, runs)
    return {'projectId': SAMPLE_ID, 'created': not (reset and existing.exists()), 'reset': reset, 'drifted': False, 'workspace': str(workspace), 'policyUntouched': True}
