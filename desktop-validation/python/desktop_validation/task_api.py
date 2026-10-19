"""Closed business DTOs. No executor, command, path or permission input."""
from __future__ import annotations
import hashlib
import json
import re
import math
from pathlib import Path
from dual_agent.models import ConfigError
from dual_agent.projects import build_project_task, show_project
from dual_agent.safety import finalize_declared
from dual_agent.execution_gate import ExecutionAuthority, ExecutionDenied, exact_path

TEMPLATES = {
    'verified': ('模拟开发 → 审查 → 可信验收', ['studio_write', 'ok', 'accept']),
    'repair': ('模拟测试失败 → 修复 → 可信验收', ['studio_wrong', 'review_note', 'studio_write', 'accept']),
    'slow': ('长任务 / 取消验证（约 20 秒）', ['studio_slow', 'ok', 'accept']),
    'delayed': ('模拟并行观察（约 3 秒）', ['studio_delay', 'ok', 'accept']),
    'failure': ('模拟子进程异常退出', ['nonzero']),
    'protocol': ('模拟非法协议', ['bad_json']),
    'flow': ('仅模拟流程结束（不验收）', ['studio_write', 'ok']),
    'review': ('只读模拟审查（无写入，不做功能验收）', ['review_note']),
}
ACTIVE = {'queued', 'running', 'verifying', 'reviewing', 'repairing'}

class BusinessError(ValueError):
    def __init__(self, code, message):
        super().__init__(message); self.code = code

def dto(raw, keys, required):
    if not isinstance(raw, str) or len(raw.encode('utf8')) > 12000:
        raise BusinessError('DTO_SIZE', '请求必须为不超过 12,000 字节的 JSON 字符串')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise BusinessError('DTO_FIELD', '不允许重复字段')
            result[key] = value
        return result
    try: value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, BusinessError): raise
        raise BusinessError('DTO_INVALID', '请求不是合法 JSON') from exc
    if type(value) is not dict or set(value) - set(keys) or set(required) - set(value):
        raise BusinessError('DTO_FIELD', '请求字段不在业务白名单内或缺少必要字段')
    return value

def identifier(value, name):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise BusinessError('DTO_TYPE', name + ' 必须是 1–80 位标识符')
    return value

def task_request(raw, submit=False):
    required = {'projectId', 'title', 'goal', 'templateId'} | ({'requestId'} if submit else set())
    value = dto(raw, required | {'budget', 'mode', 'executionMode'}, required)
    execution = value.get('executionMode', 'mock')
    if execution not in {'mock', 'real', 'offline_cli'}: raise BusinessError('MODE_DENIED', '执行来源无效')
    identifier(value['projectId'], 'projectId')
    if type(value['templateId']) is not str or value['templateId'] not in (TEMPLATES if execution == 'mock' else {'collaborate'}):
        raise BusinessError('TEMPLATE_DENIED', '执行模板不在白名单')
    for key, limit in [('title', 120), ('goal', 2000)]:
        item = value[key]
        if type(item) is not str or not item.strip() or len(item) > limit or any(ord(c) < 32 and c not in '\n\t\r' for c in item):
            raise BusinessError('DTO_TYPE', f'{key} 必须是 1–{limit} 字符的文本')
        value[key] = item.strip()
    if submit: identifier(value['requestId'], 'requestId')
    mode = value.get('mode', 'controlled_write')
    if mode not in {'read_only', 'controlled_write'}: raise BusinessError('MODE_DENIED', '只支持只读审查或受控写入')
    if execution != 'mock' and mode != 'controlled_write': raise BusinessError('MODE_DENIED', 'CLI 协作必须使用独立 Worktree')
    if (mode == 'read_only') != (value['templateId'] == 'review'):
        raise BusinessError('MODE_DENIED', '只读审查必须选择只读模板；写入模板不能用于只读模式')
    if 'budget' in value:
        budget = value['budget']
        if type(budget) is not dict or set(budget) - {'modelCalls','rounds','repairs','runSeconds','stepSeconds'}:
            raise BusinessError('DTO_FIELD', '预算字段不在白名单内')
        for field, number in budget.items():
            integer = field in {'modelCalls','rounds','repairs'}
            if type(number) not in ({int} if integer else {int,float}) or not math.isfinite(number) or number < (0 if field == 'repairs' else 0.2 if not integer else 1):
                raise BusinessError('DTO_TYPE', '预算必须为范围内的有限数值：' + field)
    return value

