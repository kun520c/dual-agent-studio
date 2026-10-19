"""Private offline fixture authority; never imported by production entrypoints."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zlib
from types import MappingProxyType

from dual_agent import PROJECT_ROOT
from dual_agent import execution_gate as gate
from dual_agent.projects import register_project


def make_key(target):
    result = subprocess.run(['node','-e', "const c=require('crypto'); const {privateKey}=c.generateKeyPairSync('rsa',{modulusLength:2048}); process.stdout.write(JSON.stringify(privateKey.export({format:'jwk'})))"], capture_output=True, check=True)
    target.write_bytes(result.stdout)
    return target


def decode(text): return int.from_bytes(base64.urlsafe_b64decode(text + '=' * (-len(text) % 4)), 'big')


def install(key_file):
    key = json.loads(Path(key_file).read_text())
    gate.TRUSTED_SIGNERS = MappingProxyType({'offline-test':{'n':hex(decode(key['n']))[2:],'e':65537,'modes':('offline_cli',)}})
    original = subprocess.Popen
    class Guarded(original):
        def __init__(self, args, *extra, **kwargs):
            tokens = list(args) if isinstance(args,(list,tuple)) else [args]
            base = Path(str(tokens[0])).stem.lower()
            installed_cursor = base == 'node' and any('cursor' in str(x).lower() and str(x).endswith('.js') for x in tokens[1:])
            if base in {'codex','cursor','cursor-agent','agent'} or installed_cursor:
                raise PermissionError('Installed CLI/probes forbidden in offline v0.5')
            if len(tokens)>2 and tokens[1:3] == ['-m','desktop_validation.task_host']:
                tokens = [tokens[0],str(PROJECT_ROOT/'desktop-validation/tests/v05_host_entry.py'),'--offline-key',str(key_file),*tokens[3:]]
            super().__init__(tokens,*extra,**kwargs)
    subprocess.Popen = Guarded
    os.environ.pop('DUAL_AGENT_ALLOW_CODEX',None)
    return key


def sign(payload, key_file):
    key = json.loads(Path(key_file).read_text()); n, d = decode(key['n']), decode(key['d']); size=(n.bit_length()+7)//8
    tail = bytes.fromhex('3031300d060960864801650304020105000420') + hashlib.sha256(gate.canonical(payload)).digest()
    encoded = b'\0\1' + b'\xff'*(size-len(tail)-3) + b'\0' + tail
    return {'payload':payload,'signature':base64.b64encode(pow(int.from_bytes(encoded,'big'),d,n).to_bytes(size,'big')).decode()}


def seed_repo(repo, scenario):
    repo.mkdir(parents=True)
    subprocess.run(['git','init','--quiet',str(repo)],check=True)
    # Hand-built fixture objects, no commit command and no user Git data.
    def obj(kind, content):
        raw=kind.encode()+b' '+str(len(content)).encode()+b'\0'+content
        sha=hashlib.sha1(raw).hexdigest();path=repo/'.git/objects'/sha[:2]/sha[2:];path.parent.mkdir(exist_ok=True);path.write_bytes(zlib.compress(raw));return sha
    entries=[]
    for name,value in [('scenario.txt',scenario+'\n'),('value.txt','4\n初始离线样本\n')]:
        raw=value.encode('utf8');(repo/name).write_bytes(raw);entries.append(b'100644 '+name.encode()+b'\0'+bytes.fromhex(obj('blob',raw)))
    tree=obj('tree',b''.join(entries));commit=obj('commit',f'tree {tree}\nauthor Offline Fixture <fixture@invalid> 1 +0000\ncommitter Offline Fixture <fixture@invalid> 1 +0000\n\nOffline fixture\n'.encode())
    (repo/'.git/refs/heads/fixture').write_text(commit+'\n');(repo/'.git/HEAD').write_text('ref: refs/heads/fixture\n')
    subprocess.run(['git','-C',str(repo),'read-tree','HEAD'],check=True)


def project(base, registry, runs, key_file, suffix='a', scenario='repair', resources=None):
    registry.mkdir(parents=True,exist_ok=True);runs.mkdir(parents=True,exist_ok=True)
    repo=runs/'studio-sources'/suffix;seed_repo(repo,scenario)
    root=runs/'studio-worktrees'/suffix
    script=PROJECT_ROOT/'desktop-validation/trusted/verify_mock.py'
    config={'project_id':'offline-'+suffix,'name':'离线 CLI '+suffix,'workspace':str(repo),'allowed_roots':[str(repo)],
      'participants':[{'id':'codex-dev','executor':'codex','role':'developer'},{'id':'cursor-review','executor':'cursor','role':'reviewer'},{'id':'cursor-accept','executor':'cursor','role':'acceptor'}],
      'verification':[{'type':'allowed_changes','allowed_paths':['value.txt'],'require_paths':['value.txt']},
      {'type':'command','argv':[sys.executable,str(script),str(repo)],'cwd':str(script.parent),'sha256':gate.digest(script),'timeout_seconds':3}],
      'worktree':{'enabled':True,'root':str(root),'base_ref':'HEAD'},'resource_locks':resources or [],
      'max_model_calls':6,'max_rounds':5,'max_repair_rounds':1,'timeout_seconds':5,'max_run_seconds':30}
    incoming=base/('incoming-'+suffix+'.json');incoming.write_text(json.dumps(config,ensure_ascii=False),encoding='utf8');register_project(incoming,registry);incoming.unlink()
    file=registry/('offline-'+suffix+'.json')
    payload={'version':1,'signer':'offline-test','mode':'offline_cli','projectId':'offline-'+suffix,'projects':str(registry),'runs':str(runs),'workspace':str(repo),'worktreeRoot':str(root),
        'projectSha256':gate.digest(file),'expires':time.time()+600,'maxTasks':20,'budget':{'max_model_calls':6,'max_rounds':5,'max_repair_rounds':1,'max_run_seconds':30,'timeout_seconds':5},
        'files':{str(p):gate.digest(p) for p in [Path(sys.executable),script,PROJECT_ROOT/'desktop-validation/tests/fake_cli_v05.py']},'cli':{}}
    target=runs/'studio-authorizations'/('offline-'+suffix+'.json');target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(sign(payload,key_file)),encoding='utf8')
    return target
