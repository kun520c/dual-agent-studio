"""v0.4-B registry, DTO, history and real coordinator regressions."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_task_api as task_tests
from desktop_validation.task_api import BusinessError, task_request
from desktop_validation.bridge import MANAGEMENT_METHODS, METHODS, BUSINESS_METHODS
from desktop_validation.security import RestrictedTransport
from test_bridge import FakePage


class StudioTests(unittest.TestCase):
    setUp=task_tests.ApiTests.setUp
    tearDown=task_tests.ApiTests.tearDown
    request=task_tests.ApiTests.request
    start=task_tests.ApiTests.start
    wait=task_tests.ApiTests.wait

    def project(self, id='studio-mock-a'):
        return self.bridge.projects.get(json.dumps({'projectId':id}))

    def fresh(self):
        config=copy.deepcopy(self.project()['config'])
        workspace=self.root/'new-workspace';workspace.mkdir()
        config.update(project_id='registered-new',name='新增项目',workspace=str(workspace),allowed_roots=[str(workspace)])
        return config

    def test_register_edit_disable_enable_and_default(self):
        config=self.fresh()
        with patch('desktop_validation.project_api.register_project', wraps=__import__('dual_agent.projects',fromlist=['register_project']).register_project) as backend:
            saved=self.bridge.projects.save(json.dumps({'config':config,'create':True}))
            self.assertEqual(backend.call_count,1)
        config=saved['config'];config['description']='已编辑'
        saved=self.bridge.projects.save(json.dumps({'config':config,'create':False,'revision':saved['revision']}))
        for enabled in [False,True]:
            saved=self.bridge.projects.enabled(json.dumps({'projectId':config['project_id'],'enabled':enabled,'revision':saved['revision']}))
            self.assertEqual(saved['config']['enabled'],enabled)
        self.bridge.projects.set_default(json.dumps({'projectId':config['project_id']}))
        self.assertTrue(next(p for p in json.loads(self.bridge.listProjects())['data'] if p['id']==config['project_id'])['isDefault'])

    def test_duplicate_id_workspace_and_stale_revision(self):
        original=self.project()
        for config in [original['config'],dict(original['config'],project_id='duplicate')]:
            with self.assertRaises(BusinessError): self.bridge.projects.save(json.dumps({'config':config,'create':True}))
        config=self.fresh();saved=self.bridge.projects.save(json.dumps({'config':config,'create':True}))
        with self.assertRaises(BusinessError):self.bridge.projects.save(json.dumps({'config':config,'create':False,'revision':'old'}))
        self.assertEqual(saved['revision'],hashlib.sha256((self.registry/'registered-new.json').read_bytes()).hexdigest())

    def test_backend_rejects_illegal_paths_permissions_fields(self):
        config=self.fresh()
        for override in [{'workspace':str(self.root/'missing')},{'allowed_roots':[str(self.root/'elsewhere')]},{'project_id':'../x'}, {'executor':'codex'}, {'participants':[{'id':'a','role':'root','executor':'mock'}]}, {'workspace':r'C:\example\ai-interview-system'}]:
            candidate=dict(config,**override)
            result=json.loads(self.bridge.saveProject(json.dumps({'config':candidate,'create':True})))
            self.assertFalse(result['ok'],override)
        self.assertFalse((self.registry/'registered-new.json').exists())

    def test_invalid_registration_does_not_hide_other_projects(self):
        (self.registry/'broken.json').write_text('{bad',encoding='utf8')
        rows=self.store.list_projects()
        self.assertEqual(len(rows),4)
        self.assertIn('configError',next(row for row in rows if row['id']=='broken'))

    def test_real_configuration_can_be_saved_but_cannot_execute(self):
        config=self.fresh();config['participants'][0]['executor']='codex'
        saved=self.bridge.projects.save(json.dumps({'config':config,'create':True}))
        self.assertFalse(saved['execution']['allowed'])
        value=self.request();value['projectId']=config['project_id']
        with patch.object(self.client,'connect',side_effect=AssertionError('must not start host')):
            self.assertFalse(json.loads(self.bridge.submitTask(json.dumps(value)))['ok'])

    def test_edit_does_not_reapprove_mock_policy(self):
        before=self.policy.read_bytes();data=self.project();config=data['config'];config['description']='changed'
        saved=self.bridge.projects.save(json.dumps({'config':config,'create':False,'revision':data['revision']}))
        self.assertFalse(saved['execution']['allowed']);self.assertEqual(before,self.policy.read_bytes())
        with self.assertRaises(BusinessError):self.rules.build(self.request(),'denied')

    def test_active_project_edit_is_refused(self):
        host=self.start();task=host.submit(json.dumps(self.request('delayed')))
        data=self.project()
        with self.assertRaises(BusinessError):self.bridge.projects.enabled(json.dumps({'projectId':'studio-mock-a','enabled':False,'revision':data['revision']}))
        self.assertEqual(self.wait(task)['state'],'verified')

    def test_budget_validation_denies_increase_nan_bool_and_fields(self):
        for budget in [{'modelCalls':7},{'modelCalls':True},{'rounds':0},{'runSeconds':float('nan')},{'repairs':2},{'stepSeconds':26},{'shell':1}]:
            value=self.request();value['budget']=budget
            result=json.loads(self.bridge.submitTask(json.dumps(value)))
            self.assertFalse(result['ok'],budget)

    def test_actual_budget_exhaustion_is_never_verified(self):
        value=self.request();value['budget']={'modelCalls':1}
        task=self.start().submit(json.dumps(value));status=self.wait(task)
        self.assertEqual(status['stop_reason'],'budget');self.assertFalse(status['success'])
        record=self.store.list_tasks()['items'][0]
        self.assertEqual(record['outcome']['kind'],'budget');self.assertFalse(record['outcome']['passed'])

    def test_readonly_review_has_no_write_or_verified_claim(self):
        before=(self.root/'mock-workspaces/a/value.txt').read_bytes()
        value=self.request('review');value['mode']='read_only'
        parsed=task_request(json.dumps(value),True);config=self.rules.build(parsed,'readonly-config')
        self.assertEqual(config.write_paths,[]);self.assertEqual(config.flow,['mock-review'])
        task=self.start().submit(json.dumps(value));status=self.wait(task)
        self.assertFalse(status['success']);self.assertNotEqual(status['state'],'verified')
        self.assertEqual(before,(self.root/'mock-workspaces/a/value.txt').read_bytes())
        record=self.store.list_tasks()['items'][0];run=self.store.load_task(record['id'])
        self.assertEqual(run['usage']['mock_calls'],1);self.assertEqual(run['task']['modelCalls'],0)

    def test_mode_cannot_select_write_template(self):
        for template,mode in [('verified','read_only'),('review','controlled_write'),('verified','real')]:
            value=self.request(template);value['mode']=mode
            self.assertFalse(json.loads(self.bridge.submitTask(json.dumps(value)))['ok'])

    def test_over_100_history_resolves_opaque_and_schedule_task(self):
        for index in range(151):
            run=self.runs/f'history-{index}'/'saved';run.mkdir(parents=True)
            (run/'task.json').write_text(json.dumps({'task_id':f'history-{index}','project_id':'studio-mock-a','title':str(index),'created_at':f'{index:06}'}))
        old=next(row for row in self.store.list_tasks('',100,100)['items'] if row['taskId']=='history-0')
        self.assertNotIn(old['id'],[row['id'] for row in self.store.list_tasks()['items']])
        self.assertEqual(self.store.load_task(old['id'])['task']['taskId'],'history-0')
        self.assertEqual(self.store.find_task('history-0')['task']['id'],old['id'])

    def test_schedule_selection_outside_100_and_cancel_feedback(self):
        host=self.start();task=host.submit(json.dumps(self.request('delayed')))
        host.cancel(json.dumps({'scheduleId':task['scheduleId']}))
        row=host.schedules('{}')['items'][0];self.assertTrue(row['cancellationRequested'])
        self.wait(task)
        for index in range(120):
            file=self.runs/'schedule'/f'sch-studio-fixture-{index}.json'
            file.write_text(json.dumps({'schedule_id':file.stem,'state':'failed','queued_at':f'999{index:05}'}))
        page=host.schedules(json.dumps({'scheduleId':task['scheduleId']}))
        self.assertEqual(page['total'],121);self.assertEqual(len(page['items']),101)
        self.assertTrue(any(row['schedule_id']==task['scheduleId'] for row in page['items']))

    def test_management_transport_and_fields_are_allowlisted(self):
        transport=RestrictedTransport(FakePage(),self.bridge)
        self.assertEqual(transport.methods,set(METHODS+BUSINESS_METHODS+MANAGEMENT_METHODS))
        for method in MANAGEMENT_METHODS:
            self.assertFalse(json.loads(getattr(self.bridge,method)('{"shell":"whoami"}'))['ok'])
            for args in [[{}],['x'*12001],[],[True]]:
                self.assertFalse(transport.receive(json.dumps({'type':6,'id':1,'object':'history','method':method,'args':args})))

    def test_new_project_access_report_does_not_sign_or_execute(self):
        config=self.fresh()
        self.bridge.projects.save(json.dumps({'config':config,'create':True}))
        with patch('subprocess.Popen', side_effect=AssertionError('must not start a process')):
            report=self.bridge.projects.inspect_access(json.dumps({'projectId':'registered-new','export':True}))
        self.assertEqual(report['realModelCalls'],0)
        self.assertFalse(report['signerInstalled'])
        self.assertFalse(report['modes']['mock']['allowed'])
        self.assertFalse(report['modes']['real']['allowed'])
        self.assertFalse(report['modes']['offline_cli']['allowed'])
        self.assertNotIn('signature', report['unsigned'])
        self.assertEqual(report['account']['status'],'NOT CHECKED')
        text=(self.runs/'studio-grant-exports'/'registered-new.unsigned.json').read_text(encoding='utf8')
        self.assertNotIn('BEGIN PRIVATE KEY', text)
        self.assertNotIn('"signature"', text)
        self.assertIn('unsigned-grant-request', text)
        self.assertFalse((self.runs/'studio-authorizations'/'registered-new.json').exists())
        self.assertTrue(any('公钥' in step for step in report['nextSteps']))
        self.assertTrue(any(item['code']=='permission' and item['status']=='BLOCKED' for item in report['checklist']))
        self.assertNotIn('BEGIN PRIVATE KEY', json.dumps(report['checklist']))
        allowed=self.bridge.projects.inspect_access(json.dumps({'projectId':'studio-mock-a'}))
        self.assertTrue(allowed['modes']['mock']['allowed'])
        self.assertIsNone(allowed['export'])

    def test_git_status_read_only_and_trusted_commands_not_executed(self):
        config=self.fresh();config['verification'][1]['argv']=['unapproved-program','danger']
        with patch('desktop_validation.project_api.subprocess.run') as process:
            process.return_value.returncode=1;process.return_value.stdout='';process.return_value.stderr='not git'
            saved=self.bridge.projects.save(json.dumps({'config':config,'create':True}))
            self.assertFalse(saved['execution']['allowed'])
            for call in process.call_args_list:self.assertEqual(call.args[0][0],'git')

if __name__=='__main__':unittest.main()
