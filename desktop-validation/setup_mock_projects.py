"""Native one-time registration of dedicated mock projects. No production access."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(ROOT))
from dual_agent.projects import register_project

def setup(base=HERE, registry=ROOT/'projects'):
    base=Path(base).resolve();registry=Path(registry).resolve();registry.mkdir(parents=True,exist_ok=True)
    workspaces=base/'mock-workspaces';workspaces.mkdir(exist_ok=True)
    trusted=HERE/'trusted'/'verify_mock.py';digest=hashlib.sha256(trusted.read_bytes()).hexdigest()
    projects={}
    for suffix,name in [('a','模拟工作台 A'),('b','模拟工作台 B'),('c','模拟工作台 C')]:
        project_id='studio-mock-'+suffix;workspace=workspaces/suffix
        workspace.mkdir(exist_ok=True)
        if not (workspace/'value.txt').exists():
            (workspace/'value.txt').write_text('4\n初始模拟样本\n',encoding='utf8')
        # An index baseline provides a genuine Git patch without any commit.
        if not (workspace/'.git').exists():
            subprocess.run(['git','init','--quiet',str(workspace)],check=True)
            subprocess.run(['git','-C',str(workspace),'add','value.txt'],check=True)
        project={'project_id':project_id,'name':name,'workspace':str(workspace),'allowed_roots':[str(workspace)],
            'participants':[{'id':'mock-dev','executor':'mock','role':'developer'},{'id':'mock-review','executor':'mock','role':'reviewer'},{'id':'mock-accept','executor':'mock','role':'acceptor'}],
            'verification':[{'type':'allowed_changes','allowed_paths':['value.txt'],'require_paths':['value.txt']},
                {'type':'command','argv':[sys.executable,str(trusted),str(workspace)],'cwd':str(trusted.parent),'sha256':digest,'timeout_seconds':10}],
            'max_model_calls':6,'max_rounds':4,'max_repair_rounds':1,'timeout_seconds':25,'max_run_seconds':90,
            'concurrency':{'max_write_tasks':1,'allow_shared_context':False},'resource_locks':[],
            'description':'v0.4-A 专用隔离项目。只运行 mock，不连接数据库或外部 API。'}
        file=registry/(project_id+'.json')
        if not file.exists():
            incoming=base/(project_id+'.registration.json')
            incoming.write_text(json.dumps(project,ensure_ascii=False,indent=2),encoding='utf8')
            register_project(incoming,registry)
            incoming.unlink()
        projects[project_id]={'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    policy={'workspaceRoot':str(workspaces),'projects':projects,'trusted':{str(trusted):digest}}
    target=base/'mock-policy.json';target.write_text(json.dumps(policy,ensure_ascii=False,indent=2),encoding='utf8')
    return target

if __name__=='__main__':print(setup())
