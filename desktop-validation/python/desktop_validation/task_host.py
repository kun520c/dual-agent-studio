"""Detached gated host. Authenticated local JSON pipe; never pickle RPC."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Client, Listener
from dual_agent.models import dump_json
from dual_agent.scheduler import TaskScheduler, cancel_schedule
from dual_agent.status import read_status, write_status, is_terminal
from dual_agent.safety import finalize_declared
from .task_api import ACTIVE, BusinessError, TaskPolicy, dto, identifier, task_request

HERE = Path(__file__).resolve().parents[2]
ROOT = HERE.parent

def save_request(path, value):
    temporary=path.with_suffix('.pending')
    with temporary.open('w',encoding='utf8') as stream:
        json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)

def address(runs):
    digest = hashlib.sha256(str(Path(runs).resolve()).casefold().encode()).hexdigest()[:24]
    return r'\\.\pipe\dual-agent-studio-' + digest

def token_file(runs): return Path(runs) / 'studio-host' / 'auth.token'

def host_unavailable(log: Path) -> str:
    lines = []
    try:
        lines = [line.strip() for line in log.read_bytes()[-4000:].decode('utf-8', errors='replace').splitlines() if line.strip()][-4:]
    except OSError:
        lines = []
    message = '无法连接独立模拟宿主。任务没有启动；宿主恢复后必须使用原来的请求 ID，不能另起一条任务。'
    if lines:
        message += ' 诊断：' + ' | '.join(lines)
    elif (ROOT / 'deployment.json').is_file():
        message += ' 没有宿主日志。请在部署目录运行诊断启动.cmd。'
    else:
        message += ' 没有宿主日志。请查看 runs/studio-host/host.log。'
    return message[:500]


def singleton_file(runs):
    # A locked byte must not live inside a coordinator workspace snapshot.
    # This is application runtime state, outside every task-writable root.
    # Portable builds keep it in the deployment state directory. Development
    # and the existing isolation tests keep the LOCALAPPDATA location.
    if (ROOT / 'deployment.json').is_file():
        root = ROOT / 'state' / 'host-locks'
    else:
        root=Path(os.environ['LOCALAPPDATA'])/'DualAgentStudio'/'mock-host-locks'
    root.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(str(Path(runs).resolve()).casefold().encode()).hexdigest()
    return root/(digest+'.lock')

def token(runs):
    file = token_file(runs); file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file.open('xb') as stream: stream.write(os.urandom(32))
    except FileExistsError: pass
    value = file.read_bytes()
    if len(value) != 32: raise BusinessError('HOST_AUTH', '宿主认证文件无效')
    return value

class TaskHost:
    def __init__(self, policy):
        self.policy = policy; self.ledger = policy.runs / 'studio-host' / 'requests'
        self.ledger.mkdir(parents=True, exist_ok=True); self.guard = threading.RLock()
        self.recover()
        self.scheduler = TaskScheduler(policy.runs)
        from dual_agent.goal_host.service import GoalHost
        self.goal_host = GoalHost(self)
        self.goals = self.goal_host.goals

    def schedules(self, raw):
        value = dto(raw, {'projectId', 'offset', 'limit', 'scheduleId'}, set())
        selected=value.get('scheduleId','')
        if selected:identifier(selected,'scheduleId')
        project = value.get('projectId', '')
        if project: identifier(project, 'projectId')
        offset = value.get('offset', 0); limit = value.get('limit', 100)
        if type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or not 1 <= limit <= 100:
            raise BusinessError('DTO_TYPE', '分页必须为整数，limit 为 1–100')
        rows = []
        for file in (self.policy.runs / 'schedule').glob('sch-studio-*.json'):
            data = read_status(file)
            if data and (not project or data.get('project_id') == project):
                rows.append({key: data.get(key, '') for key in ('schedule_id', 'project_id', 'task_id', 'title', 'state', 'detail', 'stop_reason', 'success', 'queued_at', 'started_at', 'finished_at', 'updated_at', 'mode')})
        for row in rows:
            row['cancellationRequested']=row['state'] in ACTIVE and (self.policy.runs/'schedule'/(row['schedule_id']+'.cancel')).is_file()
        rows.sort(key=lambda item: (item['state'] in ACTIVE,item['queued_at']), reverse=True)
        page=rows[offset:offset+limit]
        pinned=next((row for row in rows if row['schedule_id']==selected),None)
        if pinned and pinned not in page:page.append(pinned)
        return {'items': page, 'total': len(rows), 'maxConcurrent': 2, 'hostPid': os.getpid()}

    def submit(self, raw, *, goal_reservation=None, execution_context=None):
        value = task_request(raw, submit=True)
        request_id = value.pop('requestId')
        key = hashlib.sha256(request_id.encode()).hexdigest()
        payload_hash = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        schedule_id = 'sch-studio-' + key[:24]; task_id = 'studio-' + key[:24]
        with self.guard:
            from dual_agent.goal_host.identity import reservation_path, prepare_submission
            if goal_reservation is None and (execution_context is not None or reservation_path(self, request_id).exists()):
                raise BusinessError('GOAL_RESERVED', '请求 ID 属于受信 Goal 派工，不能从普通接口执行')
            if goal_reservation is not None and goal_reservation.get('request_id') != request_id:
                raise BusinessError('GOAL_IDENTITY', 'Goal reservation request mismatch')
            file = self.ledger / (key + '.json')
            if file.exists():
                saved = json.loads(file.read_text(encoding='utf8'))
                if saved['payloadHash'] != payload_hash:
                    raise BusinessError('IDEMPOTENCY_CONFLICT', '相同请求 ID 不能提交不同内容')
                if goal_reservation is not None and saved.get('goalRunId') != goal_reservation['run_id']:
                    raise BusinessError('GOAL_IDENTITY', 'Saved execution has a different run identity')
                return self.submission(saved, True)
            config = self.policy.build(value, task_id)
            if goal_reservation is not None:
                config = prepare_submission(self, value, config, goal_reservation, execution_context)
            if config.execution_authority:
                used = sum(json.loads(p.read_text(encoding='utf8')).get('authorization') == config.execution_authority.identity for p in self.ledger.glob('*.json'))
                if used >= config.execution_authority.payload.get('maxTasks',1):
                    raise BusinessError('AUTHORIZATION_BUDGET', '已达到该签名授权的任务次数上限')
            if sum(not is_terminal(item.state) for item in self.scheduler.tasks) >= 100:
                raise BusinessError('QUEUE_FULL', '调度队列已满')
            saved = {'requestId': request_id, 'payloadHash': payload_hash, 'scheduleId': schedule_id,
                     'taskId': task_id, 'projectId': config.project_id, 'mode':config.execution_mode}
            if config.execution_authority: saved['authorization'] = config.execution_authority.identity
            if goal_reservation is not None:
                saved['goalRunId'] = goal_reservation['run_id']
                saved['goalConfigHash'] = goal_reservation['config_hash']
            # Reserve before scheduling: a lost response or host crash cannot replay.
            save_request(file, saved)
            try:
                project = self.policy.project(config.project_id) if config.execution_mode == 'mock' else self.policy.cli_project(config.project_id,config.execution_mode)[0]
                if execution_context is None:
                    self.scheduler.submit(config, schedule_id, project.name)
                else:
                    self.scheduler.submit(config, schedule_id, project.name, execution_context=execution_context)
            except (ValueError,OSError) as exc:
                write_status(self.policy.runs/'schedule'/(schedule_id+'.json'),state='failed',schedule_id=schedule_id,
                    task_id=task_id,project_id=config.project_id,title=config.title,mode=config.execution_mode,stop_reason='submission_error',success=False,detail='提交保留后调度失败；不得重放：'+str(exc)[:200])
                raise BusinessError('SUBMISSION_FAILED','调度失败，已保留原请求；重试不会重复执行') from exc
            return self.submission(saved, False)

    def submission(self, saved, duplicate):
        status = read_status(self.policy.runs / 'schedule' / (saved['scheduleId'] + '.json'))
        return {**{key: saved[key] for key in ('requestId', 'scheduleId', 'taskId', 'projectId')},
                'state': status.get('state', 'failed'), 'stopReason': status.get('stop_reason', 'submission_interrupted' if not status else ''),
                'duplicate': duplicate, 'mode': saved.get('mode','mock')}

    def cancel(self, raw):
        value = dto(raw, {'scheduleId'}, {'scheduleId'})
        schedule_id = identifier(value['scheduleId'], 'scheduleId')
        if not schedule_id.startswith('sch-studio-'):
            raise BusinessError('SCHEDULE_DENIED', '只能取消本阶段宿主创建的任务')
        path = self.policy.runs / 'schedule' / (schedule_id + '.json')
        status = read_status(path)
        if not status or status.get('schedule_id') != schedule_id:
            raise BusinessError('SCHEDULE_UNKNOWN', '没有对应的调度记录')
        # Pin the cancellation target. Never trust a saved arbitrary cancel_file.
        expected = self.policy.runs / 'schedule' / (schedule_id + '.cancel')
        if status.get('cancel_file') != str(expected) or path.is_symlink() or expected.is_symlink() or not finalize_declared(str(path)).is_relative_to(self.policy.runs) or not finalize_declared(str(expected)).is_relative_to(self.policy.runs):
            raise BusinessError('PATH_DENIED', '取消路径校验失败')
        message = cancel_schedule(schedule_id, self.policy.runs)
        latest = read_status(path)
        return {'scheduleId': schedule_id, 'state': latest['state'], 'detail': message, 'requested': not is_terminal(status['state'])}

    def _recover_result(self, saved, status):
        task_root = self.policy.runs / saved['taskId']
        pinned = status.get('run_dir') if status else ''
        if pinned:
            try:
                folder = finalize_declared(str(pinned))
            except (OSError, ValueError):
                return {}, None
            if folder == task_root or not folder.is_relative_to(task_root):
                return {}, None
            result_path = folder / 'result.json'
            return read_status(result_path), result_path
        if status:
            # An active phase without a pinned run must not adopt an older result.
            return {}, None
        if not task_root.is_dir():
            return {}, None
        results = sorted(task_root.glob('*/result.json'))
        if not results:
            return {}, None
        return read_status(results[-1]), results[-1]

    def recover(self):
        # Only called after acquiring the singleton OS lock; no live host exists.
        for file in self.ledger.glob('*.json'):
            saved = json.loads(file.read_text(encoding='utf8'))
            path = self.policy.runs / 'schedule' / (saved['scheduleId'] + '.json')
            status = read_status(path)
            if status and status.get('state') not in ACTIVE: continue
            if status.get('state') == 'queued':
                write_status(path, state='failed', schedule_id=saved['scheduleId'], project_id=saved['projectId'], task_id=saved['taskId'],
                             mode=saved.get('mode','mock'), stop_reason='process_interrupted', success=False,
                             detail='宿主退出时任务仍在排队或等待重试。不采用更早的运行结果，也不会自动重跑。')
                continue
            result, result_path = self._recover_result(saved, status)
            if result_path and result.get('task_id') == saved['taskId'] and result.get('status') in {'verified', 'failed', 'cancelled'}:
                checks=[v for v in result.get('verification',[]) if isinstance(v,dict) and v.get('type')=='command' and not v.get('superseded') and not v.get('evidence_invalidated')]
                if result.get('status') == 'verified' and (result.get('stop_reason') != 'verified' or result.get('success') is not True or not checks or not all(v.get('ok') is True and v.get('exit_code') == v.get('expect_exit_code',0) for v in checks)):
                    result.update(status='failed',success=False,stop_reason='unverified')
                write_status(path, state=result['status'], schedule_id=saved['scheduleId'], task_id=saved['taskId'], project_id=saved['projectId'],
                             mode=saved.get('mode','mock'), title=result.get('title',''), run_dir=str(result_path.parent), report_path=str(result_path.parent/'报告.md'),
                             cancel_file=str(self.policy.runs/'schedule'/(saved['scheduleId']+'.cancel')),
                             stop_reason=result.get('stop_reason'), success=result.get('success'), detail='从已保存协调器结果恢复', finished_at=result.get('finished_at'))
            else:
                write_status(path, state='failed', schedule_id=saved['scheduleId'], project_id=saved['projectId'], task_id=saved['taskId'],
                             mode=saved.get('mode','mock'), stop_reason='process_interrupted', success=False, detail='Python 宿主或 Windows 异常退出；没有完整验收证据。不会自动重跑。')

    def dispatch(self, method, raw):
        if method == 'getTaskDefaults': return self.policy.defaults(raw)
        if method == 'validateTask': return self.policy.validate(raw)
        if method == 'preflightTask': return self.policy.preflight(raw)
        if method == 'submitTask': return self.submit(raw)
        if method == 'listSchedules': return self.schedules(raw)
        if method == 'cancelTask': return self.cancel(raw)
        raise BusinessError('METHOD_DENIED', '未知业务方法')

class HostClient:
    def __init__(self, projects, runs, policy):
        self.policy = TaskPolicy(projects, runs, policy)
        self.lock = threading.Lock()

    def call(self, method, raw):
        # Validate in the Python bridge before starting or contacting a process.
        if method == 'getTaskDefaults': return self.policy.defaults(raw)
        if method == 'validateTask': return self.policy.validate(raw)
        if method == 'preflightTask': return self.policy.preflight(raw)
        if method == 'submitTask': self.policy.build(task_request(raw, True), 'studio-validation')
        if method == 'listSchedules':
            value = dto(raw, {'projectId', 'offset', 'limit', 'scheduleId'}, set())
            if value.get('scheduleId'):identifier(value['scheduleId'],'scheduleId')
            if value.get('projectId'): identifier(value['projectId'], 'projectId')
            for field, default, low, high in [('offset',0,0,100000),('limit',100,1,100)]:
                item = value.get(field, default)
                if type(item) is not int or not low <= item <= high: raise BusinessError('DTO_TYPE', '分页参数无效')
        if method == 'cancelTask':
            value = dto(raw, {'scheduleId'}, {'scheduleId'}); identifier(value['scheduleId'], 'scheduleId')
            if not value['scheduleId'].startswith('sch-studio-'): raise BusinessError('SCHEDULE_DENIED', '非本阶段任务')
        if method not in {'submitTask', 'listSchedules', 'cancelTask'}: raise BusinessError('METHOD_DENIED', '未知方法')
        with self.lock:
            conn = self.connect()
            try:
                conn.send_bytes(json.dumps({'method': method, 'request': raw}, ensure_ascii=False).encode())
                if not conn.poll(8): raise BusinessError('HOST_TIMEOUT', '宿主响应超时；重试必须保留相同请求 ID')
                response = json.loads(conn.recv_bytes(2*1024*1024))
                if not response['ok']: raise BusinessError(response.get('code','HOST_ERROR'), response['error'])
                return response['data']
            finally: conn.close()

    def connect(self):
        auth = token(self.policy.runs)
        try: return Client(address(self.policy.runs), family='AF_PIPE', authkey=auth)
        except (OSError, EOFError): pass
        log = self.policy.runs / 'studio-host' / 'host.log'
        env = dict(os.environ); env['PYTHONPATH'] = str(HERE/'python') + os.pathsep + str(ROOT)
        env['PYTHONUTF8'] = '1'; env['PYTHONDONTWRITEBYTECODE'] = '1'
        with log.open('ab') as stream:
            subprocess.Popen([sys.executable, '-m', 'desktop_validation.task_host', '--projects', str(self.policy.projects), '--runs', str(self.policy.runs), '--policy', str(self.policy.policy)],
                cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                creationflags=0x08000000 | 0x00000200, close_fds=True)
        deadline = time.monotonic() + (20 if (ROOT / 'deployment.json').is_file() else 8)
        while time.monotonic() < deadline:
            try: return Client(address(self.policy.runs), family='AF_PIPE', authkey=auth)
            except (OSError, EOFError): time.sleep(0.05)
        raise BusinessError('HOST_UNAVAILABLE', host_unavailable(log))

def main():
    parser = argparse.ArgumentParser()
    for name in ('projects','runs','policy'): parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    # Windows releases this lock on crash; a second GUI cannot start a second host.
    import msvcrt
    auth = token(args.runs)
    lock = singleton_file(args.runs).open('a+b')
    lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
    try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError: return 0
    host = TaskHost(TaskPolicy(args.projects, args.runs, args.policy))
    listener = Listener(address(args.runs), family='AF_PIPE', authkey=auth)
    dump_json(args.runs/'studio-host'/'host.json', {'pid': os.getpid(), 'mode':'gated', 'defaultMode':'mock'})
    while True:
        try:
            with listener.accept() as conn:
                if not conn.poll(5): continue
                try:
                    packet = dto(conn.recv_bytes(16384).decode(), {'method','request'}, {'method','request'})
                    data = host.dispatch(packet['method'], packet['request'])
                    response = {'ok':True, 'data':data}
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    response = {'ok':False, 'error':str(exc)[:300], 'code':getattr(exc,'code','HOST_ERROR')}
                conn.send_bytes(json.dumps(response, ensure_ascii=False).encode())
        except (EOFError, OSError): continue

if __name__ == '__main__': raise SystemExit(main())
