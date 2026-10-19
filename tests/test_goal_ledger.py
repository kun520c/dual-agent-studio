"""v0.15 Phase A, A01-A15. All positive evidence is OFFLINE_FIXTURE.

Fixture authority exists only in this test file. Production JSON never grants
verification authority; no real models or installed Agent CLIs are used here.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from dual_agent.goal_ledger import (GoalService, GoalStatus as G, WorkStatus as W, AttemptStatus as A,
    TrustLevel as T, VerificationState as V, RecoveryKind as R, EvidenceAssessment,
    ValidationError, ConflictError, TransitionError, EvidenceError, StorageError, StorageBusyError, SchemaError)
from dual_agent.goal_ledger.state_machine import require_transition
from dual_agent.goal_ledger.schemas import digest


class OfflineFixtureAuthority:
    """Explicit process-local fixture receipts, not a product force-verified API."""
    def __init__(self):
        self.receipts = {}

    def assess(self, attempt, relative, sha256):
        return self.receipts.get((attempt.attempt_id, relative, sha256))

    def fixture(self, attempt, relative, data):
        sha = hashlib.sha256(data).hexdigest()
        self.receipts[(attempt.attempt_id, relative, sha)] = EvidenceAssessment(
            attempt.attempt_id, attempt.run_id, sha, 'OFFLINE-TEST-SNAPSHOT', V.VALID, T.OFFLINE_FIXTURE)


def proposal(*names):
    return {'summary': '离线结构化计划', 'work_items': [
        dict(work_item_id=name, title=name, description='完整独立工程任务', dependencies=[],
             acceptance_criteria=['离线夹具完成验收'], allowed_write_scope=['src/' + name + '.py']) for name in names]}


class GoalLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='goal-ledger-中文-')
        self.root = Path(self.temp.name)
        self.source = OfflineFixtureAuthority()
        self.s = GoalService(self.root, evidence_source=self.source)
        self.g = self.s.create_goal(title='总目标', original_goal='  原始用户目标\n不可覆盖  ', constraints=['仅离线'], idempotency_key='goal')
        self.counter = 0

    def tearDown(self):
        self.s.close()
        self.temp.cleanup()

    def key(self):
        self.counter += 1
        return 'op-' + str(self.counter)

    def kw(self):
        return dict(expected_revision=self.s.get_goal(self.g.goal_id).revision, idempotency_key=self.key())

    def plan(self, data=None):
        data = data or proposal('A', 'B', 'C')
        p = self.s.create_plan(self.g.goal_id, data, parent_revision=None, **self.kw())
        self.s.activate_plan(self.g.goal_id, p.revision, **self.kw())
        return p

    def attempt(self, wid='A'):
        return self.s.create_attempt(self.g.goal_id, wid, executor='mock', run_id='task-' + wid + '/' + self.key(),
            workspace_ref='fixture-workspace', worktree_ref='fixture-tree', base_head='fixture-base', **self.kw())

    def event(self, a, name, **kwargs):
        return self.s.record_attempt_event(self.g.goal_id, a.attempt_id, generation=a.generation, event=name, **self.kw(), **kwargs)

    def pending(self, wid='A'):
        a = self.attempt(wid)
        self.event(a, 'STARTED')
        return self.event(a, 'RESULT_PRODUCED')

    def evidence(self, a, trusted=True):
        path = a.run_id + '/verification.json'
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        data = b'{"fixture":"offline-only","verified":true}'
        file.write_bytes(data)
        if trusted: self.source.fixture(a, path, data)
        return self.s.record_evidence(self.g.goal_id, a.attempt_id, generation=a.generation, evidence_path=path, **self.kw())

    def decision(self, a, refs=(), **overrides):
        p = dict(plan_revision=self.s.get_goal(self.g.goal_id).active_plan_revision,
            work_item_id=a.work_item_id, attempt_id=a.attempt_id, action='ACCEPT', reason='测试决策提案', evidence_ids=[r.evidence_id for r in refs])
        p.update(overrides)
        return self.s.record_decision(self.g.goal_id, p, **self.kw())

    def accept(self, a, d):
        return self.s.accept_attempt(self.g.goal_id, a.attempt_id, generation=a.generation, decision_id=d.decision_id, **self.kw())

    def complete(self, wid='A'):
        a = self.pending(wid)
        e = self.evidence(a)
        self.accept(a, self.decision(a, [e]))
        return a, e

    def test_A01_reopen_preserves_goal_original_input_revision_and_unicode(self):
        before = self.s.get_goal(self.g.goal_id)
        path = self.s.database_path
        self.s.close()
        with GoalService(self.root) as reopened:
            self.assertEqual(reopened.get_goal(before.goal_id), before)
            self.assertEqual(before.original_goal, '  原始用户目标\n不可覆盖  ')
            self.assertEqual(len(reopened.events(before.goal_id)), 1)
            self.assertEqual(path, reopened.database_path)
        with self.assertRaises(TransitionError): reopened.get_goal(before.goal_id)

    def test_A02_repeat_init_wal_foreign_keys_and_schema(self):
        with GoalService(self.root) as reopened:
            self.assertEqual(reopened.get_goal(self.g.goal_id), self.g)
            self.assertEqual(reopened.check_integrity(), [])
        with self.s._db.read() as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 4)

    def test_A02_unknown_future_schema_rejected_without_overwrite(self):
        with closing(sqlite3.connect(self.s.database_path)) as c: c.execute('PRAGMA user_version=999')
        with self.assertRaises(SchemaError): GoalService(self.root)
        with closing(sqlite3.connect(self.s.database_path)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 999)

    def test_A02_unversioned_nonempty_database_rejected(self):
        other = self.root / 'other'
        path = other / 'studio-host/goal-ledger/ledger.sqlite3'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as c: c.execute('CREATE TABLE unknown(x)')
        with self.assertRaises(SchemaError): GoalService(other)

    def test_A03_rollback_event_projection_and_idempotency_together(self):
        before = self.s.get_goal(self.g.goal_id)
        original = self.s._event
        def fail_after_event(*args):
            original(*args)
            raise RuntimeError('injected failure after event insertion')
        with patch.object(self.s, '_event', side_effect=fail_after_event):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.s.create_plan(self.g.goal_id, proposal('A'), parent_revision=None, expected_revision=0, idempotency_key='rollback')
        self.assertEqual(self.s.get_goal(before.goal_id), before)
        self.assertEqual(len(self.s.events(before.goal_id)), 1)
        p = self.s.create_plan(before.goal_id, proposal('A'), parent_revision=None, expected_revision=0, idempotency_key='rollback')
        self.assertEqual(p.revision, 1)

    def test_A04_plan_immutable_parent_revision_stale_writes_and_history(self):
        p1 = self.plan(proposal('A'))
        p2 = self.s.create_plan(self.g.goal_id, proposal('D'), parent_revision=1, **self.kw())
        self.assertEqual(p2.parent_revision, 1)
        self.assertEqual(p1, self.s.get_plan(self.g.goal_id, 1))
        with self.assertRaises(ConflictError): self.s.create_plan(self.g.goal_id, proposal('E'), parent_revision=1, **self.kw())
        with self.assertRaises(ConflictError): self.s.activate_plan(self.g.goal_id, 1, **self.kw())
        self.s.activate_plan(self.g.goal_id, 2, **self.kw())
        self.assertEqual(self.s.get_work_item('A').status, W.SUPERSEDED)
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)], ['D'])
        self.assertEqual(p1.content_hash, digest(p1.plan_content))
        with self.assertRaises(StorageError):
            with self.s._db.transaction() as c: c.execute('UPDATE plans SET data=?', ('{}',))

    def test_A04_replanning_cannot_discard_live_or_uncertain_attempt(self):
        self.plan(proposal('A'))
        a = self.attempt()
        self.s.create_plan(self.g.goal_id, proposal('D'), parent_revision=1, **self.kw())
        with self.assertRaises(ConflictError): self.s.activate_plan(self.g.goal_id, 2, **self.kw())
        self.assertEqual(self.s.get_attempt(a.attempt_id), a)

    def test_A05_cycle_unknown_duplicate_and_self_dependencies_rejected(self):
        cases = []
        p = proposal('A', 'B'); p['work_items'][0]['dependencies'] = ['B']; p['work_items'][1]['dependencies'] = ['A']; cases.append(p)
        p = proposal('A'); p['work_items'][0]['dependencies'] = ['missing']; cases.append(p)
        cases.append(proposal('A', 'A'))
        p = proposal('A'); p['work_items'][0]['dependencies'] = ['A']; cases.append(p)
        for p in cases:
            with self.subTest(p=p), self.assertRaises(ValidationError):
                self.s.create_plan(self.g.goal_id, p, parent_revision=None, **self.kw())
        self.assertEqual(self.s.get_goal(self.g.goal_id).revision, 0)

    def test_A05_cross_version_goal_and_id_reuse_rejected(self):
        self.plan(proposal('A'))
        p = proposal('D'); p['work_items'][0]['dependencies'] = ['A']
        with self.assertRaises(ValidationError): self.s.create_plan(self.g.goal_id, p, parent_revision=1, **self.kw())
        with self.assertRaises(ConflictError): self.s.create_plan(self.g.goal_id, proposal('A'), parent_revision=1, **self.kw())
        other = self.s.create_goal(title='other', original_goal='other', idempotency_key='other')
        with self.assertRaises(ValidationError):
            self.s.create_plan(other.goal_id, p, parent_revision=None, expected_revision=0, idempotency_key='cross')

    def test_A06_chain_dependencies_use_explicit_offline_fixture_only(self):
        p = proposal('A', 'B', 'C')
        p['work_items'][1]['dependencies'] = ['A']; p['work_items'][2]['dependencies'] = ['B']
        self.plan(p)
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)], ['A'])
        with self.assertRaises(TransitionError): self.attempt('B')
        self.complete('A')
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)], ['B'])
        self.complete('B')
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)], ['C'])
        self.complete('C')
        state = self.s.current_state(self.g.goal_id)
        self.assertEqual(state['goal']['status'], G.COMPLETED)
        self.assertTrue(state['completion_currently_usable'])
        self.assertEqual(state['evidence_trust_levels'], ['OFFLINE_FIXTURE'])

    def test_A06_stable_capacity_and_known_conflict_exclusions(self):
        self.plan(proposal('C', 'A', 'B'))
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id, available_capacity=2)], ['C', 'A'])
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id, conflicts=['C'])], ['A'])
        self.assertEqual(self.s.ready_work_items(self.g.goal_id, available_capacity=0), [])
        with self.assertRaises(ValidationError): self.s.ready_work_items(self.g.goal_id, available_capacity=3)

    def test_A07_goal_plan_attempt_and_event_idempotency(self):
        self.assertEqual(self.s.create_goal(title=self.g.title, original_goal=self.g.original_goal, constraints=self.g.constraints, idempotency_key='goal'), self.g)
        with self.assertRaises(ConflictError): self.s.create_goal(title='different', original_goal=self.g.original_goal, constraints=self.g.constraints, idempotency_key='goal')
        args = dict(parent_revision=None, expected_revision=0, idempotency_key='plan')
        p = self.s.create_plan(self.g.goal_id, proposal('A'), **args)
        self.assertEqual(self.s.create_plan(self.g.goal_id, proposal('A'), **args), p)
        self.s.activate_plan(self.g.goal_id, 1, **self.kw())
        args = dict(executor='mock', run_id='task/run', workspace_ref='w', worktree_ref='wt', base_head='base', **self.kw())
        a = self.s.create_attempt(self.g.goal_id, 'A', **args)
        self.assertEqual(self.s.create_attempt(self.g.goal_id, 'A', **args), a)
        args = dict(generation=a.generation, event='STARTED', **self.kw())
        started = self.s.record_attempt_event(self.g.goal_id, a.attempt_id, **args)
        self.assertEqual(self.s.record_attempt_event(self.g.goal_id, a.attempt_id, **args), started)
        self.assertEqual(len(self.s.list_attempts('A')), 1)
        self.assertEqual(len(self.s.events(self.g.goal_id)), 5)

    def test_A08_two_connections_same_revision_only_one_plan_writer(self):
        def write(i):
            with GoalService(self.root) as service:
                try:
                    return service.create_plan(self.g.goal_id, proposal('work-' + str(i)), parent_revision=None,
                        expected_revision=0, idempotency_key='parallel-' + str(i)).revision
                except ConflictError: return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(write, [1, 2]))
        self.assertCountEqual(results, [1, 'conflict'])
        self.assertEqual(self.s.get_goal(self.g.goal_id).revision, 1)

    def test_A08_two_claimants_only_one_current_attempt(self):
        self.plan(proposal('A'))
        revision = self.s.get_goal(self.g.goal_id).revision
        def claim(i):
            with GoalService(self.root) as service:
                try:
                    return service.create_attempt(self.g.goal_id, 'A', executor='mock', run_id='task/run-' + str(i),
                        workspace_ref='w', worktree_ref='wt', base_head='base', expected_revision=revision, idempotency_key='claim-' + str(i)).generation
                except ConflictError: return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(claim, [1, 2]))
        self.assertCountEqual(results, [1, 'conflict'])
        self.assertEqual(len(self.s.list_attempts('A')), 1)

    def test_A08_independent_windows_processes_conflict_on_same_goal_revision(self):
        code = """import sys,time
