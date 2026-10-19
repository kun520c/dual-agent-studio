"""Bounded, allowlisted read-only view of coordinator artifacts.

Never follows a browser supplied path. Run and diff identifiers are opaque.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import threading
from collections import OrderedDict
from typing import Any
from .index import JsonlIndex,signature
from .files import opened_under_root

from dual_agent.gui.adapter import ConsoleAdapter
from dual_agent.gui.outcome import presentation
from dual_agent.safety import redact_text
from dual_agent.status import status_from_result_dict

MAX_FILE_BYTES=8*1024*1024
MAX_ITEMS=500
MAX_PAGE_BYTES=512*1024
ARCHIVE='__archive__'
ROLE_NAMES={'developer':'开发者','reviewer':'审查者','diagnoser':'诊断者','acceptor':'验收者'}

def outcome(result:dict,task:dict)->dict:
    if task.get('execution_mode') in {'real','offline_cli'} and result.get('stop_reason') == 'verified':
        commands=[v for v in result.get('verification',[]) if isinstance(v,dict) and v.get('type')=='command' and not v.get('superseded') and not v.get('evidence_invalidated')]
        if not commands or not all(v.get('ok') is True and v.get('exit_code') == v.get('expect_exit_code',0) for v in commands):
            result={**result,'status':'failed','stop_reason':'unverified','success':False}
    state=status_from_result_dict(result)
    reason=str(result.get('stop_reason') or '')
    value=presentation(state,reason,success=result.get('success'),task=task)
    tone='success' if value['passed'] else 'neutral' if value['kind'] in {'flow_complete','cancelled'} else 'active' if state in {'queued','running','verifying','reviewing','repairing'} else 'danger'
    if reason=='budget':value.update(kind='budget',label='预算耗尽，未完成验收');tone='warning'
    if value['passed'] and task.get('participants') and all(p.get('executor')=='mock' for p in task['participants']):
        value['label']='模拟执行 · 可信验收通过'
    if task.get('execution_mode') == 'offline_cli': value['label']='离线模拟 CLI · ' + value['label']
    return {**value,'reason':reason,'tone':tone}

def text(value:Any,limit=100000)->str:
    return redact_text(str(value or ''))[:limit]

def sanitized(value):
    if isinstance(value,str):return redact_text(value)
    if isinstance(value,list):return [sanitized(item) for item in value]
    if isinstance(value,dict):return {redact_text(key):sanitized(item) for key,item in value.items()}
    return value

def page_args(offset:int,limit:int)->None:
    if type(offset) is not int or type(limit) is not int or offset<0 or offset>1000000 or not 1<=limit<=MAX_ITEMS:
        raise ValueError('分页参数超出范围')

class HistoryStore:
    def __init__(self,projects_dir:Path,runs_dir:Path):
        self.projects_dir=projects_dir.resolve()
        self.runs_dir=runs_dir.resolve()
        self.adapter=ConsoleAdapter(root=self.projects_dir,runs_dir=self.runs_dir)
        self._runs:dict[str,Path]={}
        self._projects:dict[str,dict]={}
        self._lock=threading.RLock()
        self._diff_cache=OrderedDict()
        self._json_cache=OrderedDict()
        self._usage_cache=OrderedDict()
        self.index=JsonlIndex(self.runs_dir)
        self.refresh()

    def _safe(self,path:Path)->Path:
        resolved=path.resolve()
        if not resolved.is_relative_to(self.runs_dir):raise ValueError('运行记录越过只读根目录')
        if any(p.is_symlink() for p in (path,*path.parents) if p!=self.runs_dir and p.is_relative_to(self.runs_dir)):raise ValueError('不接受运行记录符号链接')
        return resolved

    def _read(self,path:Path,redact=True)->str:
        safe=self._safe(path)
        if not safe.is_file():return ''
        if safe.stat().st_size>MAX_FILE_BYTES:raise ValueError('历史文件超过 8 MiB 读取上限')
        before=signature(safe.stat())
        with safe.open('rb') as stream:
            opened_under_root(stream,self.runs_dir)
            opened=signature(__import__('os').fstat(stream.fileno()))
            if opened[:4]!=before[:4]:raise ValueError('历史文件正在替换，请重试')
            raw=stream.read(MAX_FILE_BYTES+1)
            if signature(__import__('os').fstat(stream.fileno()))[:4]!=opened[:4]:raise ValueError('历史文件读取期间发生变化，请重试')
        if len(raw)>MAX_FILE_BYTES:raise ValueError('历史文件超过 8 MiB 读取上限')
        if signature(safe.stat())!=before:raise ValueError('历史文件读取期间发生变化，请重试')
        value=raw.decode('utf-8-sig',errors='replace')
        return redact_text(value) if redact else value

    def _json(self,path:Path,default:Any=None)->Any:
        safe=self._safe(path)
        stamp=signature(safe.stat()) if safe.is_file() else None
        cached=self._json_cache.get(safe)
        if cached and cached[0]==stamp:
            self._json_cache.move_to_end(safe);return cached[1]
        raw=self._read(path,redact=False)
        if not raw:return {} if default is None else default
        try:value=sanitized(json.loads(raw))
        except (json.JSONDecodeError,RecursionError):raise ValueError('历史 JSON 损坏：'+safe.name)
        cost=len(raw.encode('utf8'))
        self._json_cache[safe]=(stamp,value,cost)
        while len(self._json_cache)>32 or sum(v[2] for v in self._json_cache.values())>8*1024*1024:self._json_cache.popitem(last=False)
        return value

    def refresh(self)->None:
        with self._lock:
            for path in self.projects_dir.glob('*.json'):
                if path.is_symlink() or not path.resolve().is_relative_to(self.projects_dir):raise ValueError('项目登记文件越过登记目录')
            from dual_agent.projects import show_project
            self._projects={}
            for path in sorted(self.projects_dir.glob('*.json')):
                try:
                    if path.stat().st_size>256*1024:raise ValueError('项目配置超过 256 KiB 上限')
                    p=show_project(path.stem,self.projects_dir)
                    self._projects[path.stem]={'id':path.stem,'name':text(p.name,200),'workspace':text(p.workspace,1000),'enabled':p.enabled,'registered':True}
                except ValueError as error:
                    self._projects[path.stem]={'id':path.stem,'name':path.stem,'workspace':'配置错误','enabled':False,'registered':True,'configError':text(error,300)}
            candidates=[]
            if self.runs_dir.is_dir():
                for task_root in self.runs_dir.iterdir():
                    if not task_root.is_dir() or task_root.name in {'schedule','locks','batches','gui-batches','readiness'}:continue
                    self._safe(task_root)
                    for run in task_root.iterdir():
                        if run.is_dir() and self._safe(run/'task.json').is_file():candidates.append(self._safe(run))
            self._runs={hashlib.sha256(str(p.relative_to(self.runs_dir)).encode('utf8')).hexdigest()[:24]:p for p in candidates}

    def list_projects(self)->list[dict]:
        self.refresh()
        rows=list(self._projects.values())
        if any(self._json(p/'task.json').get('project_id') not in self._projects for p in self._runs.values()):
            rows.append({'id':ARCHIVE,'name':'其他历史运行','workspace':'未关联已登记项目的历史记录','enabled':False,'registered':False})
        return rows

    def _run(self,identifier:str)->Path:
        if not isinstance(identifier,str) or identifier not in self._runs:raise ValueError('未知历史任务 ID')
        return self._safe(self._runs[identifier])

    def find_task(self,task_id:str)->dict:
        if not isinstance(task_id,str) or not re.fullmatch(r'[\w-]{1,200}',task_id):raise ValueError('无效任务标识符')
        self.refresh()
        matches=[self._task(id) for id,path in self._runs.items() if self._json(path/'task.json').get('task_id')==task_id]
        matches.sort(key=lambda row:row['finished'] or row['started'] or row['id'],reverse=True)
        return {'task':matches[0] if matches else None}

    def _task(self,identifier:str)->dict:
        run=self._run(identifier); task=self._json(run/'task.json');result=self._json(run/'result.json')
        if not isinstance(task,dict) or not isinstance(result,dict):raise ValueError('任务 / 结果格式不正确')
        if result.get('task_id') and result.get('task_id')!=task.get('task_id'):raise ValueError('result.json 与 task.json 的任务 ID 不一致')
        if not result and task.get('schedule_id') and task.get('status_file'):
            status_path=Path(task['status_file'])
            expected=self.runs_dir/'schedule'/(str(task['schedule_id'])+'.json')
            if status_path!=expected:raise ValueError('任务调度状态路径不匹配')
            status=self._json(expected)
            if status.get('task_id')!=task.get('task_id'):raise ValueError('调度状态与任务 ID 不一致')
            labels={'mock':'模拟','real':'真实 CLI','offline_cli':'离线模拟 CLI'}
            result={'status':status.get('state'),'stop_reason':status.get('stop_reason'),'success':False,'started_at':status.get('started_at'),'finished_at':status.get('finished_at'),'mode_label':labels.get(status.get('mode'),'')}
        project_id=task.get('project_id');project_id=project_id if project_id in self._projects else ARCHIVE
        mock=all(p.get('executor')=='mock' for p in task.get('participants',[])) and bool(task.get('participants'))
        calls=int(result.get('codex_calls') or 0)+int(result.get('cursor_calls') or 0)
        offline=task.get('execution_mode')=='offline_cli'
        displayed=outcome(result,task)
        state=status_from_result_dict(result)
        if state=='verified' and not displayed['passed']:state='failed'
        execution_mode=task.get('execution_mode') if task.get('execution_mode') in {'mock','real','offline_cli'} else ('mock' if mock else '')
        return {'id':identifier,'projectId':project_id,'taskId':text(task.get('task_id') or run.parent.name,200),'title':text(task.get('title') or task.get('task_id'),300),'goal':text(task.get('goal'),100000),'state':state,'outcome':displayed,'started':text(result.get('started_at') or task.get('created_at'),50),'finished':text(result.get('finished_at'),50),'mode':text(result.get('mode_label') or execution_mode,200),'executionMode':execution_mode,'modelCalls':0 if offline else calls,'callKind':'offline_cli' if offline else 'mock' if mock else 'model'}

    def list_tasks(self,project_id:str='',offset:int=0,limit:int=100)->dict:
        page_args(offset,limit)
        self.refresh()
        if project_id and project_id not in self._projects and project_id!=ARCHIVE:raise ValueError('未知项目 ID')
        rows=[self._task(id) for id in self._runs]
        rows=[row for row in rows if not project_id or row['projectId']==project_id]
        rows.sort(key=lambda r:r['finished'] or r['started'] or r['id'],reverse=True)
        return {'items':rows[offset:offset+limit],'total':len(rows)}

    def _jsonl_page(self,path:Path,offset:int,limit:int)->tuple[list, int]:
        page_args(offset,limit)
        rows,total,_=self.index.read_page(self._safe(path),offset,limit,sanitized,MAX_PAGE_BYTES)
        return rows,total

    def load_task(self,identifier:str,message_offset:int=0,event_offset:int=0)->dict:
        if identifier not in self._runs:self.refresh()
        run=self._run(identifier);raw=self._json(run/'task.json'); result=self._json(run/'result.json')
        messages,total=self._jsonl_page(run/'messages.jsonl',message_offset,100)
        events,event_total=self._jsonl_page(run/'events.jsonl',event_offset,200)
        if any(e.get('task_id') and e['task_id']!=raw.get('task_id') for e in events):raise ValueError('执行事件与当前任务 ID 不一致')
        warnings=[]
        for m in messages:
            if m.get('task_id') and m.get('task_id')!=raw.get('task_id'):raise ValueError('消息与当前任务 ID 不一致')
            if m.pop('_integrityMismatch',False):warnings.append('消息摘要不一致：'+text(m.get('id'),100))
        message_index=self.index.get(self._safe(run/'messages.jsonl'))
        event_index=self.index.get(self._safe(run/'events.jsonl'))
        for name,index in [('messages.jsonl',message_index),('events.jsonl',event_index)]:
            if index['invalid']:warnings.append(f"{name}：{index['invalid']} 条损坏 / 未完整记录，未作为证据展示")
        permissions={k:raw.get(k) for k in ('flow','participants','max_model_calls','max_repair_rounds','max_rounds','timeout_seconds','max_run_seconds','allow_real_codex','allow_real_cursor','execution_mode')}
        usage_report=self._usage_report(run,raw)
        worktree=raw.get('worktree') or {};permissions['worktree_enabled']=bool(worktree.get('enabled') or worktree.get('path'));permissions['worktree_path']=worktree.get('path','')
        usage={k:int(result[k] if k in result else message_index['usage'].get(k,0)) for k in ('input_tokens','output_tokens')}
        usage.update({k:int(result.get(k) or 0) for k in ('codex_calls','cursor_calls','rounds_executed','repairs_used')})
        usage['mock_calls']=sum(a['invoked'] for id,a in message_index['agents'].items() if next((p.get('executor') for p in raw.get('participants',[]) if p.get('id')==id),None)=='mock')
        usage['offline_cli_calls']=int(result.get('offline_cli_calls') or sum(a['invoked'] for a in message_index['agents'].values())) if raw.get('execution_mode')=='offline_cli' else 0
        usage['real_model_calls']=0 if raw.get('execution_mode')=='offline_cli' else usage['codex_calls']+usage['cursor_calls']
        usage_recorded={k:(type(result.get(k)) is int or k in message_index['usage']) for k in ('input_tokens','output_tokens')}
        checks=self._json(run/'verification.json',[])
        if not isinstance(checks,list):raise ValueError('verification.json 格式不是验证数组')
        summary_fields=('type','ok','exit_code','superseded','evidence_invalidated','started_at','finished_at','attempt','baseline','diff_path','error')
        check_summary=[]
        for c in checks:
            if not isinstance(c,dict):continue
            item={k:c[k] for k in summary_fields if k in c}
            from re import findall
            counts=findall(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)',str(c.get('stdout','')))
            if counts:item['tests']=dict(zip(('run','failures','errors'),map(int,counts[-1])))
            if c.get('type')=='command':item['toolchain']='Maven / JUnit' if 'maven' in str(c.get('stdout','')).lower() or 'surefire' in str(c.get('stdout','')).lower() else '可信命令'
            if 'error' in item:item['error']=text(item['error'],300)
            check_summary.append(item)
        if any(c.get('task_id') and c['task_id']!=raw.get('task_id') for c in checks if isinstance(c,dict)):raise ValueError('验证证据与当前任务 ID 不一致')
        return {'taskId':identifier,'task':self._task(identifier),'messages':messages,'events':events,'messageTotal':total,'eventTotal':event_total,'messageOffset':message_offset,'eventOffset':event_offset,'permission':permissions,'usage':usage,'usageReport':usage_report,'usageRecorded':usage_recorded,'integrityWarnings':warnings,'agents':sanitized(message_index['agents']),'verificationSummary':check_summary[:500],'verificationTotal':len(check_summary),'resultSummary':{k:result[k] for k in ('stop_reason_zh','error_message','success','duration_seconds') if k in result},'hasResult':self._safe(run/'result.json').is_file()}

    def _usage_report(self,run,task):
        from dual_agent.usage import parse_usage,summarize_usage,unknown
        rows=[];folder=self._safe(run/'rounds')
        participants={p.get('id'):p for p in task.get('participants',[])}
        for path in sorted(folder.iterdir()) if folder.is_dir() else []:
            path=self._safe(path)
            if not path.is_dir() or not path.name.isdigit():continue
            request=self._json(path/'request.json');meta=self._json(path/'meta.json')
            participant=participants.get(request.get('agent'),{})
            executor=request.get('executor',participant.get('executor','unknown'))
            source=self._safe(path/'stdout.txt')
            stamp=signature(source.stat()) if source.is_file() else None
            key=(str(source),stamp,executor)
            value=self._usage_cache.get(key)
            if value is None:
                value=parse_usage(self._read(source,redact=False),executor) if stamp else unknown()
                self._usage_cache[key]=value
                while len(self._usage_cache)>64:self._usage_cache.popitem(last=False)
            # Re-read original CLI output rather than trust the legacy zero
            # defaults or a snapshot supplied by an untrusted model.
            rows.append({**value,'round':int(path.name),'agent':request.get('agent',''),
                'executor':executor,'role':request.get('role',participant.get('role','')),
                'invoked':meta.get('invoked'), 'source':f'rounds/{path.name}/'+value['source'],
                'simulation':task.get('execution_mode') in {'mock','offline_cli'}})
        return summarize_usage(rows,task.get('max_model_calls'))

    def _diff_sources(self,run:Path)->list[Path]:
        sources=[]
        for candidate in (run/'worktree.diff',run/'baseline'/'changes.diff'):
            if self._safe(candidate).is_file():sources.append(candidate)
        rounds=run/'rounds'
        if self._safe(rounds).is_dir():
            for folder in sorted(path for path in rounds.iterdir() if path.is_dir()):
                changes=folder/'changes.diff'
                if self._safe(changes).is_file():sources.append(changes)
        return sources

    def _diffs(self,run:Path)->list[dict]:
        sources=self._diff_sources(run)
        key=(str(run),tuple((str(path),signature(path.stat())) for path in sources))
        if sum(path.stat().st_size for path in sources)>MAX_FILE_BYTES:raise ValueError('累计 Diff 超过 8 MiB 读取上限')
        if key in self._diff_cache:
            self._diff_cache.move_to_end(key);return self._diff_cache[key][0]
        files=[]
        for source in sources:
            current=None;old=0;new=0
            source_lines=self._read(source).splitlines()
            has_patch_headers=any(line.startswith('diff --git ') for line in source_lines)
            # Coordinator can save "(无差异)" in a snapshot. It is evidence of
            # no patch, not a changed file or an executable tool event.
            if not has_patch_headers:continue
            for line in source_lines:
                if line.startswith('diff --git '):
                    if len(files)>=MAX_ITEMS:raise ValueError('Diff 文件数超过 500 上限')
                    path=line.rsplit(' b/',1)[-1];current={'id':hashlib.sha256((str(source.relative_to(run))+str(len(files))+path).encode()).hexdigest()[:24],'path':text(path,1000),'source':source.relative_to(run).as_posix(),'added':0,'removed':0,'lines':[]};files.append(current)
                if current is None:
                    if has_patch_headers:continue
                    current={'id':hashlib.sha256(str(source.relative_to(run)).encode()).hexdigest()[:24],'path':str(source.relative_to(run)),'source':str(source.relative_to(run)),'added':0,'removed':0,'lines':[]};files.append(current)
                kind='same';a='';b=''
                match=re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@',line)
                if match:old,new=int(match[1]),int(match[2]);kind='hunk'
                elif line.startswith('+') and not line.startswith('+++'):kind='add';b=str(new);new+=1;current['added']+=1
                elif line.startswith('-') and not line.startswith('---'):kind='remove';a=str(old);old+=1;current['removed']+=1
                elif line.startswith(' '):a,b=str(old),str(new);old+=1;new+=1
                current['lines'].append({'old':a,'next':b,'kind':kind,'text':line[1:] if kind in {'add','remove'} or line.startswith(' ') else line})
        cost=len(json.dumps(files,ensure_ascii=False).encode('utf8'))
        for old_key in list(self._diff_cache):
            if old_key[0]==str(run):del self._diff_cache[old_key]
        if cost<=16*1024*1024:self._diff_cache[key]=(files,cost)
        while len(self._diff_cache)>2 or sum(v[1] for v in self._diff_cache.values())>16*1024*1024:self._diff_cache.popitem(last=False)
        return files

    def load_evidence(self,identifier:str,kind:str,artifact_id:str='',offset:int=0,limit:int=300)->dict:
        page_args(offset,limit);run=self._run(identifier)
        if kind=='diff':
            files=self._diffs(run);meta=[{k:f[k] for k in ('id','path','source','added','removed')}|{'total':len(f['lines'])} for f in files]
            selected=next((f for f in files if f['id']==artifact_id),None) if artifact_id else (files[0] if files else None)
            if artifact_id and selected is None:raise ValueError('未知差异文件 ID')
            snapshot=bool(self._diff_sources(run))
            return {'taskId':identifier,'kind':kind,'files':meta,'lines':selected['lines'][offset:offset+limit] if selected else [],'total':len(selected['lines']) if selected else 0,'offset':offset,'source':selected['source'] if selected else '已保存无改动快照' if snapshot else '没有对应证据','present':bool(files),'snapshot':bool(snapshot)}
        if kind not in {'verification','result','logs'}:raise ValueError('不支持的证据类型')
        if artifact_id:raise ValueError('该证据类型不接受文件 ID')
        if kind=='logs':
            data,total=self._jsonl_page(run/'events.jsonl',offset,limit)
            raw=self._json(run/'task.json')
            if any(e.get('task_id') and e['task_id']!=raw.get('task_id') for e in data):raise ValueError('执行事件与当前任务 ID 不一致')
            index=self.index.get(self._safe(run/'events.jsonl'))
            return {'taskId':identifier,'kind':kind,'data':data,'total':total,'offset':offset,'source':'events.jsonl','present':self._safe(run/'events.jsonl').is_file(),'invalidRecords':index['invalid']}
        name=kind+'.json';data=self._json(run/name,[] if kind=='verification' else {})
        raw=self._json(run/'task.json')
        if kind=='result' and isinstance(data,dict) and data.get('task_id') and data['task_id']!=raw.get('task_id'):raise ValueError('result.json 与当前任务 ID 不一致')
        if kind=='verification' and isinstance(data,list) and any(c.get('task_id') and c['task_id']!=raw.get('task_id') for c in data if isinstance(c,dict)):raise ValueError('验证证据与当前任务 ID 不一致')
        return {'taskId':identifier,'kind':kind,'data':data,'source':name,'total':len(data) if isinstance(data,list) else 1,'present':self._safe(run/name).is_file()}

    def cache_stats(self):
        return {'indexBytes':self.index.bytes,'indexEntries':len(self.index.entries),'indexScans':self.index.scans,'indexHits':self.index.hits,'jsonBytes':sum(v[2] for v in self._json_cache.values()),'jsonEntries':len(self._json_cache),'diffBytes':sum(v[1] for v in self._diff_cache.values()),'diffEntries':len(self._diff_cache)}
