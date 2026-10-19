import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'desktop-validation'/'python')]
from PySide6.QtCore import QCoreApplication,QUrl
from PySide6.QtWebChannel import QWebChannel
from desktop_validation.store import HistoryStore,outcome
from desktop_validation.bridge import ReadOnlyBridge,METHODS
from desktop_validation.security import RestrictedTransport,allowed_url,LocalAssets
APP=QCoreApplication.instance() or QCoreApplication([])

class FakePage:
    def __init__(self):self.address=QUrl('studio://app/index.html');self.packets=[]
    def url(self):return self.address
    def runJavaScript(self,script):self.packets.append(json.loads(script.split('__channelReceive(',1)[1][:-1]))

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='studio-v03-bridge-');self.root=Path(self.temp.name);self.projects=self.root/'projects';self.runs=self.root/'runs';self.workspace=self.root/'workspace'
        for p in [self.projects,self.runs,self.workspace]:p.mkdir()
        project={'project_id':'sample','name':'只读集成测试','workspace':str(self.workspace),'allowed_roots':[str(self.root)],'participants':[{'id':'codex','executor':'mock','role':'developer'},{'id':'cursor','executor':'mock','role':'acceptor'}],'verification':[],'enabled':True}
        (self.projects/'sample.json').write_text(json.dumps(project),encoding='utf8')
        self.run=self.runs/'sample'/'20261012';self.run.mkdir(parents=True)
        self.task={'task_id':'sample-task','title':'真实文件读取测试','goal':'读取历史，不执行任何操作。','project_id':'sample','participants':project['participants'],'flow':['codex','cursor']}
        self.result={'task_id':'sample-task','success':True,'stop_reason':'verified','status':'verified','codex_calls':1,'cursor_calls':1}
        self.write('task.json',self.task);self.write('result.json',self.result);self.write('verification.json',[{'type':'command','ok':True,'exit_code':0,'stdout':'historical output'}])
        content='历史模型输出';message={'id':'m1','executor':'codex','role':'developer','content':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest(),'invoked':True}
        (self.run/'messages.jsonl').write_text(json.dumps(message,ensure_ascii=False)+'\n',encoding='utf8')
        (self.run/'events.jsonl').write_text('\n'.join(json.dumps({'time':str(i),'event':'historical','index':i}) for i in range(1200)),encoding='utf8')
        (self.run/'worktree.diff').write_text('$ git diff\ndiff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-before\n+after\n',encoding='utf8')
        self.store=HistoryStore(self.projects,self.runs);self.identifier=self.store.list_tasks()['items'][0]['id']
    def write(self,name,data):(self.run/name).write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')
    def tearDown(self):self.temp.cleanup()
    def test_registered_project_metadata(self):self.assertEqual(self.store.list_projects()[0]['id'],'sample')
    def test_history_is_read_from_files(self):self.assertEqual(self.store.list_tasks()['items'][0]['title'],self.task['title'])
    def test_messages_and_events(self):
        data=self.store.load_task(self.identifier);self.assertEqual(data['messageTotal'],1);self.assertEqual(data['eventTotal'],1200);self.assertEqual(data['messages'][0]['content'],'历史模型输出');self.assertFalse(data['integrityWarnings'])
    def test_diff_skips_non_patch_preamble(self):
        data=self.store.load_evidence(self.identifier,'diff');self.assertEqual(len(data['files']),1);self.assertEqual(data['files'][0]['path'],'a.py');self.assertEqual(data['files'][0]['added'],1)
    def test_round_diff_stays_inside_the_same_task(self):
        folder=self.run/'baseline';folder.mkdir();(folder/'changes.diff').write_text('diff --git a/old.py b/old.py\n--- a/old.py\n+++ b/old.py\n@@ -1 +1 @@\n-a\n+b\n',encoding='utf8')
        data=self.store.load_evidence(self.identifier,'diff');sources={item['source'] for item in data['files']}
        self.assertEqual(sources,{'worktree.diff','baseline/changes.diff'})
    def test_verification_and_result_are_independent(self):
        self.assertEqual(self.store.load_evidence(self.identifier,'verification')['data'][0]['exit_code'],0);self.assertEqual(self.store.load_evidence(self.identifier,'result')['data']['stop_reason'],'verified')
    def test_unknown_task_and_path_inputs_rejected(self):
        for value in ['../task',str(self.run),'file:///secret','not-known']:
            with self.assertRaises(ValueError):self.store.load_task(value)
    def test_evidence_kind_is_allowlisted(self):
        for kind in ['task.json','../result','shell','setVerified','write']:
            with self.assertRaises(ValueError):self.store.load_evidence(self.identifier,kind)
    def test_file_identifier_cannot_be_path(self):
        with self.assertRaises(ValueError):self.store.load_evidence(self.identifier,'diff','a.py')
    def test_pagination_is_bounded_and_exact(self):
        first=self.store.load_evidence(self.identifier,'logs','',0,300);second=self.store.load_evidence(self.identifier,'logs','',300,300);self.assertEqual(first['data'][-1]['index'],299);self.assertEqual(second['data'][0]['index'],300)
        for offset,limit in [(-1,3),(0,501),(0,0),(1.5,4)]:
            with self.assertRaises(ValueError):self.store.list_tasks('',offset,limit)
    def test_outside_file_is_rejected(self):
        with self.assertRaises(ValueError):self.store._read(self.root/'outside.json')
    def test_oversized_file_is_rejected(self):
        (self.run/'events.jsonl').write_bytes(b'x'*(8*1024*1024+1))
        with self.assertRaises(ValueError):self.store.load_evidence(self.identifier,'logs')
    def test_message_tamper_warning(self):
        (self.run/'messages.jsonl').write_text(json.dumps({'id':'tampered','content':'changed','content_sha256':'0'*64}),encoding='utf8')
        self.assertTrue(self.store.load_task(self.identifier)['integrityWarnings'])
    def test_fake_secret_redacted(self):
        self.result['error_message']='password=synthetic-test-value';self.write('result.json',self.result);self.assertNotIn('synthetic-test-value',json.dumps(self.store.load_evidence(self.identifier,'result')))
    def test_reading_never_changes_artifacts(self):
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.run.iterdir()};self.store.load_task(self.identifier);self.store.load_evidence(self.identifier,'diff');self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in before.items()))
    def test_status_semantics(self):
        readonly={'participants':[{'id':'c','role':'reviewer'}],'flow':['c']}
        for reason,success,kind in [('verified',True,'verified'),('flow_complete',False,'flow_complete'),('budget',False,'budget'),('cancelled',False,'cancelled'),('invalid_protocol',False,'failed')]:
            with self.subTest(reason=reason):self.assertEqual(outcome({'stop_reason':reason,'success':success},readonly)['kind'],kind)
        self.assertFalse(outcome({'stop_reason':'verified','success':False},readonly)['passed'])
    def test_native_channel_exposes_exactly_four_methods(self):
        bridge=ReadOnlyBridge(self.store);channel=QWebChannel();channel.registerObject('history',bridge);page=FakePage();transport=RestrictedTransport(page,bridge);channel.connectTo(transport);self.assertTrue(transport.receive('{"type":3,"id":0}'))
        metadata=page.packets[0]['data']['history'];self.assertEqual({m[0] for m in metadata['methods']},set(METHODS));self.assertEqual(metadata['properties'],[]);self.assertEqual(metadata['signals'],[])
    def test_actual_channel_rpc_calls_read_only_store(self):
        bridge=ReadOnlyBridge(self.store);channel=QWebChannel();channel.registerObject('history',bridge);page=FakePage();transport=RestrictedTransport(page,bridge);channel.connectTo(transport)
        self.assertTrue(transport.receive('{"type":3,"id":0}'));self.assertTrue(transport.receive('{"type":6,"id":1,"object":"history","method":"listProjects","args":[]}'));reply=json.loads(page.packets[-1]['data']);self.assertTrue(reply['ok']);self.assertEqual(reply['data'][0]['id'],'sample')
    def test_transport_denies_inherited_write_and_unknown_methods(self):
        bridge=ReadOnlyBridge(self.store);transport=RestrictedTransport(FakePage(),bridge)
        for packet in [{'type':9,'object':'history','property':0,'value':'changed'},{'type':6,'object':'history','method':'deleteLater','args':[]},{'type':6,'object':'history','method':'setVerified','args':[]},{'type':6,'object':'history','method':3,'args':[]},{'type':6,'object':'other','method':'listProjects','args':[]}]:self.assertFalse(transport.receive(json.dumps(packet)))
    def test_transport_denies_external_document_origin(self):
        bridge=ReadOnlyBridge(self.store);page=FakePage();page.address=QUrl('https://example.com/');transport=RestrictedTransport(page,bridge);self.assertFalse(transport.receive('{"type":3,"id":0}'))
    def test_resource_origin_allowlist(self):
        self.assertTrue(allowed_url(QUrl('studio://app/index.html')))
        for url in ['https://example.com','file:///C:/Windows/win.ini','studio://evil/index.html','studio://app:1234/index.html','qrc:///arbitrary.js']:self.assertFalse(allowed_url(QUrl(url)))
    def test_build_asset_map_is_exact(self):
        assets=LocalAssets(ROOT/'desktop-validation/frontend/dist');self.assertIn('/index.html',assets.files);self.assertNotIn('/../README.md',assets.files)

if __name__=='__main__':
    import time
    out=ROOT/'desktop-validation/measurements';out.mkdir(exist_ok=True);start=time.perf_counter()
    with (out/'bridge-tests.log').open('w',encoding='utf8') as stream:result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BridgeTests))
    summary={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'seconds':time.perf_counter()-start,'exit_code':0 if result.wasSuccessful() else 1}
    (out/'bridge-tests.json').write_text(json.dumps(summary,indent=2),encoding='utf8');print(summary);sys.exit(summary['exit_code'])