from pathlib import Path
from dual_agent.goal_ledger import GoalService,ConflictError
root,gid,tag=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
with GoalService(root) as s:
 (root/('ready-'+tag)).write_text('ready')
 while not (root/'go').exists(): time.sleep(.01)
 try:
  s.set_goal_blocked(gid,blocked=True,reason='concurrent process '+tag,expected_revision=0,idempotency_key='process-'+tag)
  print('written')
 except ConflictError: print('conflict')
"""
        processes = [subprocess.Popen([sys.executable, '-B', '-c', code, str(self.root), self.g.goal_id, str(i)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in [1, 2]]
        try:
            deadline = time.monotonic() + 15
            while not all((self.root / ('ready-' + str(i))).exists() for i in [1, 2]) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(all((self.root / ('ready-' + str(i))).exists() for i in [1, 2]))
            (self.root / 'go').write_text('go')
            outputs = []
            for process in processes:
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, err)
                outputs.append(out.decode().strip())
            self.assertCountEqual(outputs, ['written', 'conflict'])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.communicate()

    def test_A02_failed_schema_install_is_atomic_and_retryable(self):
        from dual_agent.goal_ledger import migrations
        other = self.root / 'migration-failure'
        with patch.object(migrations, 'DDL', migrations.DDL[:3] + ['THIS IS NOT SQL']):
            with self.assertRaises(StorageError): GoalService(other)
        with GoalService(other) as retry:
            self.assertEqual(retry.check_integrity(), [])
            self.assertEqual(retry.create_goal(title='after failure', original_goal='preserved', idempotency_key='reinit').revision, 0)

    def test_A03_foreign_key_violation_rolls_back_and_preserves_integrity(self):
        self.plan(proposal('A'))
        with self.assertRaises(StorageError):
            with self.s._db.transaction() as c:
                c.execute('INSERT INTO dependencies VALUES(?,?,?,?)', (self.g.goal_id, 1, 'A', 'missing'))
        self.assertEqual(self.s.check_integrity(), [])

    def test_A10_transitive_ancestor_expiry_blocks_later_descendant(self):
        p = proposal('A', 'B', 'C'); p['work_items'][1]['dependencies'] = ['A']; p['work_items'][2]['dependencies'] = ['B']
        self.plan(p)
        _, ancestor = self.complete('A'); _, intermediate = self.complete('B')
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)], ['C'])
        for key in list(self.source.receipts):
            if key[0] == ancestor.attempt_id: del self.source.receipts[key]
        self.assertTrue(self.s.evidence_usable(intermediate.evidence_id))
        self.assertEqual(self.s.ready_work_items(self.g.goal_id), [])
        with self.assertRaises(TransitionError): self.attempt('C')
        self.assertFalse(self.s.evidence_usable(ancestor.evidence_id))

    def test_A10_snapshot_expiry_and_source_failure_fail_closed(self):
        self.plan(proposal('A')); a = self.pending(); e = self.evidence(a)
        receipt_key = (a.attempt_id, e.evidence_path, e.sha256)
        self.source.receipts[receipt_key] = replace(self.source.receipts[receipt_key], head_or_snapshot_ref='changed workspace snapshot')
        self.assertFalse(self.s.evidence_usable(e.evidence_id))
        with patch.object(self.source, 'assess', side_effect=RuntimeError('source offline')):
            self.assertFalse(self.s.evidence_usable(e.evidence_id))
            with self.assertRaises(EvidenceError): self.accept(a, self.decision(a, [e]))

    def test_A10_decision_cannot_cite_other_attempt_evidence_or_stale_plan(self):
        self.plan(proposal('A', 'B'))
        a = self.pending('A'); e = self.evidence(a); b = self.pending('B')
        with self.assertRaises(EvidenceError): self.decision(b, [e])
        with self.assertRaises(ConflictError): self.decision(a, [e], plan_revision=99)

    def test_A11_consistency_checks_detect_projection_and_event_mismatch(self):
        self.plan(proposal('A')); a = self.attempt()
        with self.s._db.transaction() as c: c.execute('UPDATE work_items SET status=? WHERE work_item_id=?', ('RUNNING', 'A'))
        self.assertEqual(self.s.recovery_checks(self.g.goal_id)[0].kind, R.BLOCKED)
        with self.s._db.transaction() as c: c.execute('UPDATE goals SET revision=revision+1 WHERE goal_id=?', (self.g.goal_id,))
        self.assertEqual(self.s.recovery_checks(self.g.goal_id)[0].work_item_id, '')

    def test_A13_cancel_single_work_does_not_cancel_independent_sibling(self):
        self.plan(proposal('A', 'B')); a = self.attempt()
        self.s.cancel_work_item(self.g.goal_id, 'A', reason='cancel only A', **self.kw())
        self.assertEqual(self.s.get_work_item('A').status, W.CANCELLED)
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)], ['B'])
        self.assertEqual(self.s.get_attempt(a.attempt_id).status, A.NEEDS_RECONCILIATION)
        with self.assertRaises(TransitionError): self.event(a, 'RESULT_PRODUCED')

    def test_A15_connection_handles_allow_windows_rename_after_close(self):
        path = self.s.database_path
        self.s.close()
        moved = path.with_name('renamed.sqlite3')
        path.rename(moved); moved.rename(path)
        with GoalService(self.root) as reopened:
            self.assertEqual(reopened.get_goal(self.g.goal_id), self.g)

    def test_A07_evidence_decision_and_acceptance_replay_do_not_advance_twice(self):
        self.plan(proposal('A')); a = self.pending()
        path = a.run_id + '/verification.json'; file = self.root / path
        file.parent.mkdir(parents=True); data = b'{"offline-fixture":true}'; file.write_bytes(data)
        self.source.fixture(a, path, data)
        kwargs = dict(generation=a.generation, evidence_path=path, **self.kw())
        e = self.s.record_evidence(self.g.goal_id, a.attempt_id, **kwargs)
        self.assertEqual(self.s.record_evidence(self.g.goal_id, a.attempt_id, **kwargs), e)
        p = dict(plan_revision=1, action='ACCEPT', reason='offline-only', evidence_ids=[e.evidence_id], work_item_id='A', attempt_id=a.attempt_id)
        kwargs = self.kw()
        d = self.s.record_decision(self.g.goal_id, p, **kwargs)
        self.assertEqual(self.s.record_decision(self.g.goal_id, p, **kwargs), d)
        kwargs = dict(generation=a.generation, decision_id=d.decision_id, **self.kw())
        w = self.s.accept_attempt(self.g.goal_id, a.attempt_id, **kwargs)
        revision = self.s.get_goal(self.g.goal_id).revision
        self.assertEqual(self.s.accept_attempt(self.g.goal_id, a.attempt_id, **kwargs), w)
        self.assertEqual(self.s.get_goal(self.g.goal_id).revision, revision)

    def test_A09_old_attempt_late_success_and_failure_are_fenced(self):
        self.plan(proposal('A'))
        old = self.attempt()
        self.event(old, 'FAILED', reason='offline failure')
        self.s.retry_work_item(self.g.goal_id, 'A', reconciliation_note='offline process confirmed stopped', **self.kw())
        new = self.attempt()
        self.assertEqual(new.generation, old.generation + 1)
        for event in ['RESULT_PRODUCED', 'FAILED']:
            with self.assertRaises(ConflictError): self.event(old, event, reason='late')
        self.assertEqual(self.s.get_work_item('A').current_attempt_id, new.attempt_id)
        self.assertEqual(self.s.get_attempt(old.attempt_id).status, A.FAILED)
        self.assertEqual(len(self.s.list_attempts('A')), 2)

    def test_A10_model_verified_field_and_accept_alone_cannot_complete(self):
        self.plan(proposal('A'))
        a = self.pending()
        d = self.decision(a)
        with self.assertRaises(EvidenceError): self.accept(a, d)
        e = self.evidence(a, trusted=False)
        self.assertEqual(e.trust_level, T.UNTRUSTED)
        self.assertEqual(e.verification_state, V.RECORDED)
        with self.assertRaises(EvidenceError): self.accept(a, self.decision(a, [e]))
        with self.assertRaises(ValidationError): self.decision(a, verified=True)
        self.assertEqual(self.s.get_work_item('A').status, W.AWAITING_CONFIRMATION)

    def test_A10_assessment_bound_to_exact_attempt_run_hash_and_typed_source(self):
        self.plan(proposal('A'))
        a = self.pending()
        path = a.run_id + '/verification.json'; file = self.root / path
        file.parent.mkdir(parents=True); file.write_bytes(b'{}')
        sha = hashlib.sha256(b'{}').hexdigest()
        for result in [dict(verified=True), EvidenceAssessment('wrong', a.run_id, sha, 'snapshot', V.VALID, T.COORDINATOR),
                       EvidenceAssessment(a.attempt_id, a.run_id, sha, 'snapshot', V.VALID, T.UNTRUSTED)]:
            with self.subTest(result=result), patch.object(self.source, 'assess', return_value=result), self.assertRaises(EvidenceError):
                self.s.record_evidence(self.g.goal_id, a.attempt_id, generation=a.generation, evidence_path=path, **self.kw())

    def test_A10_changed_bytes_or_source_snapshot_invalidate_current_acceptance(self):
        self.plan(proposal('A'))
        a = self.pending(); e = self.evidence(a)
        d = self.decision(a, [e])
        (self.root / e.evidence_path).write_bytes(b'{"changed":true}')
        self.assertFalse(self.s.evidence_usable(e.evidence_id))
        with self.assertRaises(EvidenceError): self.accept(a, d)
        self.assertEqual(self.s.get_evidence(e.evidence_id), e)

    def test_A10_stale_manager_decision_rejected_even_with_fresh_call_revision(self):
        self.plan(proposal('A'))
        a = self.pending(); e = self.evidence(a); d = self.decision(a, [e])
        self.decision(a, [e], action='ESCALATE')
        with self.assertRaises(ConflictError): self.accept(a, d)

    def test_A10_reopen_without_authority_never_adopts_saved_verified_files(self):
        self.plan(proposal('A'))
        self.complete()
        with GoalService(self.root) as reopened:
            state = reopened.current_state(self.g.goal_id)
            self.assertFalse(state['completion_currently_usable'])
            self.assertEqual(state['recovery'][0]['kind'], R.BLOCKED)

    def test_A10_invalidation_retains_history_and_blocks_dependency_readiness(self):
        p = proposal('A', 'B'); p['work_items'][1]['dependencies'] = ['A']; self.plan(p)
        a, e = self.complete()
        self.assertEqual([w.work_item_id for w in self.s.ready_work_items(self.g.goal_id)], ['B'])
        invalid = self.s.invalidate_evidence(self.g.goal_id, e.evidence_id, reason='snapshot no longer current', **self.kw())
        self.assertEqual(invalid.verification_state, V.INVALIDATED)
        self.assertEqual(invalid.sha256, e.sha256)
        self.assertEqual(self.s.ready_work_items(self.g.goal_id), [])
        self.assertEqual(self.s.get_attempt(a.attempt_id).status, A.SUCCEEDED)

    def test_A11_recovery_saved_dispatch_running_result_and_cancel_are_distinct(self):
        self.plan(proposal('A', 'B', 'C', 'D', 'E'))
        self.attempt('B')
        c = self.attempt('C'); self.event(c, 'STARTED')
        self.pending('D')
        e = self.attempt('E'); self.event(e, 'FAILED', reason='offline failure')
        with GoalService(self.root) as reopened:
            checks = {r.work_item_id: r.kind for r in reopened.recovery_checks(self.g.goal_id)}
        self.assertEqual(checks, dict(A=R.SAFE_TO_RESUME, B=R.UNKNOWN_SIDE_EFFECT, C=R.NEEDS_RECONCILIATION, D=R.NEEDS_RECONCILIATION, E=R.TERMINAL))
        with self.assertRaises(TransitionError): self.attempt('B')

    def test_A11_killed_process_uncommitted_transaction_rolls_back(self):
        script = "import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE goals SET title=?',('uncommitted',)); os._exit(23)"
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.s.database_path)], capture_output=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        with GoalService(self.root) as reopened:
            self.assertEqual(reopened.get_goal(self.g.goal_id).title, self.g.title)
            self.assertEqual(reopened.check_integrity(), [])

    def test_A11_committed_dispatch_survives_abrupt_process_exit(self):
        self.plan(proposal('A'))
        code = """import os,sys
