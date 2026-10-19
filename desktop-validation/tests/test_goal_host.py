"""Phase B1 actual TaskHost/Scheduler/Coordinator OFFLINE/FAKE_CLI tests."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from dual_agent import PROJECT_ROOT
from dual_agent import execution_gate as gate
from dual_agent.goal_ledger import GoalService, ConflictError, TransitionError, EvidenceError, StorageBusyError
from dual_agent.goal_ledger import AttemptStatus as A, WorkStatus as W, GoalStatus as G
from desktop_validation.task_api import TaskPolicy, BusinessError
from desktop_validation.task_host import TaskHost
from dual_agent.status import read_status
from v05_support import make_key, install, project

OUT = PROJECT_ROOT / 'docs/maintenance/2026-10-18-phase-b1'


def plan(names=('A','B'), chain=True):
    return dict(summary='OFFLINE / FAKE_CLI 两项依赖工程任务',work_items=[dict(work_item_id=name,
        title='OFFLINE '+name,description='在本任务独立 Worktree 修改 value.txt，实现 value=5；保留任务身份。禁止修改源仓库。',
        dependencies=[names[i-1]] if chain and i else [],acceptance_criteria=['原有可信 verify_mock.py 检查 value=5 通过'],
        allowed_write_scope=['value.txt']) for i,name in enumerate(names)])


class GoalHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='goal-host-中文-')
        self.root = Path(self.temp.name)
        self.registry,self.runs = self.root/'projects',self.root/'runs'
        self.key = make_key(self.root/'ephemeral-offline-key.json')
        self.oldkeys,self.oldpopen = gate.TRUSTED_SIGNERS,subprocess.Popen
        install(self.key)
        project(self.root,self.registry,self.runs,self.key,scenario='pass')
        self.policy = self.root/'mock-policy.json'; self.policy.write_text('{}')
        self.host = TaskHost(TaskPolicy(self.registry,self.runs,self.policy))
        self.gh,self.s = self.host.goal_host,self.host.goals
        self.g = self.s.create_goal(title='B1 OFFLINE',original_goal='两项有依赖的离线工程任务；真实模型调用为0',idempotency_key='goal')
        self.seq = 0

    def tearDown(self):
        self.host.scheduler.close()
        self.host.goal_host.close()
        gate.TRUSTED_SIGNERS,subprocess.Popen = self.oldkeys,self.oldpopen
        if not self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()): raise AssertionError('cleanup escaped test root')
        for i in range(8):
            try: self.temp.cleanup(); break
            except PermissionError:
                if i==7: raise
                time.sleep(.1*(i+1))

    def activate(self,names=('A','B'),chain=True):
        p = self.s.create_plan(self.g.goal_id,plan(names,chain),parent_revision=None,expected_revision=0,idempotency_key='plan')
        self.s.activate_plan(self.g.goal_id,p.revision,expected_revision=1,idempotency_key='activate')

    def request(self,wid='A',**extra):
        data = dict(goalId=self.g.goal_id,workItemId=wid,projectId='offline-a',templateId='collaborate',executionMode='offline_cli',
            expectedRevision=self.s.get_goal(self.g.goal_id).revision,idempotencyKey='submit-'+wid)
        data.update(extra)
        return json.dumps(data,ensure_ascii=False)

    def submit(self,wid='A',**extra): return self.gh.submit_work_item(self.request(wid,**extra))

    def wait(self,mapping):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            ctx = self.gh._contexts.get(mapping['attempt_id'])
            if ctx is not None and ctx.result is not None:
                return ctx.result
            row = read_status(self.runs/'schedule'/(mapping['schedule_id']+'.json'))
            if row.get('state') in {'failed','cancelled'}:
                return None
            time.sleep(.03)
        self.fail('TaskHost execution did not finish: '+str(read_status(self.runs/'schedule'/(mapping['schedule_id']+'.json'))))

    def finish(self,wid='A',accept=True):
        m=self.submit(wid); result=self.wait(m)
        self.assertIsNotNone(result)
        self.assertEqual(result.stop_reason,'verified',result.to_dict())
        reconciled=self.gh.reconcile_attempt(m['attempt_id'])
        self.assertTrue(reconciled['trusted_now'],str(self.gh._contexts[m['attempt_id']].error))
        self.assertEqual(self.s.get_attempt(m['attempt_id']).status,A.RESULT_PENDING)
        if accept:
            self.gh.accept_result(m['attempt_id'],reason='OFFLINE/FAKE_CLI deterministic integration acceptance',idempotency_key='accept-'+wid)
        return m,result

    def test_B1_01_actual_host_two_item_DAG_and_restart_traceability(self):
        self.activate()
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)],['A'])
        a,ra=self.finish('A')
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)],['B'])
        b,rb=self.finish('B')
        self.assertEqual(self.s.get_goal(self.g.goal_id).status,G.COMPLETED)
        self.assertTrue(self.s.current_state(self.g.goal_id)['completion_currently_usable'])
        for m,r in [(a,ra),(b,rb)]:
            self.assertEqual(r.offline_cli_calls,3)
            self.assertEqual(r.run_dir,self.runs/m['run_id'])
            self.assertTrue(all(msg.mode_label.startswith('离线模拟 CLI') for msg in r.messages))
            self.assertIn(m['task_id'],(Path(r.workspace)/'value.txt').read_text(encoding='utf8'))
            self.assertTrue((r.run_dir/'execution.claim').exists())
            tree=json.loads((r.run_dir/'worktree.json').read_text(encoding='utf8'))
            self.assertEqual(tree['base_commit'],m['base_head'])
            self.assertEqual(tree['base_ref'],m['base_head'])
        self.assertNotEqual(a['run_id'],b['run_id'])
        self.assertNotEqual(a['workspace_ref'],b['workspace_ref'])
        self.assertEqual((self.runs/'studio-sources/a/value.txt').read_text(encoding='utf8'),'4\n初始离线样本\n')
        # Retain an explicit allowlist of the actual evidence chain, no key/grant.
        target=OUT/'e2e-evidence'/str(time.time_ns()); target.mkdir(parents=True)
        proof=dict(label='OFFLINE / FAKE_CLI; no Manager model; real model calls 0',goal=self.s.current_state(self.g.goal_id),
                   mappings=self.s.list_dispatches(self.g.goal_id),events=[e.to_dict() for e in self.s.events(self.g.goal_id)])
        (target/'chain.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf8')
        for name,r in [('A',ra),('B',rb)]:
            folder=target/name;folder.mkdir()
            for artifact in ['task.json','result.json','verification.json','messages.jsonl','events.jsonl','workspace_snapshot.json','worktree.json','worktree.diff']:
                source=r.run_dir/artifact
                if source.is_file(): (folder/artifact).write_bytes(source.read_bytes())
        self.host.scheduler.close();self.gh.close()
        self.host=TaskHost(TaskPolicy(self.registry,self.runs,self.policy));self.gh,self.s=self.host.goal_host,self.host.goals
        self.assertEqual(self.s.get_dispatch(a['attempt_id'])['run_id'],a['run_id'])
        self.assertEqual(self.s.get_goal(self.g.goal_id).status,G.COMPLETED)
        self.assertFalse(self.s.current_state(self.g.goal_id)['completion_currently_usable'])
        self.assertEqual(self.gh.reconcile_attempt(b['attempt_id'])['recovery'],'TERMINAL')

    def test_B1_02_duplicate_request_queries_original_execution(self):
        self.activate(('A',))
        raw=self.request()
        m=self.gh.submit_work_item(raw)
        again=self.gh.submit_work_item(raw)
        self.assertEqual(m['attempt_id'],again['attempt_id'])
        self.wait(m);self.gh.reconcile_attempt(m['attempt_id'])
        self.assertEqual(len(self.s.list_attempts('A')),1)
        self.assertEqual(len(list((self.runs/m['task_id']).glob('*/execution.claim'))),1)
        with self.assertRaises(ConflictError): self.submit(projectId='offline-other')

    def test_B1_03_concurrent_claimants_dispatch_only_once(self):
        self.activate(('A',));raw=self.request()
        with ThreadPoolExecutor(max_workers=2) as pool:
            mappings=list(pool.map(self.gh.submit_work_item,[raw,raw]))
        self.assertEqual(mappings[0]['attempt_id'],mappings[1]['attempt_id'])
        self.wait(mappings[0])
        self.assertEqual(len(self.s.list_attempts('A')),1)
        self.assertEqual(len(list((self.runs/'studio-host/requests').glob('*.json'))),1)

    def test_B1_04_persisted_intent_without_submit_is_never_replayed(self):
        self.activate(('A',))
        with patch.object(self.host,'submit',side_effect=OSError('injected before TaskHost call')):
            with self.assertRaises(OSError): self.submit()
        m=self.s.list_dispatches(self.g.goal_id)[0]
        self.gh._contexts.clear()
        with patch.object(self.host,'submit',side_effect=AssertionError('must not replay')):
            state=self.gh.reconcile_attempt(m['attempt_id'])
            self.assertEqual(state['recovery'],'UNKNOWN_SIDE_EFFECT')
            self.assertEqual(self.s.get_work_item('A').status,W.BLOCKED)
            self.gh.reconcile_attempt(m['attempt_id'])
        self.assertFalse((self.runs/m['run_id']/'execution.claim').exists())

    def test_B1_05_host_accepted_but_acknowledgement_lost(self):
        self.activate(('A',))
        original=self.host.submit
        def lost(*args,**kwargs):
            original(*args,**kwargs)
            raise OSError('lost TaskHost reply')
        with patch.object(self.host,'submit',side_effect=lost):
            with self.assertRaises(OSError): self.submit()
        m=self.s.list_dispatches(self.g.goal_id)[0]
        self.wait(m)
        with patch.object(self.host,'submit',side_effect=AssertionError('no second execution')):
            reconciled=self.gh.reconcile_attempt(m['attempt_id'])
        self.assertTrue(reconciled['trusted_now'])
        self.gh.accept_result(m['attempt_id'],reason='OFFLINE acknowledgement reconciliation',idempotency_key='ack-lost')
        self.assertEqual(self.s.get_work_item('A').status,W.COMPLETED)

    def test_B1_06_restart_discards_running_context_and_never_reexecutes(self):
        self.activate(('A',));m=self.submit()
        deadline=time.monotonic()+10
        while not self.gh._contexts[m['attempt_id']].started and time.monotonic()<deadline: time.sleep(.01)
        self.assertTrue(self.gh._contexts[m['attempt_id']].started)
        # Drop all process-local observations as a new host epoch would. The
        # original scheduler still owns its worker/locks until explicit close.
        from dual_agent.goal_host.service import GoalHost
        fresh=GoalHost(self.host)
        try:
            with patch.object(self.host,'submit',side_effect=AssertionError('no restart replay')):
                for _ in range(2): self.assertEqual(fresh.reconcile_attempt(m['attempt_id'])['recovery'],'NEEDS_RECONCILIATION')
            self.assertEqual(fresh.goals.get_work_item('A').status,W.BLOCKED)
        finally: fresh.close()
        self.wait(m)

    def _old_and_new(self):
        self.activate(('A',));old=self.submit();self.wait(old)
        self.s.block_work_item(self.g.goal_id,'A',reason='offline reconciliation for retry',
            expected_revision=self.s.get_goal(self.g.goal_id).revision,idempotency_key='retry-block')
        self.s.retry_work_item(self.g.goal_id,'A',reconciliation_note='Original scheduler finished and processes are cleared',
            expected_revision=self.s.get_goal(self.g.goal_id).revision,idempotency_key='retry-allow')
        new=self.submit(idempotencyKey='second-attempt')
        return old,new

    def test_B1_07_late_started_from_old_generation_rejected(self):
        old,new=self._old_and_new()
        with self.assertRaises(ConflictError): self.gh._event(old,'STARTED')
        self.assertEqual(self.s.get_work_item('A').current_attempt_id,new['attempt_id'])
        self.wait(new)

    def test_B1_08_late_result_from_old_attempt_rejected(self):
        old,new=self._old_and_new()
        with self.assertRaises(ConflictError): self.gh.reconcile_attempt(old['attempt_id'])
        self.assertEqual(self.s.get_work_item('A').current_attempt_id,new['attempt_id'])
        self.wait(new)

    def test_B1_09_copied_or_forged_verified_json_cannot_create_observation(self):
        self.activate(('A','B'),chain=False)
        a,result=self.finish('A',accept=False)
        self.assertTrue(self.gh._currently_usable(a))
        path=result.run_dir/'verification.json';original=path.read_bytes()
        path.write_text('[{"verified":true,"ok":true,"exit_code":0}]')
        self.assertFalse(self.gh._currently_usable(a))
        with self.assertRaises(EvidenceError): self.gh.accept_result(a['attempt_id'],reason='forged',idempotency_key='forged')
        path.write_bytes(original)
        self.gh.source.forget(a['attempt_id'])
        self.assertFalse(self.gh._currently_usable(a))
        self.gh.reconcile_attempt(a['attempt_id'])  # direct result cannot recreate forgotten lock-time observation
        self.assertFalse(self.gh._currently_usable(a))

    def test_B1_10_workspace_or_HEAD_change_invalidates_result(self):
        self.activate(('A',));m,result=self.finish(accept=False)
        file=Path(result.workspace)/'value.txt';original=file.read_bytes()
        file.write_bytes(original+b'changed-after-verification')
        self.assertFalse(self.gh._currently_usable(m))
        with self.assertRaises(EvidenceError): self.gh.accept_result(m['attempt_id'],reason='stale',idempotency_key='stale')
        file.write_bytes(original)
        self.assertTrue(self.gh._currently_usable(m))
        # Detach HEAD at a different valid fixture commit object without product commits.
        from dual_agent.worktree import _git
        code,branch,error=_git(Path(result.workspace),['symbolic-ref','HEAD'])
        self.assertEqual(code,0,error)
        headfile=self.runs/'studio-sources/a/.git'/branch.strip()
        old=headfile.read_text();headfile.write_text('0'*40+'\n')
        try: self.assertFalse(self.gh._currently_usable(m))
        finally: headfile.write_text(old)

    def test_B1_11_cancellation_before_acceptance_never_completes_work(self):
        self.activate(('A',));m,result=self.finish(accept=False)
        self.gh.cancel_attempt(m['attempt_id'])
        self.assertEqual(self.s.get_attempt(m['attempt_id']).status,A.CANCELLED)
        self.assertEqual(self.s.get_work_item('A').status,W.CANCELLED)
        with self.assertRaises((EvidenceError,TransitionError)):
            self.gh.accept_result(m['attempt_id'],reason='late accept',idempotency_key='late')

    def test_B1_11_cancel_and_accept_race_has_one_terminal_outcome(self):
        self.activate(('A',));m,_=self.finish(accept=False)
        def accept():
            try: return self.gh.accept_result(m['attempt_id'],reason='OFFLINE race',idempotency_key='race')
            except (EvidenceError,TransitionError): return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(accept),pool.submit(self.gh.cancel_attempt,m['attempt_id'])]
            [f.result() for f in futures]
        self.assertIn(self.s.get_work_item('A').status,{W.COMPLETED,W.CANCELLED})
        self.assertEqual(len([e for e in self.s.events(self.g.goal_id) if e.kind in {'attempt.accepted','attempt.cancelled'}]),1)

    def test_B1_12_original_scheduler_serializes_same_project_and_resource(self):
        self.activate(('A','B'),chain=False)
        from dual_agent.locks import WriteLockManager
        # Pre-reserve both identities, then force the same protected worktree
        # through existing Mock-free scheduler SchedItem conflict calculation.
        from dual_agent.scheduler import SchedItem,pick_runnable
        item=SchedItem('first','offline-a',('C:/fixture/tree',),('shared',),'C:/fixture/tree',True,1)
        other=SchedItem('second','offline-b',('C:/fixture/tree',),('shared',),'C:/fixture/tree',True,1)
        self.assertIsNone(pick_runnable([other],[item],2))
        # Actual host tasks in one registered project must also serialize.
        a=self.submit('A');b=self.submit('B')
        ra=self.wait(a);rb=self.wait(b)
        self.assertEqual(ra.stop_reason,'verified');self.assertEqual(rb.stop_reason,'verified')
        self.assertEqual(self.host.scheduler.peak,1)

    def test_B1_13_no_signature_cannot_be_bypassed_by_goal(self):
        self.activate(('A',))
        with patch.object(gate,'TRUSTED_SIGNERS',{}),patch('dual_agent.winproc.subprocess.Popen',side_effect=AssertionError('must not launch')):
            with self.assertRaises(BusinessError): self.submit()
            with self.assertRaises(BusinessError): self.submit(executionMode='real')
        self.assertEqual(self.s.get_work_item('A').status,W.READY)
        self.assertEqual(self.s.list_attempts('A'),[])

    def test_B1_14_legacy_single_task_uses_original_path(self):
        raw=json.dumps(dict(requestId='legacy',projectId='offline-a',title='OFFLINE legacy',goal='value=5',templateId='collaborate',executionMode='offline_cli'))
        saved=self.host.submit(raw)
        self.assertEqual(self.host.submit(raw)['taskId'],saved['taskId'])
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            row=read_status(self.runs/'schedule'/(saved['scheduleId']+'.json'))
            if row.get('state') in {'verified','failed','cancelled'}: break
            time.sleep(.02)
        self.assertEqual(row['state'],'verified',row)
        self.assertEqual(self.s.list_dispatches(self.g.goal_id),[])

    def test_B1_15_reconciliation_reentry_has_no_extra_events_or_attempts(self):
        self.activate(('A',));m,_=self.finish(accept=False)
        before=self.s.get_goal(self.g.goal_id).revision
        for _ in range(4): self.gh.reconcile_goal(self.g.goal_id)
        self.assertEqual(self.s.get_goal(self.g.goal_id).revision,before)
        self.assertEqual(len(self.s.list_attempts('A')),1)

    def test_B1_16_cross_attempt_run_generation_evidence_binding(self):
        self.activate(('A','B'),chain=False);m,result=self.finish(accept=False)
        from dataclasses import replace
        a=self.s.get_attempt(m['attempt_id']);path=a.run_id+'/verification.json'
        with self.gh.source.guard(self.g.goal_id):
            sha=self.gh.source.artifact_hash(a,path)
            for changed in [replace(a,attempt_id='other'),replace(a,generation=999),replace(a,run_id='other/run'),replace(a,work_item_id='B')]:
                if changed.attempt_id=='other': self.assertIsNone(self.gh.source.assess(changed,path,sha))
                else:
                    with self.assertRaises(EvidenceError): self.gh.source.assess(changed,path,sha)
        b=self.s.create_goal(title='other',original_goal='other',idempotency_key='other-goal')
        with self.assertRaises(ConflictError):
            self.s.record_evidence(b.goal_id,a.attempt_id,generation=a.generation,evidence_path=path,expected_revision=0,idempotency_key='wrong-goal')

    def test_B1_17_closed_interfaces_reject_untrusted_capabilities(self):
        self.activate(('A',))
        for field in ['database_path','evidence_source','execution_authority','argv','sql','workspace','run_id','verified']:
            p=json.loads(self.request());p[field]='injected'
            with self.subTest(field=field),self.assertRaises(BusinessError): self.gh.submit_work_item(json.dumps(p))
        for method in ['submitGoalWork','rawSQL','exec']:
            with self.assertRaises(BusinessError): self.host.dispatch(method,'{}')

    def test_B1_18_actual_phase_A_zip_v1_database_upgrades_without_data_loss(self):
        package=self.root/'legacy_package';package.mkdir()
        # Exact legacy Python source only; no internal handoff ZIP is public.
        import shutil
        for source in (PROJECT_ROOT/'desktop-validation/tests/fixtures/ledger_v1').glob('*.py'):
            shutil.copyfile(source,package/source.name)
        name='phase_a_legacy_fixture'
        spec=importlib.util.spec_from_file_location(name,package/'__init__.py',submodule_search_locations=[str(package)])
        legacy=importlib.util.module_from_spec(spec);sys.modules[name]=legacy;spec.loader.exec_module(legacy)
        runs=self.root/'v1-runs'
        old=legacy.GoalService(runs)
        g=old.create_goal(title='真实 Phase A v1',original_goal='原文保留',idempotency_key='v1-goal')
        p=old.create_plan(g.goal_id,plan(('legacy-A',)),parent_revision=None,expected_revision=0,idempotency_key='v1-plan')
        old.close()
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(runs/'studio-host/goal-ledger/ledger.sqlite3')) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],1)
        with GoalService(runs) as upgraded:
            self.assertEqual(upgraded.get_goal(g.goal_id).original_goal,g.original_goal)
            self.assertEqual(upgraded.get_plan(g.goal_id,1).to_dict(),p.to_dict())
            replay=upgraded.create_goal(title=g.title,original_goal=g.original_goal,idempotency_key='v1-goal')
            self.assertEqual(replay.to_dict(),g.to_dict())
            self.assertEqual(upgraded.check_integrity(),[])

    def test_B1_19_sqlite_busy_before_intent_does_not_submit(self):
        self.activate(('A',))
        from dual_agent.goal_host.identity import reserve
        payload=dict(projectId='offline-a',title='reserved',goal='value=5',templateId='collaborate',executionMode='offline_cli',requestId='busy-only')
        # Use a contender after initialization, then hold a real SQLite writer.
        with GoalService(self.runs,busy_timeout_ms=20) as contender:
            with self.s._db.transaction():
                with self.assertRaises(StorageBusyError): contender.create_goal(title='busy',original_goal='busy',idempotency_key='busy')
        self.assertEqual(len(self.host.scheduler.tasks),0)
        self.assertEqual(self.s.list_attempts('A'),[])

    def test_B1_20_unicode_worktree_process_cleanup_and_lock_release(self):
        self.activate(('A',));m,result=self.finish()
        self.assertIn('中文',str(result.workspace))
        self.assertTrue(result.processes_cleared);self.assertTrue(result.lock_released)
        self.assertTrue(Path(result.workspace).is_dir())
        from dual_agent.locks import WriteLockManager
        from dual_agent.coordinator import default_lock_dir
        lock,_=WriteLockManager(default_lock_dir()).try_acquire('B1-release-test',[Path(result.workspace)])
        self.assertIsNotNone(lock)
        lock.release()

    def test_crash_window_01_uncommitted_attempt_and_mapping_roll_back_together(self):
        self.activate(('A',))
        original=self.s._event
        def fail(c,*args,**kwargs):
            original(c,*args,**kwargs)
            raise RuntimeError('injected transaction failure')
        with patch.object(self.s,'_event',side_effect=fail):
            with self.assertRaises(RuntimeError): self.submit()
        self.assertEqual(self.s.list_attempts('A'),[])
        self.assertEqual(self.s.list_dispatches(self.g.goal_id),[])
        self.assertEqual(self.s.get_goal(self.g.goal_id).revision,2)
        self.assertEqual(len(self.host.scheduler.tasks),0)
        self.assertFalse(list(self.runs.glob('studio-*/*/execution.claim')))
        # Reservation is real and safe to reuse before any intent was committed.
        m=self.submit();self.wait(m)

    def test_crash_window_02_real_process_exit_after_atomic_intent(self):
        child=self.root/'crash-child';child.mkdir()
        registry,runs=child/'projects',child/'runs'
        project(child,registry,runs,self.key,scenario='pass')
        (child/'policy.json').write_text('{}')
        done=subprocess.run([sys.executable,'-B',str(PROJECT_ROOT/'desktop-validation/tests/goal_host_crash_entry.py'),str(child),str(self.key)],capture_output=True,timeout=20)
        self.assertEqual(done.returncode,77,done.stderr)
        proof=json.loads((child/'crash-proof.json').read_text(encoding='utf8'))
        recovered=TaskHost(TaskPolicy(registry,runs,child/'policy.json'))
        try:
            with patch.object(recovered,'submit',side_effect=AssertionError('must not replay crashed intent')):
                for _ in range(2):
                    state=recovered.goal_host.reconcile_attempt(proof['attempt_id'])
                    self.assertEqual(state['recovery'],'UNKNOWN_SIDE_EFFECT')
            self.assertEqual(recovered.goals.get_dispatch(proof['attempt_id'])['run_id'],proof['run_id'])
            self.assertFalse((runs/proof['run_id']/'execution.claim').exists())
            self.assertEqual(recovered.goals.check_integrity(),[])
        finally:
            recovered.scheduler.close();recovered.goal_host.close()

    def test_crash_windows_05_06_start_and_result_saved_before_ledger_callbacks(self):
        self.activate(('A',));m=self.submit();result=self.wait(m)
        self.assertEqual(result.stop_reason,'verified')
        self.assertEqual(self.s.get_attempt(m['attempt_id']).status,A.DISPATCH_INTENT)
        self.assertTrue((self.runs/m['run_id']/'result.json').exists())
        self.gh.reconcile_attempt(m['attempt_id'])
        self.assertEqual(self.s.get_attempt(m['attempt_id']).status,A.RESULT_PENDING)
        kinds=[e.kind for e in self.s.events(self.g.goal_id)]
        self.assertEqual(kinds.count('attempt.started'),1)
        self.assertEqual(kinds.count('attempt.result_produced'),1)

    def test_crash_window_07_saved_result_before_evidence_confirmation(self):
        self.activate(('A',));m=self.submit();self.wait(m)
        with patch.object(self.gh.source,'finalize',side_effect=OSError('source temporarily unavailable')):
            with self.assertRaises(OSError): self.gh.reconcile_attempt(m['attempt_id'])
        self.assertEqual(self.s.get_attempt(m['attempt_id']).status,A.RESULT_PENDING)
        self.assertFalse(self.gh._currently_usable(m))
        self.gh.reconcile_attempt(m['attempt_id'])
        self.assertTrue(self.gh._currently_usable(m))

    def test_crash_window_08_accept_committed_but_response_lost(self):
        self.activate(('A',));m,_=self.finish(accept=False)
        original=self.s.accept_attempt
        def lose(*args,**kwargs):
            original(*args,**kwargs)
            raise OSError('upper acceptance response lost')
        with patch.object(self.s,'accept_attempt',side_effect=lose):
            with self.assertRaises(OSError): self.gh.accept_result(m['attempt_id'],reason='OFFLINE confirmed',idempotency_key='lost-accept')
        revision=self.s.get_goal(self.g.goal_id).revision
        again=self.gh.accept_result(m['attempt_id'],reason='OFFLINE confirmed',idempotency_key='lost-accept')
        self.assertEqual(again.status,W.COMPLETED)
        self.assertEqual(self.s.get_goal(self.g.goal_id).revision,revision)

    def test_source_tampered_host_identity_or_new_failure_invalidates_cached_success(self):
        self.activate(('A',));m,result=self.finish(accept=False)
        from dual_agent.goal_host.identity import ids
        file=self.runs/'studio-host/requests'/(ids(m['request_id'])[0]+'.json')
        original=file.read_bytes();data=json.loads(original);data['taskId']='wrong';file.write_text(json.dumps(data))
        self.assertFalse(self.gh._currently_usable(m))
        with self.assertRaises(ConflictError): self.gh.reconcile_attempt(m['attempt_id'])
        file.write_bytes(original)
        resultfile=result.run_dir/'result.json';data=json.loads(resultfile.read_text(encoding='utf8'))
        data.update(status='failed',success=False,stop_reason='timeout');resultfile.write_text(json.dumps(data))
        self.assertFalse(self.gh._currently_usable(m))

    def test_source_never_runs_git_or_workspace_snapshot_inside_sqlite_transaction(self):
        self.activate(('A',));m,_=self.finish(accept=False)
        from dual_agent.goal_host import trusted_evidence as source
        active=[0]
        original=self.s._db.transaction
        from contextlib import contextmanager
        @contextmanager
        def transaction():
            with original() as c:
                active[0]+=1
                try: yield c
                finally: active[0]-=1
        oldgit,oldsnapshot=source._git,source._snapshot_workspace
        def checked_git(*args,**kwargs):
            self.assertEqual(active[0],0,'external Git inside SQLite write transaction')
            return oldgit(*args,**kwargs)
        def checked_snapshot(*args,**kwargs):
            self.assertEqual(active[0],0,'workspace scan inside SQLite write transaction')
            return oldsnapshot(*args,**kwargs)
        with patch.object(self.s._db,'transaction',transaction),patch.object(source,'_git',side_effect=checked_git),patch.object(source,'_snapshot_workspace',side_effect=checked_snapshot):
            self.gh.accept_result(m['attempt_id'],reason='OFFLINE performance/lock-order check',idempotency_key='lease-check')

    def test_host_fact_duplicate_is_idempotent_but_same_id_changed_fact_conflicts(self):
        self.activate(('A',));m=self.submit();self.wait(m)
        first=self.gh._event(m,'STARTED')
        revision=self.s.get_goal(self.g.goal_id).revision
        again=self.gh._event(m,'STARTED')
        self.assertEqual(first,again)
        self.assertEqual(self.s.get_goal(self.g.goal_id).revision,revision)
        with self.assertRaises(ConflictError): self.gh._event(m,'STARTED',reason='changed fact')

    def test_fresh_evidence_lease_refuses_conflicting_writer(self):
        self.activate(('A',));m,result=self.finish(accept=False)
        from dual_agent.locks import WriteLockManager
        from dual_agent.coordinator import default_lock_dir
        lock,_=WriteLockManager(default_lock_dir()).try_acquire('offline-conflicting-writer',[Path(result.workspace)])
        self.assertIsNotNone(lock)
        try:
            self.assertFalse(self.gh.verification_status(m['attempt_id'])['available'])
            with self.assertRaises(EvidenceError): self.gh.accept_result(m['attempt_id'],reason='no lock',idempotency_key='locked')
        finally: lock.release()
        self.assertTrue(self.gh.verification_status(m['attempt_id'])['available'])

    def test_taskhost_reservation_cannot_be_hijacked_by_ordinary_DTO(self):
        self.activate(('A',))
        from dual_agent.goal_host.identity import reserve
        value,work,payload=self.gh._request(self.request())
        reservation=reserve(self.host,json.dumps({**payload,'requestId':'goal-reserved-ordinary'}),list(work.allowed_write_scope))
        with self.assertRaises(BusinessError): self.host.submit(json.dumps({**payload,'requestId':reservation['request_id']}))
        self.assertEqual(len(self.host.scheduler.tasks),0)

    def test_effective_work_scope_is_intersection_with_signed_project_checks(self):
        p=plan(('A',));p['work_items'][0]['allowed_write_scope']=['scenario.txt']
        self.s.create_plan(self.g.goal_id,p,parent_revision=None,expected_revision=0,idempotency_key='plan')
        self.s.activate_plan(self.g.goal_id,1,expected_revision=1,idempotency_key='activate')
        m=self.submit();result=self.wait(m)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.stop_reason,'verified')
        self.gh.reconcile_attempt(m['attempt_id'])
        self.assertEqual(self.s.get_work_item('A').status,W.FAILED)
        self.assertFalse(self.gh.verification_status(m['attempt_id'])['available'])
        task=json.loads((result.run_dir/'task.json').read_text(encoding='utf8'))
        checks=[v['allowed_paths'] for v in task['verification'] if v['type']=='allowed_changes']
        self.assertIn(['value.txt'],checks);self.assertIn(['scenario.txt'],checks)

    def test_actual_scheduler_resource_lock_serializes_different_projects(self):
        project(self.root,self.registry,self.runs,self.key,'b','slow',['goal-b1:shared'])
        project(self.root,self.registry,self.runs,self.key,'c','slow',['goal-b1:shared'])
        self.activate(('A','B'),chain=False)
        a=self.submit('A',projectId='offline-b');b=self.submit('B',projectId='offline-c')
        time.sleep(.1)
        states=self.host.schedules('{}')['items']
        self.assertEqual(sum(v['state']=='queued' for v in states),1)
        ra=self.wait(a);rb=self.wait(b)
        self.assertEqual(ra.stop_reason,'verified');self.assertEqual(rb.stop_reason,'verified')
        self.assertEqual(self.host.scheduler.peak,1)


if __name__=='__main__': unittest.main()
