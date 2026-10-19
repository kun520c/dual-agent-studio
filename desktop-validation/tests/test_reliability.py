"""v0.3.1 regression additions; original assertions remain unchanged."""
import json, os, sys, time, threading, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from test_bridge import BridgeTests, FakePage, APP, ROOT
from desktop_validation.bridge import ReadOnlyBridge
from desktop_validation.security import RestrictedTransport
from desktop_validation.reader import ReadDispatcher
from PySide6.QtCore import QEventLoop, QTimer

class ReliabilityTests(unittest.TestCase):
    setUp=BridgeTests.setUp;tearDown=BridgeTests.tearDown;write=BridgeTests.write
    def test_cached_page_does_not_rescan(self):
        self.store.load_evidence(self.identifier,'logs');scans=self.store.index.scans
        self.assertEqual(self.store.load_evidence(self.identifier,'logs','',900)['data'][0]['index'],900)
        self.assertEqual(self.store.index.scans,scans)
    def test_append_truncate_replace_invalidate(self):
        path=self.run/'events.jsonl';self.store.load_evidence(self.identifier,'logs')
        with path.open('a',encoding='utf8') as f:f.write('\n'+json.dumps({'index':1200})+'\n')
        self.assertEqual(self.store.load_evidence(self.identifier,'logs','',1200)['data'][0]['index'],1200)
        path.write_text('{"index":7}\n',encoding='utf8')
        self.assertEqual(self.store.load_evidence(self.identifier,'logs')['total'],1)
        replacement=self.run/'replacement';replacement.write_text('{"index":9}\n',encoding='utf8');os.replace(replacement,path)
        self.assertEqual(self.store.load_evidence(self.identifier,'logs')['data'][0]['index'],9)
    def test_same_size_rewrite_invalidate(self):
        path=self.run/'events.jsonl';path.write_text('{"index":1}\n',encoding='utf8');self.store.load_evidence(self.identifier,'logs')
        path.write_text('{"index":2}\n',encoding='utf8')
        self.assertEqual(self.store.load_evidence(self.identifier,'logs')['data'][0]['index'],2)
    def test_partial_line_and_bad_utf8_are_explicit(self):
        path=self.run/'events.jsonl';path.write_bytes(b'\xef\xbb\xbf{"index":1}\nnot-json\n[]\n\xff\n{"index":')
        result=self.store.load_evidence(self.identifier,'logs');self.assertEqual(result['total'],1);self.assertEqual(result['invalidRecords'],4)
        with path.open('ab') as f:f.write(b'2}\n')
        result=self.store.load_evidence(self.identifier,'logs');self.assertEqual(result['total'],2);self.assertEqual(result['data'][-1]['index'],2)
    def test_missing_file_evicts_old_evidence(self):
        self.store.load_evidence(self.identifier,'logs');(self.run/'events.jsonl').unlink()
        result=self.store.load_evidence(self.identifier,'logs');self.assertFalse(result['present']);self.assertEqual(result['data'],[])
    def test_corrupt_json_fails_explicitly(self):
        (self.run/'result.json').write_text('{bad json',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'损坏'):self.store.load_evidence(self.identifier,'result')
    def test_usage_is_independent_of_message_page(self):
        path=self.run/'messages.jsonl';path.write_text('\n'.join(json.dumps({'id':str(i),'sender':'codex','content':'ok','input_tokens':i,'output_tokens':2}) for i in range(201)),encoding='utf8')
        first=self.store.load_task(self.identifier);second=self.store.load_task(self.identifier,100)
        self.assertEqual(first['usage'],second['usage']);self.assertEqual(first['usage']['input_tokens'],20100);self.assertEqual(first['agents']['codex']['saved'],201)
    def test_redaction_preserves_json_and_integrity(self):
        import hashlib
        content='password=synthetic-test-value';m={'id':'secret','sender':content,'content':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()}
        (self.run/'messages.jsonl').write_text(json.dumps(m)+'\n',encoding='utf8')
        result=self.store.load_task(self.identifier);self.assertEqual(result['integrityWarnings'],[]);self.assertNotIn('synthetic-test-value',json.dumps(result));self.assertEqual(result['messageTotal'],1)
        self.result['error_message']=content;self.write('result.json',self.result)
        result=self.store.load_evidence(self.identifier,'result');self.assertTrue(result['data']['success']);self.assertNotIn('synthetic-test-value',json.dumps(result))
    def test_result_and_message_task_mismatch_rejected(self):
        self.result['task_id']='different';self.write('result.json',self.result)
        with self.assertRaisesRegex(ValueError,'ID'):self.store.load_task(self.identifier)
        self.result['task_id']=self.task['task_id'];self.write('result.json',self.result)
        (self.run/'messages.jsonl').write_text('{"task_id":"different","content":"foreign"}\n',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'ID'):self.store.load_task(self.identifier)
    def test_two_task_diff_evidence_isolation(self):
        other=self.runs/'readonly'/'fixed';other.mkdir(parents=True)
        (other/'task.json').write_text(json.dumps({**self.task,'task_id':'readonly'}),encoding='utf8');(other/'result.json').write_text('{"task_id":"readonly","stop_reason":"budget","success":false}',encoding='utf8')
        rows=self.store.list_tasks()['items'];readonly=next(t['id'] for t in rows if t['taskId']=='readonly')
        for _ in range(5):
            self.assertEqual(self.store.load_evidence(self.identifier,'diff')['files'][0]['path'],'a.py')
            e=self.store.load_evidence(readonly,'diff');self.assertEqual(e['taskId'],readonly);self.assertEqual(e['files'],[]);self.assertFalse(e['present'])
    def test_no_change_snapshot_is_not_a_modified_file(self):
        (self.run/'worktree.diff').write_text('(无差异)\n',encoding='utf8')
        evidence=self.store.load_evidence(self.identifier,'diff');self.assertEqual(evidence['files'],[]);self.assertEqual(evidence['lines'],[]);self.assertTrue(evidence['snapshot'])
    def test_log_and_verification_task_mismatch_rejected(self):
        (self.run/'events.jsonl').write_text('{"task_id":"foreign","event":"status"}\n',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'ID'):self.store.load_evidence(self.identifier,'logs')
        with self.assertRaisesRegex(ValueError,'ID'):self.store.load_task(self.identifier)
        self.write('verification.json',[{'task_id':'foreign','ok':True}])
        with self.assertRaisesRegex(ValueError,'ID'):self.store.load_evidence(self.identifier,'verification')
    def test_index_cache_has_count_and_byte_bounds(self):
        for i in range(20):
            p=self.run/f'{i}.jsonl';p.write_text('{"index":1}\n',encoding='utf8');self.store._jsonl_page(p,0,1)
        self.assertLessEqual(len(self.store.index.entries),8);self.assertLessEqual(self.store.index.bytes,2*1024*1024)
    def test_json_cache_bounded(self):
        for i in range(45):self.write(f'{i}.json',{'text':'x'*100});self.store._json(self.run/f'{i}.json')
        self.assertLessEqual(len(self.store._json_cache),32);self.assertLessEqual(self.store.cache_stats()['jsonBytes'],8*1024*1024)
    def test_oversized_page_and_response_rejected(self):
        (self.run/'events.jsonl').write_text(json.dumps({'text':'x'*(512*1024+1)})+'\n',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'单条'):self.store.load_evidence(self.identifier,'logs')
        response=ReadOnlyBridge(self.store)._reply('test',lambda:'x'*(2*1024*1024));self.assertFalse(json.loads(response)['ok'])
    def test_transport_rejects_wrong_types_and_size(self):
        transport=RestrictedTransport(FakePage(),ReadOnlyBridge(self.store))
        for args in [['',False,100],['',0,'100'],['',0,100,1]]:
            self.assertFalse(transport.receive(json.dumps({'type':6,'id':1,'object':'history','method':'listTasks','args':args})))
        self.assertFalse(transport.receive(' '*16385))
    def test_background_worker_and_gui_heartbeat(self):
        bridge=ReadOnlyBridge(self.store);page=FakePage();transport=RestrictedTransport(page,bridge,async_reads=True);reader=transport.reader
        original=self.store.list_projects;main_thread=threading.get_ident();worker=[]
        def slow():worker.append(threading.get_ident());time.sleep(.18);return original()
        self.store.list_projects=slow
        loop=QEventLoop();beats=[];timer=QTimer();timer.timeout.connect(lambda:beats.append(time.perf_counter()));timer.start(10)
        transport.receive(json.dumps({'type':6,'id':1,'object':'history','method':'listProjects','args':[]}));self.assertEqual(page.packets,[])
        QTimer.singleShot(400,loop.quit);loop.exec();timer.stop();reader.shutdown()
        self.assertNotEqual(worker[0],main_thread);self.assertGreater(len(beats),15);self.assertTrue(json.loads(page.packets[-1]['data'])['ok'])
    def test_queue_is_bounded_and_document_epoch_drops_results(self):
        bridge=ReadOnlyBridge(self.store);delivered=[];reader=ReadDispatcher(bridge,lambda p,v:delivered.append((p,v)))
        original=self.store.load_evidence
        def slow(*args):time.sleep(.06);return original(*args)
        self.store.load_evidence=slow
        for i in range(20):reader.submit({'id':i,'args':[self.identifier,'logs','',i,1]},'loadEvidence')
        self.assertLessEqual(reader.peak_pending,8);self.assertTrue(any('队列已满' in v for _,v in delivered));delivered.clear();reader.invalidate()
        loop=QEventLoop();QTimer.singleShot(600,loop.quit);loop.exec();reader.shutdown();self.assertEqual(delivered,[])
    def test_cancelled_reads_do_not_accumulate_in_worker_queue(self):
        bridge=ReadOnlyBridge(self.store);reader=ReadDispatcher(bridge,lambda *_:None)
        original=self.store.load_task
        self.store.load_task=lambda *args:(time.sleep(.15),original(*args))[1]
        for i in range(1000):reader.submit({'id':i,'args':[self.identifier,0,0]},'loadTask')
        self.assertLessEqual(len(reader.pool.jobs),8);self.assertLessEqual(reader.pool.peak_queued,8);self.assertLessEqual(reader.peak_pending,8)
        reader.shutdown();reader.pool.shutdown(wait=True)
    def test_concurrent_append_is_retryable_and_final_page_exact(self):
        path=self.run/'events.jsonl';path.write_text('{"index":0}\n',encoding='utf8');errors=[]
        def writer():
            with path.open('a',encoding='utf8') as stream:
                for i in range(1,301):stream.write(json.dumps({'index':i})+'\n');stream.flush();time.sleep(.0005)
        thread=threading.Thread(target=writer);thread.start()
        while thread.is_alive():
            try:
                result=self.store.load_evidence(self.identifier,'logs','',0,50)
                self.assertEqual([v['index'] for v in result['data']],list(range(len(result['data']))))
            except ValueError as error:errors.append(str(error))
        thread.join();result=self.store.load_evidence(self.identifier,'logs','',300,1);self.assertEqual(result['total'],301);self.assertEqual(result['data'][0]['index'],300)
    def test_opened_handle_cannot_escape_root(self):
        from desktop_validation.files import opened_under_root
        outside=self.root/'outside';outside.write_text('synthetic',encoding='utf8')
        with outside.open('rb') as stream:
            with self.assertRaisesRegex(ValueError,'根目录'):opened_under_root(stream,self.runs)
    def test_completed_evidence_request_keys_are_released(self):
        reader=ReadDispatcher(ReadOnlyBridge(self.store),lambda *_:None)
        for batch in range(10):
            for i in range(8):reader.submit({'id':batch*8+i,'args':[self.identifier,'logs','',i,1]},'loadEvidence')
            deadline=time.perf_counter()+2
            while reader.pending and time.perf_counter()<deadline:
                loop=QEventLoop();QTimer.singleShot(10,loop.quit);loop.exec()
            self.assertFalse(reader.pending);self.assertFalse(reader.latest)
        reader.shutdown();reader.pool.shutdown(wait=True)
    def test_unknown_token_usage_is_distinct_from_saved_zero(self):
        result=self.store.load_task(self.identifier);self.assertFalse(result['usageRecorded']['input_tokens']);self.assertFalse(result['usageRecorded']['output_tokens'])
        self.result.update(input_tokens=0,output_tokens=0);self.write('result.json',self.result)
        result=self.store.load_task(self.identifier);self.assertTrue(result['usageRecorded']['input_tokens']);self.assertTrue(result['usageRecorded']['output_tokens']);self.assertEqual(result['usage']['input_tokens'],0)

if __name__=='__main__':
    start=time.perf_counter();out=ROOT/'desktop-validation/measurements-v031'
    with (out/'reliability-tests.log').open('w',encoding='utf8') as stream:r=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReliabilityTests))
    result={'tests':r.testsRun,'failures':len(r.failures),'errors':len(r.errors),'skipped':len(r.skipped),'seconds':time.perf_counter()-start,'exit_code':0 if r.wasSuccessful() else 1}
    (out/'reliability-tests.json').write_text(json.dumps(result,indent=2),encoding='utf8');print(result);sys.exit(result['exit_code'])
