"""Public offline checks: fixed fixtures, no installed agents or model network.

Generated fixture keys stay local under ignored .test-output; never upload it.
This guard covers this interpreter and reviewed fixture launch paths, not the OS.
"""
from pathlib import Path
import argparse
import ast
import faulthandler
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--suite', choices=['all','core','desktop','api','rehearsal','ledger','host','manager','manager-cli'], default='all')
    p.add_argument('--test', help='One protected diagnostic unittest ID')
    args = p.parse_args()
    if args.suite == 'all' and not args.test:
        # Separate interpreters match the accepted internal suite boundaries,
        # preserve each completed result, and avoid cross-suite Qt/test globals.
        groups=['api','manager-cli','manager','ledger','host','core','desktop','rehearsal']
        records=[]
        for group in groups:
            print('Starting offline group: '+group,flush=True)
            completed=subprocess.run([sys.executable,'-B',str(Path(__file__)), '--suite',group],cwd=ROOT)
            records.append(dict(suite=group,exitCode=completed.returncode))
            (ROOT/'.test-output/group-checkpoints.json').write_text(json.dumps(records,indent=2)+'\n',encoding='utf8')
        print(json.dumps(records,indent=2))
        return 0 if all(row['exitCode']==0 for row in records) else 1
    output = ROOT / '.test-output' / (time.strftime('%Y%m%d-%H%M%S') + '-' + args.suite)
    scratch = output / 'temp'
    scratch.mkdir(parents=True)
    tempfile.tempdir = str(scratch)
    os.environ.update(TEMP=str(scratch), TMP=str(scratch), PYTHONDONTWRITEBYTECODE='1', QT_QPA_PLATFORM='offscreen')
    sys.path[:0] = [str(ROOT),str(ROOT/'tests'),str(ROOT/'desktop-validation/tests'),str(ROOT/'desktop-validation/python')]
    popen, which = subprocess.Popen, shutil.which
    blocked = []
    attempts = []
    def no_connect(*a, **kw):
        attempts.append('socket connection rejected')
        raise PermissionError('Public offline tests forbid network connections')
    socket.socket.connect = no_connect
    socket.socket.connect_ex = no_connect
    class OfflinePopen(popen):
        def __init__(self, command, *extra, **kw):
            tokens = [str(x) for x in command] if isinstance(command,(tuple,list)) else [str(command)]
            base = Path(tokens[0]).stem.casefold()
            is_cursor = base == 'node' and any('cursor' in t.casefold() and t.endswith('.js') for t in tokens[1:])
            if base in {'codex','cursor','cursor-agent','agent'} or is_cursor:
                blocked.append(base)
                raise PermissionError('Installed agent launch rejected by offline checks')
            super().__init__(command,*extra,**kw)
    subprocess.Popen = OfflinePopen
    shutil.which = lambda command,*a,**kw: None if Path(str(command)).stem.casefold() in {'codex','cursor','cursor-agent','agent'} else which(command,*a,**kw)
    modules = ['test_bridge','test_task_api','test_authorization_display_v141','test_usage_v131',
               'test_studio_v04b','test_reliability','test_cli_v05']
    # Override only test evidence roots. No original internal maintenance paths
    # or original run artifacts are required by this public test entry point.
    for name in ['test_goal_host','test_manager','test_manager_transport','test_manager_api']:
        module = __import__(name)
        module.OUT = output / name
        module.OUT.mkdir()
    for path in list((ROOT/'dual_agent').rglob('*.py')) + list((ROOT/'desktop-validation/python').rglob('*.py')):
        ast.parse(path.read_text(encoding='utf8'), filename=str(path.relative_to(ROOT)))
    loader = unittest.defaultTestLoader
    suites = []
    if args.suite in {'all','core'}:
        suites.append(loader.discover(str(ROOT/'tests')))
    if args.suite in {'all','desktop'}:
        suites.append(loader.loadTestsFromNames(modules))
    if args.suite == 'api':
        suites.append(loader.loadTestsFromName('test_manager_api'))
    if args.suite == 'rehearsal':
        suites.append(loader.loadTestsFromName('test_rehearsal_v10'))
    for group,module in [('ledger','test_goal_ledger'),('host','test_goal_host'),('manager','test_manager'),('manager-cli','test_manager_transport')]:
        if args.suite == group:
            suites.append(loader.loadTestsFromName(module))
    if args.test:
        suites=[loader.loadTestsFromName(args.test)]
    unique = {}
    def visit(suite):
        for item in suite:
            if isinstance(item,unittest.TestSuite):
                visit(item)
            else:
                if item._testMethodName in {'test_codex_exec_help_matches_builder_flags','test_doctor_reads_real_versions_without_prompt'}:
                    method = getattr(type(item),item._testMethodName)
                    setattr(type(item),item._testMethodName,unittest.skip('Explicit zero installed Agent CLI scope, including help/version')(method))
                unique.setdefault(item.id(),item)
    for suite in suites:
        visit(suite)
    started = time.monotonic()
    with (output/'tests.log').open('w',encoding='utf8') as log:
        class ProgressResult(unittest.TextTestResult):
            # Public progress contains test identities only, never fixture keys,
            # payloads, raw model messages or environment values.
            def startTest(self,test):
                print('TEST_START '+test.id(),flush=True)
                faulthandler.dump_traceback_later(90,repeat=True,file=log)
                super().startTest(test)
            def stopTest(self,test):
                faulthandler.cancel_dump_traceback_later()
                super().stopTest(test)
            def addSuccess(self,test):
                super().addSuccess(test)
                print('TEST_PASS '+test.id(),flush=True)
            def addFailure(self,test,err):
                super().addFailure(test,err)
                print('TEST_FAIL '+test.id(),flush=True)
            def addError(self,test,err):
                super().addError(test,err)
                print('TEST_ERROR '+test.id(),flush=True)
            def addSkip(self,test,reason):
                super().addSkip(test,reason)
                print('TEST_SKIP '+test.id(),flush=True)
        result = unittest.TextTestRunner(stream=log,verbosity=2,resultclass=ProgressResult).run(unittest.TestSuite(unique.values()))
    summary = dict(suite=args.suite,tests=result.testsRun,passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        failures=len(result.failures),errors=len(result.errors),skipped=[dict(test=str(t),reason=r) for t,r in result.skipped],
        blockedAgentLaunches=blocked,blockedNetworkAttempts=attempts,seconds=round(time.monotonic()-started,3),
        success=result.wasSuccessful() and not blocked,python=sys.version,platform=sys.platform,testIds=list(unique))
    (output/'results.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf8')
    print(json.dumps({k:v for k,v in summary.items() if k!='testIds'},indent=2))
    print('Local evidence: ' + str(output.relative_to(ROOT)))
    if not summary['success']:
        print((output/'tests.log').read_text(encoding='utf8')[-18000:],file=sys.stderr)
    return 0 if summary['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
