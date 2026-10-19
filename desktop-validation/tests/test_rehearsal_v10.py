"""v0.10-P1 onboarding and the fixed mock rehearsal. No real model calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_task_api as task_tests
from desktop_validation.rehearsal import SAMPLE_ID
from desktop_validation.task_api import BusinessError, task_request
from dual_agent.projects import show_project


class RehearsalTests(unittest.TestCase):
    setUp = task_tests.ApiTests.setUp
    tearDown = task_tests.ApiTests.tearDown
    request = task_tests.ApiTests.request
    start = task_tests.ApiTests.start
    wait = task_tests.ApiTests.wait

    def rehearsal_request(self, template='verified', execution='mock'):
        return {'projectId': SAMPLE_ID, 'title': '安全演练', 'goal': '把样本值写成 5，并完成可信验收。请不要执行 powershell -Command Remove-Item。',
                'templateId': template if execution == 'mock' else 'collaborate', 'requestId': 'rehearsal-request', 'executionMode': execution}

    def test_rehearsal_is_created_without_touching_policy_or_user_json(self):
        before = self.policy.read_bytes()
        (self.runs / 'allowlist.json').write_text(json.dumps({'projects': {'evil': {'sha256': '0' * 64}}}), encoding='utf8')
        created = self.bridge.projects.create_rehearsal('{}')
        self.assertEqual(before, self.policy.read_bytes())
        self.assertTrue(created['execution']['allowed'])
        self.assertIn('mock_executable', created['posture']['states'])
        self.assertNotIn('real_authorized', created['posture']['states'])
        self.assertFalse(created['posture']['executionGrantedByUi'])
        self.assertTrue(created['rehearsal']['policyUntouched'])
        self.assertTrue(str(created['rehearsal']['workspace']).endswith(str(Path('studio-samples') / SAMPLE_ID / 'workspace')))
        with self.assertRaises(BusinessError):
            self.rules.project('evil')
        again = self.bridge.projects.create_rehearsal('{}')
        self.assertFalse(again['rehearsal']['created'])
        self.assertFalse(json.loads(self.bridge.createRehearsal('{"reset":false,"workspace":"C:\\\\Windows"}'))['ok'])

    def test_template_drift_policy_edit_and_foreign_workspace_stay_blocked(self):
        self.bridge.projects.create_rehearsal('{}')
        user = self.root / 'user-repo'
        user.mkdir()
        (user / 'secret.txt').write_text('keep', encoding='utf8')
        registered = self.registry / (SAMPLE_ID + '.json')
        payload = json.loads(registered.read_text(encoding='utf8'))
        payload['workspace'] = str(user)
        payload['allowed_roots'] = [str(user)]
        payload['verification'][1]['argv'] = ['cmd', '/c', 'whoami']
        registered.write_text(json.dumps(payload), encoding='utf8')
        manifest = json.loads(self.policy.read_text(encoding='utf8'))
        manifest['projects'][SAMPLE_ID] = {'sha256': hashlib.sha256(registered.read_bytes()).hexdigest()}
        self.policy.write_text(json.dumps(manifest), encoding='utf8')
        with patch('subprocess.run', side_effect=AssertionError('drift must not execute')):
            with self.assertRaises(BusinessError) as caught:
                self.rules.project(SAMPLE_ID)
        self.assertIn(caught.exception.code, {'PATH_DENIED', 'REHEARSAL_MISMATCH'})
        self.assertEqual((user / 'secret.txt').read_text(encoding='utf8'), 'keep')
        self.assertFalse((user / 'value.txt').exists())
        self.rules.project('studio-mock-a')
        report = self.bridge.projects.create_rehearsal('{}')
        self.assertTrue(report['rehearsal']['drifted'])
        self.assertFalse(report['execution']['allowed'])

    def test_junction_cannot_redirect_the_sample(self):
        user = self.root / 'outside-repo'
        user.mkdir()
        (user / 'secret.txt').write_text('keep', encoding='utf8')
        parent = self.root / 'studio-samples' / SAMPLE_ID
        parent.mkdir(parents=True)
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(parent / 'workspace'), str(user)], capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        with self.assertRaises(BusinessError) as caught:
            self.bridge.projects.create_rehearsal('{}')
        self.assertEqual(caught.exception.code, 'PATH_DENIED')
        self.assertEqual((user / 'secret.txt').read_text(encoding='utf8'), 'keep')
        self.assertFalse((user / 'value.txt').exists())
        self.assertFalse((self.registry / (SAMPLE_ID + '.json')).exists())

    def test_goal_text_cannot_become_a_command_and_real_cli_stays_closed(self):
        self.bridge.projects.create_rehearsal('{}')
        value = self.rehearsal_request()
        config = self.rules.build(task_request(json.dumps(value), True), 'studio-validation')
        blob = json.dumps([item.to_dict() for item in config.verification], ensure_ascii=False)
        self.assertNotIn('powershell', blob.lower())
        self.assertNotIn('Remove-Item', blob)
        self.assertEqual(config.mock_plan, ['studio_write', 'ok', 'accept'])
        self.assertFalse(config.allow_real_codex or config.allow_real_cursor)
        self.assertTrue(all(item.executor == 'mock' for item in config.participants))
        for execution in ('real', 'offline_cli'):
            denied = self.rehearsal_request(execution=execution)
            with patch.object(self.client, 'connect', side_effect=AssertionError('must not start a CLI')):
                result = json.loads(self.bridge.submitTask(json.dumps(denied)))
            self.assertFalse(result['ok'], execution)

    def test_rehearsal_mock_run_verifies_and_cancel_does_not(self):
        self.bridge.projects.create_rehearsal('{}')
        host = self.start()
        task = host.submit(json.dumps(self.rehearsal_request()))
        status = self.wait(task)
        self.assertEqual(status['state'], 'verified', status)
        self.assertEqual(status['stop_reason'], 'verified')
        self.assertTrue(status['success'])
        self.assertEqual(status.get('mode'), 'mock')
        record = self.store.list_tasks()['items'][0]
        self.assertEqual(record['callKind'], 'mock')
        self.assertEqual(record['modelCalls'], 0)
        self.assertEqual(record['executionMode'], 'mock')
        self.assertTrue(record['outcome']['passed'])
        text = (self.root / 'studio-samples' / SAMPLE_ID / 'workspace' / 'value.txt').read_text(encoding='utf8')
        self.assertTrue(text.startswith('5\n任务：studio-'))
        delayed = self.rehearsal_request('delayed')
        delayed['requestId'] = 'rehearsal-cancel'
        running = host.submit(json.dumps(delayed))
        host.cancel(json.dumps({'scheduleId': running['scheduleId']}))
        cancelled = self.wait(running)
        self.assertNotEqual(cancelled['state'], 'verified')
        self.assertFalse(cancelled.get('success'))

    def test_path_preview_duplicate_and_retire_keep_history(self):
        preview = self.bridge.projects.inspect_workspace(json.dumps({'workspace': str(self.root / 'missing-workspace')}))
        self.assertFalse(preview['accepted'])
        self.assertFalse(preview['executionGranted'])
        known = show_project('studio-mock-a', self.registry)
        duplicate = self.bridge.projects.inspect_workspace(json.dumps({'workspace': known.workspace, 'projectId': 'other-id', 'create': True}))
        self.assertFalse(duplicate['accepted'])
        self.assertEqual(duplicate['duplicate'], 'studio-mock-a')
        blocked = self.bridge.projects.inspect_workspace(json.dumps({'workspace': r'C:\example\ai-interview-system'}))
        self.assertFalse(blocked['accepted'])
        self.assertFalse(blocked['executionGranted'])
        self.bridge.projects.create_rehearsal('{}')
        workspace = self.root / 'studio-samples' / SAMPLE_ID / 'workspace'
        (workspace / 'keep.txt').write_text('source', encoding='utf8')
        run = self.runs / 'studio-rehearsal-history' / 'saved'
        run.mkdir(parents=True)
        (run / 'task.json').write_text(json.dumps({'task_id': 'studio-rehearsal-history', 'project_id': SAMPLE_ID, 'title': '保留的历史'}), encoding='utf8')
        current = self.bridge.projects.get(json.dumps({'projectId': SAMPLE_ID}))
        retired = self.bridge.projects.retire(json.dumps({'projectId': SAMPLE_ID, 'revision': current['revision'], 'confirm': 'retire-registration-only'}))
        self.assertTrue(retired['retired'])
        self.assertFalse(retired['sourceDeleted'])
        self.assertFalse((self.registry / (SAMPLE_ID + '.json')).exists())
        self.assertEqual((workspace / 'keep.txt').read_text(encoding='utf8'), 'source')
        self.assertTrue((run / 'task.json').is_file())
        with self.assertRaises(BusinessError):
            self.bridge.projects.retire(json.dumps({'projectId': 'studio-mock-a', 'revision': '0' * 64, 'confirm': 'delete-everything'}))
        self.assertTrue((self.registry / 'studio-mock-a.json').is_file())


if __name__ == '__main__':
    unittest.main()
