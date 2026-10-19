import hashlib,json,sys,unittest,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'desktop-validation/python')]
from desktop_validation.store import HistoryStore
from dual_agent.coordinator import _format_evidence
from dual_agent.winproc import run_captured
import os

class UsageHistoryTests(unittest.TestCase):
 def test_success_compaction_preserves_verdict_and_failures(self):
  output='[INFO] compiling\n[WARNING] caution\nTests run: 8, Failures: 0, Errors: 0\nBUILD SUCCESS\n'
  base={'type':'command','ok':True,'exit_code':0,'stdout':output,'argv':['pinned','test'],'stderr':'stderr evidence'}
  compact=_format_evidence([base]);self.assertNotIn('compiling',compact)
  for marker in ['Tests run: 8','[WARNING] caution','BUILD SUCCESS','pinned test','stderr evidence','SHA-256=']:self.assertIn(marker,compact)
  failure={**base,'ok':False,'exit_code':1,'stdout':output+'FAIL important failure stack'}
  self.assertIn(failure['stdout'],_format_evidence([failure]))

 def test_live_public_output_before_process_exit(self):
  observations=[];start=time.monotonic()
  captured=run_captured([sys.executable,'-u','-c','import time;print("first",flush=True);time.sleep(.5);print("last",flush=True)'],cwd=str(ROOT),env=os.environ.copy(),input_bytes=None,timeout=3,on_stdout=lambda line:observations.append((line,time.monotonic()-start)))
  self.assertEqual(captured.exit_code,0);self.assertEqual(captured.stdout,b'first\r\nlast\r\n')
  self.assertEqual(len(observations),2);self.assertGreater(observations[-1][1]-observations[0][1],.3)

if __name__=='__main__':unittest.main()