class TaskPolicy:
    def __init__(self, projects: Path, runs: Path, policy: Path):
        self.projects = projects.resolve(); self.runs = runs.resolve(); self.policy = policy.resolve()

    def project(self, project_id):
        identifier(project_id, 'projectId')
        from .rehearsal import SAMPLE_ID, load_rehearsal
        if project_id == SAMPLE_ID:
            return load_rehearsal(self.projects, self.runs, self.policy)
        try:
            if self.policy.stat().st_size>256*1024:raise BusinessError('PRESET_INVALID','模拟策略文件超过大小上限')
            manifest = json.loads(self.policy.read_text(encoding='utf8'))
            entry = manifest['projects'].get(project_id)
            if not entry: raise BusinessError('PROJECT_DENIED', '本阶段只允许明确登记的隔离模拟项目')
            file = self.projects / (project_id + '.json')
            if file.is_symlink() or not file.resolve().is_relative_to(self.projects):
                raise BusinessError('PATH_DENIED', '项目登记路径不安全')
            if file.stat().st_size>256*1024:raise BusinessError('PRESET_INVALID','项目配置超过大小上限')
            if hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
                raise BusinessError('POLICY_CHANGED', '项目预设已改变，需原生侧重新审查登记')
            project = show_project(project_id, self.projects)
            if not project.enabled: raise BusinessError('PROJECT_DISABLED', '项目已禁用')
            sandbox = finalize_declared(manifest['workspaceRoot'])
            workspace = finalize_declared(project.workspace)
            if not workspace.is_relative_to(sandbox) or workspace == sandbox or project.worktree.enabled:
                raise BusinessError('PATH_DENIED', '工作区必须位于隔离模拟根目录')
            if any(finalize_declared(root) != workspace for root in project.allowed_roots):
                raise BusinessError('PATH_DENIED', '模拟项目允许根必须限定为该工作区')
            if not project.participants or any(p.executor != 'mock' for p in project.participants):
                raise BusinessError('MOCK_REQUIRED', '桥接拒绝任何非 mock 参与者')
            roles = [p.role for p in project.participants]
            if roles != ['developer', 'reviewer', 'acceptor']:
                raise BusinessError('PRESET_INVALID', '模拟项目必须登记开发、审查和验收参与者')
            if not any(v.type == 'command' for v in project.verification):
                raise BusinessError('PRESET_INVALID', '缺少项目可信测试预设')
            for script, digest in manifest['trusted'].items():
                script_path = finalize_declared(script)
                if not script_path.is_file() or script_path.stat().st_size>1024*1024:raise BusinessError('PRESET_INVALID','可信脚本不存在或超过大小上限')
                if hashlib.sha256(script_path.read_bytes()).hexdigest() != digest:
                    raise BusinessError('POLICY_CHANGED', '可信测试脚本发生变化')
            return project
        except BusinessError: raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BusinessError('PRESET_INVALID', '无法加载受限模拟项目预设') from exc

    def cli_project(self, project_id, execution):
        identifier(project_id, 'projectId')
        try:
            authority = ExecutionAuthority(self.projects, self.runs, project_id, execution)
            project = show_project(project_id, self.projects)
            if not project.enabled: raise BusinessError('PROJECT_DISABLED', '项目已禁用')
            workspace = exact_path(project.workspace)
            if workspace != exact_path(authority.payload['workspace']) or project.allowed_roots != [str(workspace)]:
                raise ExecutionDenied('项目允许根必须限定为授权源仓库')
            return project, authority
        except ExecutionDenied as exc: raise BusinessError(exc.code, str(exc)) from exc

    def execution_status(self, project_id):
        result = {}
        for mode in ('mock', 'real', 'offline_cli'):
            try:
                if mode == 'mock': self.project(project_id)
                else:
                    self.build({'projectId': project_id, 'title':'授权校验', 'goal':'授权校验', 'templateId':'collaborate', 'executionMode':mode}, 'studio-validation')
                result[mode] = {'allowed':True, 'code':'ALLOWED', 'reason':'已登记隔离 Mock' if mode == 'mock' else '独立签名授权有效'}
            except (ValueError, OSError) as exc: result[mode] = {'allowed':False, 'code':getattr(exc, 'code', 'DENIED'), 'reason':str(exc)}
        return result

    def defaults(self, raw):
        value = dto(raw, {'projectId', 'executionMode'}, {'projectId'})
        execution = value.get('executionMode', 'mock')
        if execution == 'mock': project = self.project(value['projectId'])
        elif execution in {'real', 'offline_cli'}:
            project, _ = self.cli_project(value['projectId'], execution)
            self.build({'projectId': project.project_id, 'title':'授权校验', 'goal':'授权校验', 'templateId':'collaborate', 'executionMode':execution}, 'studio-validation')
        else: raise BusinessError('MODE_DENIED', '执行来源无效')
        return {'projectId': project.project_id, 'mode': execution,
                'execution': self.execution_status(project.project_id),
                'templates': [{'id': key, 'label': item[0]} for key, item in TEMPLATES.items()] if execution == 'mock' else [{'id':'collaborate','label':'Codex 开发后立即可信测试；失败则审查、修复、复测，再验收'}],
                'budget': {'modelCalls': project.max_model_calls, 'rounds': project.max_rounds,
                           'repairs': project.max_repair_rounds, 'runSeconds': project.max_run_seconds,
                           'stepSeconds': project.timeout_seconds},
                'verification': [{'type': v.type, 'label': '项目已配置的可信测试' if v.type == 'command' else '项目改动范围检查'} for v in project.verification],
                'participants': [p.to_dict() for p in project.participants], 'maxConcurrent': 2,
                'worktree': project.worktree.to_dict(), 'workspace': project.workspace,
                'modes': ['read_only', 'controlled_write'], 'assignment': '项目固定开发 → 审查 → 验收；只读模式仅运行审查者'}

    def build(self, value, task_id):
        execution = value.get('executionMode', 'mock'); authority = None
        if execution == 'mock': project = self.project(value['projectId'])
        else: project, authority = self.cli_project(value['projectId'], execution)
        ids = [p.id for p in project.participants]
        flow = ids
        template = value['templateId']
        if template == 'flow': flow = ids[:2]
        if template == 'review': flow = ids[1:2]
        spec = {'task_id': task_id, 'title': value['title'], 'goal': value['goal'],
                'flow': flow, 'mock_plan': TEMPLATES[template][1] if execution == 'mock' else [], 'runs_dir': str(self.runs),
                'allow_real_codex': execution != 'mock', 'allow_real_cursor': execution != 'mock', 'baseline_verify': False}
        if authority:
            if execution == 'real':
                spec['codex_bin'] = authority.payload['cli']['codex'][0]
                spec['cursor_bin'] = authority.payload['cli']['cursor'][0]
            else:
                spec['codex_bin'] = 'offline-codex'
                spec['cursor_bin'] = 'offline-cursor'
        ceilings = {'modelCalls': ('max_model_calls', project.max_model_calls), 'rounds': ('max_rounds', project.max_rounds),
                    'repairs': ('max_repair_rounds', project.max_repair_rounds), 'runSeconds': ('max_run_seconds', project.max_run_seconds),
                    'stepSeconds': ('timeout_seconds', project.timeout_seconds)}
        for field, number in value.get('budget', {}).items():
            key, ceiling = ceilings[field]
            if number > ceiling: raise BusinessError('BUDGET_DENIED', f'{field} 不能超过项目预设 {ceiling}')
            spec[key] = number
        try: config = build_project_task(project.project_id, spec, self.projects)
        except ConfigError as exc: raise BusinessError('CONFIG_INVALID', str(exc)) from exc
        if template == 'review': config.write_paths = []
        config.execution_mode = execution
        config.execution_authority = authority
        if authority:
            try: authority.authorize_config(config)
            except ExecutionDenied as exc: raise BusinessError(exc.code, str(exc)) from exc
        elif config.allow_real_codex or config.allow_real_cursor or any(p.executor != 'mock' for p in config.participants):
            raise BusinessError('MOCK_REQUIRED', '最终 TaskConfig 必须强制 mock')
        return config

    def preflight(self, raw):
        from .preflight import inspect
        return inspect(self, raw)

    def validate(self, raw):
        value = task_request(raw); self.build(value, 'studio-validation')
        return {'valid': True, 'mode': value.get('executionMode','mock'), 'projectId': value['projectId'],
                'templateId': value['templateId'], 'defaults': self.defaults(json.dumps({'projectId': value['projectId'], 'executionMode':value.get('executionMode','mock')}))}
