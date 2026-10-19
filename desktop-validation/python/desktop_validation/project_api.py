"""Registry-only desktop operations using the coordinator's project validator.

Editing a registration never approves an executor or changes the mock manifest.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import time
import subprocess
import tempfile
import threading

from dual_agent.grants import reject_registration
from dual_agent.models import ConfigError
from dual_agent.projects import register_project, update_project, show_project
from dual_agent.safety import PACKAGE_DIR, PathRejected, finalize_declared, is_inside
from .rehearsal import SAMPLE_ID, provision
from .task_api import ACTIVE, BusinessError, dto, identifier

PROJECT_METHODS = ('getProject', 'saveProject', 'setProjectEnabled', 'setDefaultProject', 'inspectAccess', 'inspectWorkspace', 'retireProject', 'createRehearsal')


class ProjectService:
    def __init__(self, store, policy=None):
        self.store = store
        self.policy = policy
        self.root = store.projects_dir
        self.preferences = store.runs_dir / 'studio-preferences.json'
        self.guard = threading.RLock()

    def _path(self, project_id):
        identifier(project_id, 'projectId')
        path = self.root / (project_id + '.json')
        if path.is_symlink() or not finalize_declared(str(path)).is_relative_to(self.root):
            raise BusinessError('PATH_DENIED', '项目登记路径不安全')
        if path.exists() and path.stat().st_size > 256 * 1024:
            raise BusinessError('CONFIG_SIZE', '项目配置超过读取上限')
        return path

    def default_id(self):
        if not self.preferences.exists(): return ''
        if self.preferences.is_symlink() or not self.preferences.resolve().is_relative_to(self.store.runs_dir):
            raise BusinessError('PATH_DENIED', '桌面偏好路径不安全')
        if self.preferences.stat().st_size > 12000: raise BusinessError('CONFIG_SIZE', '桌面偏好超过上限')
        return json.loads(self.preferences.read_text(encoding='utf8')).get('defaultProjectId', '')

    def _project_busy(self, project_id):
        for path in (self.store.runs_dir / 'schedule').glob('*.json'):
            if path.is_symlink() or path.stat().st_size > 256 * 1024: continue
            try: status = json.loads(path.read_text(encoding='utf8'))
            except (OSError, ValueError): continue
            if status.get('project_id') == project_id and status.get('state') in ACTIVE:
                return True
        return False

    def _editable(self, project):
        reject_registration(project.workspace)
        for root in project.allowed_roots: reject_registration(root)
        if self._project_busy(project.project_id):
            raise BusinessError('PROJECT_BUSY', '项目有运行中或排队任务，请结束或取消后再修改配置')

    def list_rows(self):
        default = self.default_id()
        rows = []
        for row in self.store.list_projects():
            item = dict(row, isDefault=row['id'] == default)
            if row.get('configError'):
                path = self.root / (row['id'] + '.json')
                if path.is_file() and not path.is_symlink():
                    item['revision'] = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(item)
        return rows

    def _write_default(self, project_id):
        self.default_id()
        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        pending = self.preferences.with_suffix('.pending')
        if pending.is_symlink(): raise BusinessError('PATH_DENIED', '偏好临时文件不安全')
        pending.write_text(json.dumps({'defaultProjectId': project_id}), encoding='utf8')
        os.replace(pending, self.preferences)

    def get(self, raw):
        value = dto(raw, {'projectId'}, {'projectId'})
        path = self._path(value['projectId'])
        project = show_project(value['projectId'], self.root)
        executable = False; reason = '真实 Codex/Cursor 执行由 Python 后端阻断'
        if self.policy:
            try:
                self.policy.project(project.project_id); executable = True
                reason = '内置安全演练已通过后端模板校验' if project.project_id == SAMPLE_ID else '已批准的隔离 Mock 项目'
            except BusinessError as error: reason = str(error)
        modes = self.policy.execution_status(project.project_id) if self.policy else {}
        # Read-only presentation of the existing host ledger. A valid signature
        # alone says nothing about its remaining task count. Submission gates
        # remain in TaskPolicy/TaskHost and are not changed here.
        availability = _authorization_availability(self.store.runs_dir, project.project_id)
        if availability and availability['remaining'] == 0:
            mode = availability['mode']
            if mode in modes:
                modes[mode] = {'allowed': False, 'code': 'AUTHORIZATION_BUDGET',
                               'reason': f"历史授权已用尽，剩余 0/{availability['maximum']}；新任务需新的有效授权"}
        git = _git_status(project.workspace, self.store.runs_dir)
        return {'config': project.to_dict(), 'revision': hashlib.sha256(path.read_bytes()).hexdigest(),
                'git': git, 'execution': {'allowed': executable, 'mode': 'mock' if executable else 'blocked', 'reason': reason, 'modes':modes},
                'posture': _posture(project.enabled, modes), 'readout': _readout(project),
                'authorizationUsage': availability,
                'isDefault': self.default_id() == project.project_id}

    def save(self, raw):
        value = dto(raw, {'config', 'create', 'revision'}, {'config', 'create'})
        if type(value['config']) is not dict or type(value['create']) is not bool:
            raise BusinessError('DTO_TYPE', 'config 必须是对象，create 必须是布尔值')
        project_id = value['config'].get('project_id')
        path = self._path(project_id)
        with self.guard:
            if not value['create']:
                current = show_project(project_id, self.root)
                self._editable(current)
                if value.get('revision') != hashlib.sha256(path.read_bytes()).hexdigest():
                    raise BusinessError('CONFIG_CONFLICT', '配置已改变，请重新打开后编辑')
            elif path.exists(): raise BusinessError('PROJECT_DUPLICATE', '项目 ID 已存在：' + project_id)
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=self.root, suffix='.incoming', delete=False) as stream:
                json.dump(value['config'], stream, ensure_ascii=False); incoming = Path(stream.name)
            try:
                # Validate before ANY registry write. This calls the existing backend,
                # including its registration grants, canonical paths and field rules.
                from dual_agent.projects import load_project_file
                candidate = load_project_file(incoming)
                self._editable(candidate)
                workspace = finalize_declared(candidate.workspace)
                if not any(is_inside(workspace, finalize_declared(root)) for root in candidate.allowed_roots):
                    raise BusinessError('PATH_DENIED', '工作区必须位于项目 allowed_roots 内')
                for other in self.root.glob('*.json'):
                    if other == path: continue
                    try: existing = show_project(other.stem, self.root)
                    except ValueError: continue
                    if finalize_declared(existing.workspace) == workspace:
                        raise BusinessError('PROJECT_DUPLICATE', '工作区已登记：' + existing.project_id)
                if value['create']: register_project(incoming, self.root)
                else: update_project(project_id, incoming, self.root)
            finally: incoming.unlink(missing_ok=True)
        return self.get(json.dumps({'projectId': project_id}))

    def enabled(self, raw):
        value = dto(raw, {'projectId', 'enabled', 'revision'}, {'projectId', 'enabled', 'revision'})
        if type(value['enabled']) is not bool: raise BusinessError('DTO_TYPE', 'enabled 必须是布尔值')
        current = self.get(json.dumps({'projectId': value['projectId']}))
        current['config']['enabled'] = value['enabled']
        return self.save(json.dumps({'config': current['config'], 'create': False, 'revision': value['revision']}))

    def set_default(self, raw):
        value = dto(raw, {'projectId'}, {'projectId'})
        self._path(value['projectId'])
        project = show_project(value['projectId'], self.root)
        if not project.enabled: raise BusinessError('PROJECT_DISABLED', '禁用项目不能设为默认项目')
        with self.guard:
            self._write_default(project.project_id)
        return {'defaultProjectId': project.project_id}

    def inspect_workspace(self, raw):
        """Read-only path check. Never registers a project or grants execution."""
        value = dto(raw, {'workspace', 'projectId', 'create'}, {'workspace'})
        if type(value['workspace']) is not str or not value['workspace'].strip() or len(value['workspace']) > 1000:
            raise BusinessError('DTO_TYPE', '工作区路径无效')
        if 'create' in value and type(value['create']) is not bool:
            raise BusinessError('DTO_TYPE', 'create 必须是布尔值')
        creating = value.get('create', True)
        report = {'workspace': value['workspace'].strip(), 'normalized': '', 'accepted': False, 'errors': [], 'warnings': [],
                  'duplicate': None, 'git': {'available': False, 'branch': '', 'changes': '', 'error': '', 'summary': '尚未检查'},
                  'executionGranted': False}
        project_id = value.get('projectId') or ''
        if project_id:
            try: identifier(project_id, 'projectId')
            except BusinessError as exc: report['errors'].append(str(exc))
        try:
            reject_registration(report['workspace'])
            path = finalize_declared(report['workspace'])
        except (ConfigError, PathRejected, OSError, ValueError) as exc:
            report['errors'].append(str(exc)[:300])
            return report
        report['normalized'] = str(path)
        if not path.is_dir():
            report['errors'].append('路径不存在或不是目录')
        elif is_inside(path, PACKAGE_DIR) or path == PACKAGE_DIR:
            report['errors'].append('项目工作区不能是协调器源码目录')
        else:
            report['git'] = _git_status(str(path), self.store.runs_dir)
            if not report['git']['available']:
                report['warnings'].append('未检测到 Git 仓库。' + (report['git']['error'] or '可以登记，但协作前需要先初始化 Git。'))
        if project_id and creating and (self.root / (project_id + '.json')).is_file():
            report['errors'].append('项目 ID 已存在：' + project_id)
        for other in self.root.glob('*.json'):
            if project_id and other.stem == project_id: continue
            if other.is_symlink(): continue
            try: existing = show_project(other.stem, self.root)
            except (ConfigError, OSError, ValueError): continue
            try: same = finalize_declared(existing.workspace) == path
            except (PathRejected, OSError, ValueError): same = False
            if same:
                report['duplicate'] = existing.project_id
                report['errors'].append('工作区已登记：' + existing.project_id)
                break
        report['accepted'] = not report['errors']
        return report

    def retire(self, raw):
        """Remove the registry JSON only. History, worktrees and source stay."""
        value = dto(raw, {'projectId', 'revision', 'confirm'}, {'projectId', 'revision', 'confirm'})
        if value['confirm'] != 'retire-registration-only':
            raise BusinessError('CONFIRM_REQUIRED', '移除登记需要明确确认：只删除登记文件，不删除源码、历史或 Worktree')
        if type(value['revision']) is not str or len(value['revision']) != 64:
            raise BusinessError('DTO_TYPE', 'revision 必须是配置摘要')
        path = self._path(value['projectId'])
        if not path.is_file(): raise BusinessError('PROJECT_MISSING', '项目登记不存在')
        with self.guard:
            if hashlib.sha256(path.read_bytes()).hexdigest() != value['revision']:
                raise BusinessError('CONFIG_CONFLICT', '配置已改变，请重新打开后再移除登记')
            if self._project_busy(value['projectId']):
                raise BusinessError('PROJECT_BUSY', '项目有运行中或排队任务，请结束或取消后再移除登记')
            workspace = ''
            try: workspace = show_project(value['projectId'], self.root).workspace
            except ConfigError: workspace = ''
            path.unlink()
            if self.default_id() == value['projectId']: self._write_default('')
        return {'retired': True, 'projectId': value['projectId'], 'workspacePreserved': workspace,
                'historyPreserved': True, 'sourceDeleted': False}

    def create_rehearsal(self, raw):
        value = dto(raw, {'reset'}, set())
        if 'reset' in value and type(value['reset']) is not bool:
            raise BusinessError('DTO_TYPE', 'reset 必须是布尔值')
        with self.guard:
            info = provision(self.root, self.store.runs_dir, reset=bool(value.get('reset', False)), busy=lambda: self._project_busy(SAMPLE_ID))
        details = self.get(json.dumps({'projectId': info['projectId']}))
        details['rehearsal'] = info
        return details

    def inspect_access(self, raw):
        """Read-only permission explanation. Never signs, installs a key, or starts a CLI."""
        value = dto(raw, {'projectId', 'export'}, {'projectId'})
        if 'export' in value and type(value['export']) is not bool:
            raise BusinessError('DTO_TYPE', 'export 必须是布尔值')
        path = self._path(value['projectId'])
        project = show_project(value['projectId'], self.root)
        modes = self.policy.execution_status(project.project_id) if self.policy else {}
        from dual_agent.execution_gate import TRUSTED_SIGNERS, canonical
        availability = _authorization_availability(self.store.runs_dir, project.project_id)
        if availability and availability['remaining'] == 0 and availability['mode'] in modes:
            modes[availability['mode']] = {'allowed': False, 'code': 'AUTHORIZATION_BUDGET',
                'reason': f"历史授权已用尽，剩余 0/{availability['maximum']}；新任务需新的有效授权"}
        authorization = _authorization_view(self.store.runs_dir / 'studio-authorizations' / (project.project_id + '.json'))
        programs = _cli_view(authorization.get('payload'))
        unsigned = _unsigned_request(project, path, self.root, self.store.runs_dir)
        exported = None
        if value.get('export') is True:
            exported = _write_unsigned(self.store.runs_dir, project.project_id, unsigned, canonical)
        return {
            'projectId': project.project_id,
            'realModelCalls': 0,
            'signerInstalled': bool(TRUSTED_SIGNERS),
            'modes': modes,
            'authorizationUsage': availability,
            'authorization': {key: authorization[key] for key in authorization if key != 'payload'},
            'cli': programs,
            'account': {'status': 'NOT CHECKED', 'detail': '不读取登录凭据、令牌或账号文件。需要你在本机 CLI 中自行登录。'},
            'unsigned': unsigned,
            'export': exported,
            'nextSteps': _next_steps(modes, bool(TRUSTED_SIGNERS)),
            'checklist': _access_checklist(project, path, modes, authorization, programs, bool(TRUSTED_SIGNERS)),
        }


def _git_status(workspace, spool: Path):
    git = {'available': False, 'branch': '', 'changes': '', 'error': '', 'summary': '未检测到 Git 仓库'}
    try:
        command = ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                   '-C', workspace, 'status', '--porcelain=v1', '--branch', '--untracked-files=no', '--ignore-submodules=all']
        spool.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=spool) as output, tempfile.TemporaryFile(dir=spool) as errors:
            result = subprocess.run(command, stdout=output, stderr=errors, timeout=4,
                                    creationflags=0x08000000 if os.name == 'nt' else 0)
            output.seek(0); errors.seek(0)
            stdout = output.read(8192).decode('utf8', errors='replace')
            stderr = errors.read(500).decode('utf8', errors='replace')
        lines = stdout.splitlines()
        git = {'available': result.returncode == 0, 'branch': lines[0] if lines else '',
               'changes': '\n'.join(lines[1:])[:8000], 'error': stderr if result.returncode else '', 'summary': ''}
        if not git['available']: git['summary'] = '未检测到 Git 仓库'
        elif git['changes'].strip(): git['summary'] = '有未提交改动'
        else: git['summary'] = '工作区干净（未列出未跟踪文件）'
    except (OSError, subprocess.TimeoutExpired) as error:
        git['error'] = str(error)[:500]
        git['summary'] = 'Git 状态没有读到'
    return git


def _authorization_availability(runs, project_id):
    """Display-only count; never validates, writes, reserves or grants execution."""
    from dual_agent.execution_gate import canonical
    from dual_agent.safety import is_reparse_point
    runs = Path(runs).resolve()
    envelope = runs / 'studio-authorizations' / (project_id + '.json')
    ledger = runs / 'studio-host' / 'requests'
    try:
        if any(is_reparse_point(p) for p in (envelope, envelope.parent, ledger, ledger.parent)):
            return None
        if not envelope.is_file() or envelope.stat().st_size > 65536:
            return None
        payload = json.loads(envelope.read_text(encoding='utf8'))['payload']
        maximum = payload.get('maxTasks', 1)
        if payload.get('projectId') != project_id or payload.get('mode') not in {'real', 'offline_cli'} or type(maximum) is not int or maximum < 1:
            return None
        identity = hashlib.sha256(canonical(payload)).hexdigest()
        used = 0
        for index, path in enumerate(ledger.glob('*.json')):
            if index >= 10000 or is_reparse_point(path) or path.stat().st_size > 65536:
                return None
            row = json.loads(path.read_text(encoding='utf8'))
            if row.get('authorization') == identity:
                used += 1
        return {'mode': payload['mode'], 'used': used, 'maximum': maximum,
                'remaining': max(0, maximum-used), 'signatureVerifiedByThisRead': False}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _posture(enabled, modes):
    modes = modes or {}
    mock = modes.get('mock') or {}
    real = modes.get('real') or {}
    offline = modes.get('offline_cli') or {}
    states = ['registered']
    if not enabled:
        states.extend(('disabled', 'read_only'))
        next_step = '项目已禁用。历史仍可阅读。启用只恢复登记，不会授予执行权限。'
    else:
        if mock.get('allowed'): states.append('mock_executable')
        if real.get('allowed'): states.append('real_authorized')
        if offline.get('allowed'): states.append('offline_authorized')
        if len(states) == 1: states.append('read_only')
        if mock.get('allowed'):
            next_step = '可以提交 Mock 任务。结果标记为 Mock，不是真实模型。真实 CLI 仍需单独的人工签名。'
        elif real.get('allowed'):
            next_step = '真实 CLI 签名当前有效。提交前仍会重新预检，预检不启动模型。'
        elif offline.get('allowed'):
            next_step = '离线模拟 CLI 已授权。它是受控替身，不能代替真实模型。'
        else:
            next_step = (mock.get('reason') or real.get('reason') or '当前不能执行') + '。登记不会自动获得执行权限。'
    labels = {'registered': '已登记', 'disabled': '已禁用', 'read_only': '只读可用', 'mock_executable': 'Mock 可执行',
              'real_authorized': '真实 CLI 已授权', 'offline_authorized': '离线 CLI 已授权'}
    return {'states': states, 'label': ' · '.join(labels[item] for item in states), 'nextStep': next_step, 'executionGrantedByUi': False}


def _readout(project):
    checks = []
    for item in project.verification:
        if item.type == 'command': checks.append({'type': 'command', 'summary': '已配置可信命令。保存项目不会运行它。'})
        elif item.type == 'allowed_changes': checks.append({'type': 'allowed_changes', 'summary': '改动范围：' + '、'.join(item.allowed_paths)[:180]})
        else: checks.append({'type': item.type, 'summary': item.type})
    worktree = project.worktree.to_dict()
    worktree['summary'] = '未启用 Worktree。任务使用登记的工作区。这不是操作系统沙箱。' if not project.worktree.enabled else '已启用 Worktree。路径由后端在执行时再次检查。这不是操作系统沙箱。'
    return {'verification': checks, 'worktree': worktree,
            'budget': {'modelCalls': project.max_model_calls, 'rounds': project.max_rounds, 'repairs': project.max_repair_rounds,
                       'runSeconds': project.max_run_seconds, 'stepSeconds': project.timeout_seconds}}


def _authorization_view(path: Path):
    view = {'present': False, 'status': 'missing', 'detail': '没有授权文件。GUI 登记不会生成签名。'}
    if path.is_symlink():
        view.update(present=True, status='blocked', detail='授权路径是重解析点，未读取，也未验签。')
        return view
    if not path.exists():
        return view
    view['present'] = True
    try:
        if path.stat().st_size > 65536:
            view.update(status='blocked', detail='授权文件超过大小限制，未读取全文，也未验签。')
            return view
        data = json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        view.update(status='blocked', detail='授权文件无法读取。未验签，未启动 CLI。')
        return view
    if not isinstance(data, dict) or set(data) != {'payload', 'signature'}:
        view.update(status='blocked', detail='授权封装必须同时包含 payload 与 signature。界面没有改写该文件。')
        return view
    payload = data.get('payload') if isinstance(data.get('payload'), dict) else {}
    expires = payload.get('expires') if isinstance(payload.get('expires'), int) else None
    tasks = payload.get('maxTasks') if isinstance(payload.get('maxTasks'), int) else None
    digest = payload.get('projectSha256') if isinstance(payload.get('projectSha256'), str) else ''
    view.update(status='present', payload=payload, detail='发现授权封装。是否接受只由 Python 签名门控决定。',
                signer=str(payload.get('signer') or '')[:80], mode=str(payload.get('mode') or '')[:40],
                expires=expires, maxTasks=tasks, projectSha256=digest[:64])
    return view


def _cli_view(payload):
    empty = {'status': 'NOT CHECKED', 'detail': '没有签名中的程序路径。未启动 Codex 或 Cursor。', 'version': 'NOT CHECKED'}
    view = {'codex': dict(empty), 'cursor': dict(empty)}
    cli = payload.get('cli') if isinstance(payload, dict) else None
    if not isinstance(cli, dict):
        return view
    from dual_agent.execution_gate import exact_path
    for name in ('codex', 'cursor'):
        prefix = cli.get(name)
        if not isinstance(prefix, list) or not prefix or not isinstance(prefix[0], str):
            view[name] = {'status': 'NOT CHECKED', 'detail': '签名没有给出 ' + name + ' 程序。未启动。', 'version': 'NOT CHECKED'}
            continue
        try:
            path = exact_path(prefix[0])
        except (OSError, ValueError) as exc:
            view[name] = {'status': 'BLOCKED', 'detail': str(exc)[:180] + '。未启动程序。', 'version': 'NOT CHECKED'}
            continue
        if path.suffix.lower() != '.exe' or not path.is_file():
            view[name] = {'status': 'BLOCKED', 'detail': '签名路径不是已存在的 exe：' + path.name + '。未启动。', 'version': 'NOT CHECKED'}
            continue
        view[name] = {'status': 'PASS', 'detail': '文件存在：' + path.name + '。未启动，因此没有版本输出，也不表示本部署已接受签名。', 'version': 'NOT CHECKED'}
    return view


def _unsigned_request(project, path: Path, projects: Path, runs: Path):
    return {
        'version': 1,
        'signer': '',
        'mode': 'real',
        'projectId': project.project_id,
        'projects': str(projects),
        'runs': str(runs),
        'workspace': '',
        'worktreeRoot': '',
        'projectSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'expires': 0,
        'maxTasks': 1,
        'budget': {
            'max_model_calls': project.max_model_calls,
            'max_rounds': project.max_rounds,
            'max_repair_rounds': project.max_repair_rounds,
            'max_run_seconds': project.max_run_seconds,
            'timeout_seconds': project.timeout_seconds,
        },
        'files': {},
        'cli': {'codex': [], 'cursor': []},
    }


def _write_unsigned(runs: Path, project_id: str, payload: dict, canonical):
    directory = runs / 'studio-grant-exports'
    if directory.is_symlink():
        raise BusinessError('PATH_DENIED', '待签名导出目录不安全')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (project_id + '.unsigned.json')
    if target.is_symlink() or not target.resolve().is_relative_to(directory.resolve()):
        raise BusinessError('PATH_DENIED', '待签名导出文件不安全')
    encoded = canonical(payload)
    document = {
        'kind': 'unsigned-grant-request',
        'signed': False,
        'realModelCalls': 0,
        'canonicalSha256': hashlib.sha256(encoded).hexdigest(),
        'canonical': encoded.decode('utf8'),
        'payload': payload,
        'instructions': [
            '用你自己保管的私钥在程序外对 canonical 原文签名。',
            '本程序没有生产私钥，不会代签，也不会安装公钥。',
            '签好的 payload 与 signature 由你放到 runs/studio-authorizations/<projectId>.json。',
            '空的 workspace、worktreeRoot、files 和 cli 还不能构成有效授权。',
        ],
    }
    if 'signature' in document or 'signature' in payload:
        raise BusinessError('EXPORT_DENIED', '待签名导出不能包含签名')
    temporary = directory / (project_id + '.unsigned.pending')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    os.replace(temporary, target)
    return {'relative': 'runs/studio-grant-exports/' + target.name, 'bytes': target.stat().st_size}


def _access_checklist(project, path: Path, modes: dict, authorization: dict, programs: dict, signer_installed: bool):
    """Read-only explanation. It does not sign, install a key, or start a CLI."""
    real = (modes or {}).get('real') or {}
    allowed = bool(real.get('allowed'))
    payload = authorization.get('payload') if isinstance(authorization.get('payload'), dict) else {}
    checks = []

    def add(code, title, status, detail, action):
        checks.append({'code': code, 'title': title, 'status': status, 'detail': detail[:400], 'action': action[:300]})

    add('permission', '真实执行权限', 'PASS' if allowed else 'BLOCKED',
        str(real.get('reason') or '后端没有返回真实执行状态'),
        '' if allowed else '登记不会授予执行权限。需要在程序外完成签名，再由 Python 门控决定。')
    add('signer', '授权公钥', 'PASS' if signer_installed else 'BLOCKED',
        '本部署已有人工授权公钥。界面不能替换信任根。' if signer_installed else '本部署没有人工授权公钥。',
        '' if signer_installed else '不能在界面安装公钥，也不能代签。')
    now = int(time.time())
    expires = authorization.get('expires')
    if not authorization.get('present'):
        add('expiry', '授权期限', 'BLOCKED', '没有授权文件。', '导出待签名参数后，在程序外签名。不要复用其他项目的授权。')
    elif authorization.get('status') == 'blocked':
        add('expiry', '授权期限', 'BLOCKED', authorization.get('detail') or '授权文件不可用。', '重新准备授权封装。界面不会改写该文件。')
    elif not isinstance(expires, int) or expires <= 0:
        add('expiry', '授权期限', 'BLOCKED', '授权没有可用的到期时间。', '在程序外填写未过期的 expires 后重新签名。')
    elif expires <= now:
        add('expiry', '授权期限', 'BLOCKED', '授权已过期。不能用这份授权执行新任务。', '重新签发，不要沿用旧授权。')
    else:
        add('expiry', '授权期限', 'PASS' if allowed else 'BLOCKED',
            f'封装中的到期时间尚未过去。是否接受只由签名门控决定。', '' if allowed else '期限字段存在，但签名门控仍未放行。')
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    signed = authorization.get('projectSha256') or ''
    if not signed:
        add('match', '授权与项目配置', 'BLOCKED', '授权没有当前项目配置摘要。', '用导出的参数重新签名。修改项目配置后，旧授权不能继续使用。')
    elif signed != current:
        add('match', '授权与项目配置', 'BLOCKED', '授权摘要与当前项目配置不一致。', '不要用旧授权执行新配置。重新导出并在程序外签名。')
    else:
        add('match', '授权与项目配置', 'PASS' if allowed else 'BLOCKED',
            '配置摘要与授权一致。', '' if allowed else '摘要一致仍不等于可以执行，签名门控尚未放行。')
    _cli_check(payload, programs, allowed, add)
    _budget_check(project, payload, allowed, add)
    locks = '、'.join(project.resource_locks) or '无'
    worktree = project.worktree
    if worktree.enabled and not worktree.root:
        add('worktree', 'Worktree 与资源锁', 'BLOCKED', f'已启用 Worktree，但没有根目录。资源锁：{locks}。这不是操作系统沙箱。', '补上与源仓库不重叠的 Worktree 根，或关闭 Worktree。界面不会创建目录。')
    elif worktree.enabled:
        add('worktree', 'Worktree 与资源锁', 'PASS' if allowed else 'BLOCKED',
            f'Worktree 根已填写。资源锁：{locks}。此处不获取锁，也不是操作系统沙箱。',
            '' if allowed else '配置已填写，但真实执行未授权。提交预检不会创建 Worktree。')
    else:
        add('worktree', 'Worktree 与资源锁', 'NOT CHECKED' if not allowed else 'PASS',
            f'未启用 Worktree，任务使用登记工作区。资源锁：{locks}。这不是操作系统沙箱。',
            '' if allowed else '真实执行未授权。活动锁要到提交预检时才检查，此处不加锁。')
    return checks


def _cli_check(payload, programs, allowed, add):
    codex = (programs or {}).get('codex') or {}
    cursor = (programs or {}).get('cursor') or {}
    files = payload.get('files') if isinstance(payload.get('files'), dict) else {}
    if not files and codex.get('status') == 'NOT CHECKED':
        add('cli', 'CLI 与可信脚本摘要', 'NOT CHECKED', '没有签名中的程序路径。未启动 Codex 或 Cursor。', '在程序外把程序路径和脚本摘要写入授权后再签名。')
        return
    problems = []
    for name in ('codex', 'cursor'):
        item = (programs or {}).get(name) or {}
        if item.get('status') == 'BLOCKED':
            problems.append(item.get('detail') or name)
    from dual_agent.execution_gate import digest, exact_path
    checked = 0
    for raw, expected in list(files.items())[:6]:
        if not isinstance(raw, str) or not isinstance(expected, str):
            problems.append('摘要项格式无效')
            continue
        try:
            file = exact_path(raw)
        except (OSError, ValueError) as exc:
            problems.append(str(exc)[:120])
            continue
        if file.suffix.lower() in {'.pem', '.key'} or 'private' in file.name.lower():
            problems.append('授权里出现疑似私钥路径，未读取')
            continue
        if not file.is_file():
            problems.append(file.name + ' 不存在')
            continue
        if digest(file) != expected:
            problems.append(file.name + ' 摘要不符')
        checked += 1
    if problems:
        add('cli', 'CLI 与可信脚本摘要', 'BLOCKED', '；'.join(problems)[:300] + '。未启动程序。', '更正签名里的程序和脚本摘要。界面不会改写白名单。')
        return
    detail = f"Codex {codex.get('status', 'NOT CHECKED')}，Cursor {cursor.get('status', 'NOT CHECKED')}，已核对摘要 {checked} 项。未启动程序。"
    add('cli', 'CLI 与可信脚本摘要', 'PASS' if allowed and checked else 'BLOCKED' if not allowed else 'PASS',
        detail, '' if allowed else '摘要核对不能代替签名门控。')


def _budget_check(project, payload, allowed, add):
    signed = payload.get('budget') if isinstance(payload.get('budget'), dict) else None
    tasks = payload.get('maxTasks') if isinstance(payload.get('maxTasks'), int) else None
    if not signed:
        add('budget', '预算与任务次数', 'NOT CHECKED',
            f'没有授权预算。项目上限为调用 {project.max_model_calls}、轮次 {project.max_rounds}、修复 {project.max_repair_rounds}。界面不会放宽这些上限。',
            '在授权里写明预算和 maxTasks。剩余次数到提交预检才按账本计算，这里不消耗次数。')
        return
    names = (('max_model_calls', project.max_model_calls), ('max_rounds', project.max_rounds), ('max_repair_rounds', project.max_repair_rounds),
             ('max_run_seconds', project.max_run_seconds), ('timeout_seconds', project.timeout_seconds))
    higher = [name for name, value in names if name in signed and isinstance(signed[name], (int, float)) and value > signed[name]]
    if higher:
        add('budget', '预算与任务次数', 'BLOCKED', '项目配置高于授权预算：' + '、'.join(higher) + '。', '降低项目预算或重新签名。界面不会自动提高授权。')
        return
    add('budget', '预算与任务次数', 'PASS' if allowed else 'BLOCKED',
        f"项目预算未超过授权。授权任务次数 {tasks if tasks is not None else '未记录'}。此处不消耗次数。",
        '' if allowed else '预算字段存在，但真实执行仍未放行。')


def _next_steps(modes: dict, signer_installed: bool):
    steps = []
    mock = modes.get('mock') or {}
    real = modes.get('real') or {}
    offline = modes.get('offline_cli') or {}
    if mock.get('allowed'):
        steps.append('Mock 已由隔离策略允许。提交只会走 Mock，真实模型调用保持 0。')
    elif mock.get('code') == 'POLICY_CHANGED':
        steps.append('项目配置已改变，原 Mock 策略哈希失效。界面不能自行重写策略。需要维护者重新审查 mock-policy。')
    else:
        steps.append('GUI 登记不会加入执行白名单。已批准的隔离 Mock 项目仍可使用。新的安全演练只能由应用创建在自己的样本目录，修改 JSON 不能扩大白名单。')
    if real.get('allowed'):
        steps.append('真实 CLI 的签名门控目前允许该项目。提交前仍会重新预检，预检本身不启动模型。')
    elif not signer_installed:
        steps.append('真实 CLI 被拒绝：本部署没有人工授权公钥。不能在界面安装公钥或代签。')
    else:
        steps.append('真实 CLI 被拒绝：' + str(real.get('reason') or '缺少有效签名授权') + '。')
    if offline.get('allowed'):
        steps.append('离线模拟 CLI 已有独立签名。它使用受控替身，真实模型调用为 0，不能代替真实 CLI。')
    else:
        steps.append('离线模拟 CLI 与真实 CLI 都需要独立签名，不能用来绕过门控。')
    steps.append('下一步若要准备真实授权：导出待签名参数，在程序外补全路径、程序摘要和有效期后自行签名。')
    return steps