from pathlib import Path
from dual_agent.goal_ledger import GoalService
s=GoalService(Path(sys.argv[1]))
s.create_attempt(sys.argv[2],'A',executor='mock',run_id='task/crashed',workspace_ref='w',worktree_ref='wt',base_head='base',expected_revision=2,idempotency_key='crash-intent')
os._exit(24)
"""
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.root), self.g.goal_id], cwd=Path(__file__).resolve().parents[1], capture_output=True)
        self.assertEqual(result.returncode, 24, result.stderr)
        self.assertEqual(self.s.recovery_checks(self.g.goal_id)[0].kind, R.UNKNOWN_SIDE_EFFECT)
        self.assertEqual(len(self.s.list_attempts('A')), 1)

    def test_A11_explicit_reconciliation_required_before_retry(self):
        self.plan(proposal('A'))
        a = self.attempt()
        self.s.block_work_item(self.g.goal_id, 'A', reason='startup ambiguous', **self.kw())
        self.assertEqual(self.s.get_attempt(a.attempt_id).status, A.NEEDS_RECONCILIATION)
        with self.assertRaises(TransitionError): self.event(a, 'STARTED')
        self.s.retry_work_item(self.g.goal_id, 'A', reconciliation_note='operator confirmed no writer remains', **self.kw())
        self.assertEqual(self.s.get_attempt(a.attempt_id).status, A.INTERRUPTED)
        self.assertEqual(self.attempt().generation, 2)

    def test_A12_absolute_traversal_unc_ads_reserved_and_cross_run_paths_rejected(self):
        self.plan(proposal('A')); a = self.pending()
        for path in ['C:/Windows/verification.json', '/verification.json', '../verification.json', 'task/../verification.json',
                     '//server/share/file', 'task/run/verification.json:stream', 'task/run/NUL', 'other/run/verification.json', 'task/run/auth.token']:
            with self.subTest(path=path), self.assertRaises((ValidationError, EvidenceError)):
                self.s.record_evidence(self.g.goal_id, a.attempt_id, generation=a.generation, evidence_path=path, **self.kw())

    def test_A12_plan_dto_rejects_authority_status_and_unsafe_write_scopes(self):
        for field in ['verified', 'execution_authority', 'database_path', 'status']:
            p = proposal('A'); p[field] = True
            with self.assertRaises(ValidationError): self.s.create_plan(self.g.goal_id, p, parent_revision=None, **self.kw())
        for scope in ['C:/outside', '../outside', '\\outside', 'src/../../outside']:
            p = proposal('A'); p['work_items'][0]['allowed_write_scope'] = [scope]
            with self.assertRaises(ValidationError): self.s.create_plan(self.g.goal_id, p, parent_revision=None, **self.kw())

    def test_A12_windows_junction_evidence_and_database_paths_rejected(self):
        self.plan(proposal('A')); a = self.pending()
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'verification.json').write_text('{}')
        link = self.root / a.run_id
        link.parent.mkdir(parents=True)
        if os.name == 'nt':
            # Only creates a junction in this test's temporary directory.
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else: link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(EvidenceError):
                self.s.record_evidence(self.g.goal_id, a.attempt_id, generation=a.generation, evidence_path=a.run_id + '/verification.json', **self.kw())
            with self.assertRaises(StorageError): GoalService(link)
        finally:
            if os.name == 'nt': link.rmdir()
            else: link.unlink()

    def test_A13_cancelled_goal_cannot_be_reactivated_and_unknown_effects_preserved(self):
        self.plan(proposal('A')); a = self.attempt()
        self.s.cancel_goal(self.g.goal_id, reason='user cancelled', **self.kw())
        self.assertEqual(self.s.get_goal(self.g.goal_id).status, G.CANCELLED)
        self.assertEqual(self.s.ready_work_items(self.g.goal_id), [])
        with self.assertRaises(TransitionError): self.event(a, 'STARTED')
        with self.assertRaises(TransitionError): self.s.retry_work_item(self.g.goal_id, 'A', reconciliation_note='old retry', **self.kw())
        self.assertEqual(self.s.recovery_checks(self.g.goal_id)[0].kind, R.NEEDS_RECONCILIATION)

    def test_A13_completed_goal_and_work_are_terminal(self):
        self.plan(proposal('A')); a, _ = self.complete()
        with self.assertRaises(TransitionError): self.event(a, 'FAILED', reason='late failure')
        with self.assertRaises(TransitionError): self.s.cancel_goal(self.g.goal_id, reason='late cancel', **self.kw())
        self.assertEqual(self.s.get_work_item('A').status, W.COMPLETED)

    def test_A13_goal_block_requires_explicit_unblock(self):
        self.plan(proposal('A'))
        self.s.set_goal_blocked(self.g.goal_id, blocked=True, reason='review required', **self.kw())
        self.assertEqual(self.s.ready_work_items(self.g.goal_id), [])
        with self.assertRaises(TransitionError): self.attempt()
        self.s.set_goal_blocked(self.g.goal_id, blocked=False, reason='review resolved', **self.kw())
        self.assertEqual(len(self.s.ready_work_items(self.g.goal_id)), 1)

    def test_A14_task_policy_adapter_only_uses_configured_runs_and_no_executor_import(self):
        class Policy:
            runs = self.root
        with GoalService.for_task_policy(Policy()) as adapted:
            self.assertEqual(adapted.get_goal(self.g.goal_id), self.s.get_goal(self.g.goal_id))
            self.assertEqual(adapted.database_path, self.root / 'studio-host/goal-ledger/ledger.sqlite3')
        # A phase A operation never calls the existing scheduler/authority/executor.
        with patch('dual_agent.scheduler.TaskScheduler.submit', side_effect=AssertionError('must not dispatch')):
            self.plan(proposal('A')); self.attempt()

    def test_A15_lock_contention_is_bounded_and_connections_release(self):
        with GoalService(self.root, busy_timeout_ms=30) as contender:
            with self.s._db.transaction():
                with self.assertRaisesRegex(StorageBusyError, 'locked'):
                    contender.create_goal(title='busy', original_goal='busy', idempotency_key='busy')
            g = contender.create_goal(title='busy', original_goal='busy', idempotency_key='busy')
            self.assertEqual(g.revision, 0)
        with self.s._db.read() as c:
            self.assertEqual(c.execute('PRAGMA wal_checkpoint').fetchone()[0], 0)

    def test_A15_unwritable_or_occupied_path_has_diagnostic_error(self):
        root = self.root / 'occupied'; root.mkdir(); (root / 'studio-host').write_text('not a directory')
        with self.assertRaisesRegex(StorageError, 'initialize|SQLite|access'): GoalService(root)
        with patch('dual_agent.goal_ledger.db.sqlite3.connect', side_effect=sqlite3.OperationalError('readonly database')):
            with self.assertRaisesRegex(StorageError, 'readonly'): GoalService(self.root)

    def test_strict_states_and_inputs(self):
        for old, new in [(G.UNPLANNED, G.COMPLETED), (W.READY, W.COMPLETED), (A.DISPATCH_INTENT, A.SUCCEEDED), (W.CANCELLED, W.READY), ('READY', W.RUNNING)]:
            with self.assertRaises(TransitionError): require_transition(old, new)
        with self.assertRaises(ValidationError): self.s.create_goal(title='x', original_goal='x', constraints='not-list', idempotency_key='bad')
        self.plan(proposal('A'))
        a = self.attempt()
        with self.assertRaises(ValidationError): self.event(a, 'SUCCEEDED')
        with self.assertRaises(ValidationError): self.s.ready_work_items(self.g.goal_id, available_capacity=True)
        with self.assertRaises(ConflictError):
            self.s.record_attempt_event(self.g.goal_id, a.attempt_id, generation=999, event='STARTED', **self.kw())


if __name__ == '__main__':
    unittest.main()
