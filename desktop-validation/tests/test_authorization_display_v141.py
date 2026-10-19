"""GUI projection tests: public payload and ledger only, no keys or signatures."""
from pathlib import Path
import hashlib,json,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'desktop-validation/python')]
from desktop_validation.project_api import _authorization_availability
from dual_agent.execution_gate import canonical

class DisplayTests(unittest.TestCase):
 def test_consumed_payload_is_not_presented_as_remaining_authority(self):
  with tempfile.TemporaryDirectory(prefix='studio-ui-count-',dir=tempfile.gettempdir()) as temp:
   runs=Path(temp);grants=runs/'studio-authorizations';ledger=runs/'studio-host/requests'
   grants.mkdir();ledger.mkdir(parents=True)
   payload={'projectId':'sample','mode':'real','maxTasks':1}
   file=grants/'sample.json';file.write_text(json.dumps({'payload':payload}),encoding='utf8')
   record=ledger/'used.json';record.write_text(json.dumps({'authorization':hashlib.sha256(canonical(payload)).hexdigest()}),encoding='utf8')
   before={p:p.read_bytes() for p in [file,record]}
   view=_authorization_availability(runs,'sample')
   self.assertEqual((view['used'],view['remaining'],view['maximum']),(1,0,1))
   self.assertFalse(view['signatureVerifiedByThisRead'])
   self.assertEqual(before,{p:p.read_bytes() for p in before})
 def test_missing_payload_does_not_invent_remaining_execution(self):
  with tempfile.TemporaryDirectory(prefix='studio-ui-count-',dir=tempfile.gettempdir()) as temp:
   self.assertIsNone(_authorization_availability(Path(temp),'sample'))
 def test_invalid_ledger_does_not_become_zero_usage(self):
  with tempfile.TemporaryDirectory(prefix='studio-ui-count-',dir=tempfile.gettempdir()) as temp:
   runs=Path(temp);(runs/'studio-authorizations').mkdir();(runs/'studio-host/requests').mkdir(parents=True)
   (runs/'studio-authorizations/sample.json').write_text(json.dumps({'payload':{'projectId':'sample','mode':'real','maxTasks':1}}),encoding='utf8')
   (runs/'studio-host/requests/bad.json').write_text('{',encoding='utf8')
   self.assertIsNone(_authorization_availability(runs,'sample'))
if __name__=='__main__':unittest.main()
