"""Backend execution capabilities. No environment/config switch can grant access.

Grants use RSA PKCS#1 v1.5 / SHA-256 signatures from deployment-pinned public
keys. This release deliberately has NO production signing authority. Private
keys must remain with the human operator, outside the application and Agents.
Tests inject an ephemeral, offline-only public key in their own processes.
Worktrees and write_paths are cooperative safeguards, not OS confinement.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import math
import sys
import time
from pathlib import Path
from types import MappingProxyType

from dual_agent.safety import SafetyError, finalize_declared, is_reparse_point

# An audited deployment update is required to install an operator public key.
# There is intentionally no API, policy field, environment variable or CLI
# option to add keys, sign grants, or approve trusted test commands.
TRUSTED_SIGNERS = MappingProxyType({})


class ExecutionDenied(SafetyError):
    code = 'REAL_DENIED'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def exact_path(raw):
    path = Path(raw)
    if not path.is_absolute() or is_reparse_point(path):
        raise ExecutionDenied('授权路径必须为绝对路径，不能使用重解析点')
    resolved = finalize_declared(str(path))
    if str(path).casefold() != str(resolved).casefold():
        raise ExecutionDenied('授权路径经过重解析或含有非规范路径')
    return resolved


def verify_signature(payload, signature, key):
    """Exact EMSA-PKCS1-v1_5 encoding; no permissive ASN.1 parsing."""
    n, e = int(key['n'], 16), int(key['e'])
    size = (n.bit_length() + 7) // 8
    if n.bit_length() < 2048 or e != 65537 or len(signature) != size:
        return False
    number = int.from_bytes(signature, 'big')
    if number >= n:
        return False
    tail = bytes.fromhex('3031300d060960864801650304020105000420') + hashlib.sha256(canonical(payload)).digest()
    expected = b'\x00\x01' + b'\xff' * (size - len(tail) - 3) + b'\x00' + tail
    return hmac.compare_digest(pow(number, e, n).to_bytes(size, 'big'), expected)


class ExecutionAuthority:
    def __init__(self, projects, runs, project_id, mode):
        self.projects = exact_path(str(projects)); self.runs = exact_path(str(runs))
        self.project_id = project_id; self.mode = mode
        self.file = self.runs / 'studio-authorizations' / (project_id + '.json')
        self.payload = self.reload()
        self.identity = hashlib.sha256(canonical(self.payload)).hexdigest()

    def reload(self):
        try:
            if self.mode not in {'real', 'offline_cli'} or not TRUSTED_SIGNERS:
                raise ExecutionDenied('真实 CLI 未授权：部署未安装人工授权公钥；默认仅 Mock')
            exact_path(str(self.file))
            if self.file.stat().st_size > 65536:
                raise ExecutionDenied('授权文件超过大小限制')
            def unique(items):
                result = {}
                for key, value in items:
                    if key in result: raise ExecutionDenied('授权不允许重复字段')
                    result[key] = value
                return result
            envelope = json.loads(self.file.read_text(encoding='utf8'), object_pairs_hook=unique)
            if set(envelope) != {'payload', 'signature'}: raise ExecutionDenied('授权封装无效')
            payload = envelope['payload']; key = TRUSTED_SIGNERS.get(payload['signer'])
            if not key or self.mode not in key['modes'] or not verify_signature(payload, base64.b64decode(envelope['signature'], validate=True), key):
                raise ExecutionDenied('授权签名无效或签名者无此执行权限')
            expires = payload['expires']
            if type(expires) not in {int, float} or not math.isfinite(expires) or expires <= time.time():
                raise ExecutionDenied('授权已过期')
            if payload['version'] != 1 or payload['mode'] != self.mode or payload['projectId'] != self.project_id:
                raise ExecutionDenied('授权用途或项目不匹配')
            if exact_path(payload['projects']) != self.projects or exact_path(payload['runs']) != self.runs:
                raise ExecutionDenied('授权不属于本部署')
            source = self.projects / (self.project_id + '.json')
            if digest(exact_path(str(source))) != payload['projectSha256']:
                raise ExecutionDenied('项目配置已改变，授权失效')
            for raw, expected in payload['files'].items():
                path = exact_path(raw)
                if digest(path) != expected: raise ExecutionDenied('授权程序或可信脚本摘要已改变')
                workspace = exact_path(payload['workspace'])
                tree_root = exact_path(payload['worktreeRoot'])
                if path.is_relative_to(workspace) or path.is_relative_to(tree_root):
                    raise ExecutionDenied('授权程序和可信脚本不能处于 Agent 工作区')
            if hasattr(self, 'identity') and hashlib.sha256(canonical(payload)).hexdigest() != self.identity:
                raise ExecutionDenied('排队或执行中的授权已替换；不得扩大权限')
            return payload
        except ExecutionDenied: raise
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ExecutionDenied('缺少有效的独立签名授权，真实执行被拒绝') from exc

    def authorize_config(self, config, prepared=False):
        payload = self.reload()
        expected = [('codex', 'developer'), ('cursor', 'reviewer'), ('cursor', 'acceptor')]
        if [(p.executor, p.role) for p in config.participants] != expected or config.flow != [p.id for p in config.participants]:
            raise ExecutionDenied('只允许 Codex 开发 → Cursor 审查 → Cursor 验收的固定流程')
        if config.execution_mode != self.mode or Path(config.runs_dir).resolve() != self.runs:
            raise ExecutionDenied('执行模式或运行目录不匹配')
        root = exact_path(payload['worktreeRoot']); repo = exact_path(payload['workspace'])
        if not repo.is_relative_to(self.runs / 'studio-sources'):
            raise ExecutionDenied('CLI 源仓库必须是本部署 runs/studio-sources 内明确批准的专属副本')
        if not root.is_relative_to(self.runs / 'studio-worktrees') or root.is_relative_to(repo) or repo.is_relative_to(root):
            raise ExecutionDenied('Worktree 必须位于该部署专属 runs/studio-worktrees 根')
        if not prepared:
            if exact_path(config.workspace) != repo or not config.worktree.enabled or exact_path(config.worktree.root) != root:
                raise ExecutionDenied('真实开发必须在已授权的独立 Worktree 中执行')
        else:
            path = exact_path(config.workspace)
            if path == root or not path.is_relative_to(root) or config.worktree.enabled:
                raise ExecutionDenied('Worktree 创建后执行范围校验失败')
        if config.worktree.path and not exact_path(config.worktree.path).is_relative_to(root):
            raise ExecutionDenied('Worktree 目标越过授权范围')
        if config.write_paths != [config.workspace] or config.share_context or config.cycle:
            raise ExecutionDenied('写入范围必须为专属 Worktree；禁止共享上下文或任意循环')
        for name in ('max_model_calls', 'max_rounds', 'max_repair_rounds', 'max_run_seconds', 'timeout_seconds'):
            number, ceiling = getattr(config,name), payload['budget'][name]
            if type(number) not in {int,float} or type(ceiling) not in {int,float} or not math.isfinite(number) or not math.isfinite(ceiling) or number < (0 if name=='max_repair_rounds' else .01) or number > ceiling:
                raise ExecutionDenied('预算无效或超过签名授权')
        if not any(v.type == 'command' for v in config.verification) or not any(v.type == 'allowed_changes' for v in config.verification):
            raise ExecutionDenied('真实任务必须保留可信命令及改动范围检查')
        if not config.cycle and config.max_rounds < len(config.flow):
            raise ExecutionDenied('预算轮数不足以完成固定的开发、审查和验收流程')
        if self.mode == 'real':
            self.require_real_cli(payload)
        # Config hash pins the whole verification preset. Executables/scripts
        # additionally require file hashes; no model may introduce commands.
        for check in config.verification:
            if check.type != 'command': continue
            if not check.argv or str(exact_path(check.argv[0])) not in payload['files'] or any(part.lower() in {'-command','/c','-c','--command','-encodedcommand'} for part in check.argv):
                raise ExecutionDenied('可信命令程序未签名或含任意 Shell 参数')
            for token in check.argv:
                if (Path(token).is_file() or token.lower().endswith(('.py','.ps1','.exe','.cmd','.bat','.js'))) and str(exact_path(token)) not in payload['files']:
                    raise ExecutionDenied('可信验证程序未由独立签名批准')
        return payload

    def command(self, request, command):
        payload = self.reload()
        if request.execution_mode != self.mode: raise ExecutionDenied('CLI 请求来源与授权不一致')
        workspace = exact_path(str(request.workspace)); root = exact_path(payload['worktreeRoot'])
        if workspace == root or not workspace.is_relative_to(root): raise ExecutionDenied('CLI 工作区不在授权 Worktree')
        if request.executor == 'codex' and request.role != 'developer': raise ExecutionDenied('Codex 职责越权')
        if request.executor == 'cursor' and request.role not in {'reviewer', 'acceptor'}: raise ExecutionDenied('Cursor 职责越权')
        if self.mode == 'offline_cli':
            # Offline grants can ONLY redirect into this deployed, hash-pinned
            # simulator. They can never authorize an installed Agent executable.
            from dual_agent import PROJECT_ROOT
            script = PROJECT_ROOT / 'desktop-validation/tests/fake_cli_v05.py'
            if str(script) not in payload['files'] or digest(script) != payload['files'][str(script)]:
                raise ExecutionDenied('离线替身未固定或不在部署内')
            return [sys.executable, str(script), request.executor, *command[1:]], '离线模拟 CLI · ' + request.executor
        prefix = payload['cli'][request.executor]
        if not prefix or Path(prefix[0]).suffix.lower() != '.exe':
            raise ExecutionDenied('真实 CLI 必须使用明确批准的原生 exe，不执行 Shell 启动器')
        if command[:len(prefix)] != list(prefix) or any(not self.file_covers(part) for part in prefix):
            raise ExecutionDenied('CLI 启动命令不在签名白名单')
        return command, '真实 ' + request.executor.capitalize() + ' CLI'

    def file_covers(self, raw):
        target = exact_path(raw)
        for key in self.payload.get('files') or {}:
            if exact_path(key) == target:
                return True
        return False

    def require_real_cli(self, payload):
        cli = payload.get('cli')
        if not isinstance(cli, dict):
            raise ExecutionDenied('真实授权缺少 CLI 启动前缀')
        for name in ('codex', 'cursor'):
            prefix = cli.get(name)
            if not isinstance(prefix, list) or not prefix or not all(isinstance(part, str) and part for part in prefix):
                raise ExecutionDenied('真实 CLI 启动前缀无效：' + name)
            if Path(prefix[0]).suffix.lower() != '.exe':
                raise ExecutionDenied('真实 CLI 必须使用明确批准的原生 exe，不执行 Shell 启动器')
            for part in prefix:
                if Path(part).suffix.lower() in {'.cmd', '.bat', '.ps1'} or part.lower() in {'-command', '/c', '-c', '--command', '-encodedcommand'}:
                    raise ExecutionDenied('CLI 启动前缀含有 Shell 启动器或任意命令参数')
                if not self.file_covers(part):
                    raise ExecutionDenied('CLI 启动程序未纳入签名摘要：' + name)


def require_authority(config, prepared=False):
    if config.execution_mode == 'mock' and all(p.executor == 'mock' for p in config.participants): return
    if not isinstance(config.execution_authority, ExecutionAuthority):
        raise ExecutionDenied('Python 执行授权缺失；配置和环境变量不能批准真实调用')
    config.execution_authority.authorize_config(config, prepared)
