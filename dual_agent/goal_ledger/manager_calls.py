"""Manager model-call audit business API. Reservations are never refunded/replayed."""
import json
from .db import now
from .model import ConflictError
from .schemas import canonical, identifier

PENDING = {'INTENT','STARTED'}
FINAL = {'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','NEEDS_RECONCILIATION'}


class ManagerCallAPI:
    def _call_owner(self,c,goal_id,owner):
        row=self._row(c,'manager_runs','goal_id',identifier(goal_id))
        if not owner or row['owner']!=owner: raise ConflictError('Stale Manager call owner')
        return row

    def manager_call_reserve(self,goal_id,owner,metadata,*,max_calls,expected_revision):
        self._open()
        identifier(metadata['request_id']);identifier(metadata['action_id'])
        with self._db.transaction() as c:
            row=self._call_owner(c,goal_id,owner)
            if self._goal(c,goal_id).revision!=expected_revision: raise ConflictError('Stale Manager request')
            if row['signal']: raise ConflictError('Manager signalled before request')
            if metadata['manager_run_id']!=row['run_id']: raise ConflictError('Manager run changed')
            action=c.execute('SELECT * FROM manager_actions WHERE run_id=? AND action_id=?',
                             (row['run_id'],metadata['action_id'])).fetchone()
            if action is None:raise ConflictError('Manager call action missing')
            details=json.loads(action['data'])
            kinds={'generate_plan':'PLAN','review_result':'REVIEW','propose_replan':'REPLAN'}
            if action['run_id']!=row['run_id'] or details['status']!='INTENT' or details['kind']!=kinds.get(metadata['operation']):
                raise ConflictError('Manager call lacks matching action intent')
            if c.execute('SELECT 1 FROM manager_calls WHERE request_id=?',(metadata['request_id'],)).fetchone():
                raise ConflictError('Manager request already reserved; never replay')
            used=c.execute('SELECT count(*) FROM manager_calls WHERE authorization=?',(metadata['authorization'],)).fetchone()[0]
            if type(max_calls) is not int or max_calls<1 or used>=max_calls:
                raise ConflictError('Signed Manager call budget exhausted')
            data={**metadata,'goal_id':goal_id,'started_at':None,'ended_at':None,
                'intent_at':now(),'usage':{'source':'UNKNOWN'},'billing':'UNKNOWN','failure_reason':'',
                'cancel_reason':'','exit_code':None,'processes_cleared':None,'result_hash':None,'session_id':None}
            c.execute('INSERT INTO manager_calls VALUES(?,?,?,?,?,?)',(metadata['request_id'],row['run_id'],
                metadata['action_id'],metadata['authorization'],'INTENT',canonical(data)))
            return {**data,'state':'INTENT'}

    def manager_call_update(self,goal_id,owner,request_id,state,**changes):
        self._open()
        allowed={'failure_reason','cancel_reason','exit_code','processes_cleared','result_hash','usage','billing','session_id',
            'confirmed_result','usage_source','billing_state','failure_kind','cancellation','outcome','http_status','response_hash'}
        if set(changes)-allowed or len(canonical(changes))>16000: raise ValueError('Invalid call audit update')
        with self._db.transaction() as c:
            run=self._call_owner(c,goal_id,owner)
            row=self._row(c,'manager_calls','request_id',identifier(request_id))
            if row['run_id']!=run['run_id'] or row['state'] not in PENDING:
                raise ConflictError('Finalized or foreign Manager call')
            if state not in FINAL and not (row['state']=='INTENT' and state=='STARTED'):
                raise ConflictError('Invalid Manager call transition')
            data={**json.loads(row['data']),**changes}
            if state=='STARTED':data['started_at']=now()
            else:data['ended_at']=now()
            c.execute('UPDATE manager_calls SET state=?,data=? WHERE request_id=?',(state,canonical(data),request_id))
            return {**data,'state':state}

    def manager_calls(self,goal_id):
        self._open()
        with self._db.read() as c:
            run=self._row(c,'manager_runs','goal_id',identifier(goal_id))
            return [{**json.loads(r['data']),'state':r['state']} for r in c.execute(
                'SELECT * FROM manager_calls WHERE run_id=? ORDER BY rowid',(run['run_id'],))]

    def manager_calls_reconcile(self,goal_id,owner):
        # Owning the Manager tick excludes another live caller. PID absence
        # never restores quota or proves that a provider did not charge.
        with self._db.transaction() as c:
            run=self._call_owner(c,goal_id,owner)
            rows=c.execute("SELECT * FROM manager_calls WHERE run_id=? AND state IN ('INTENT','STARTED')",(run['run_id'],)).fetchall()
            for row in rows:
                data={**json.loads(row['data']),'ended_at':now(),'failure_reason':'host_confirmation_lost','billing':'UNKNOWN'}
                if data.get('backend_kind')=='api':
                    data.update(outcome='NEEDS_RECONCILIATION',failure_kind='host_confirmation_lost',billing_state='UNKNOWN')
                c.execute("UPDATE manager_calls SET state='NEEDS_RECONCILIATION',data=? WHERE request_id=?",(canonical(data),row['request_id']))
            return bool(rows) or bool(c.execute("SELECT 1 FROM manager_calls WHERE run_id=? AND state='NEEDS_RECONCILIATION'",(run['run_id'],)).fetchone())
