"""GoalService business API for Manager progress; never a second task ledger.

Owner tokens have no clock expiry. Only explicit release or a confirmed dead
process permits takeover. Thus a slow model cannot create an expired writer.
Model calls and execution waits never hold a SQLite transaction or file lock.
"""
import json
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from .db import now
from .model import ConflictError, TransitionError
from .schemas import canonical, digest, identifier, text

_EFFECT_OWNER = ContextVar('manager_effect_owner', default=None)
_MODEL_EFFECTS = frozenset({'plan.created','plan.activated','attempt.intent_recorded',
    'decision.proposed','attempt.accepted','work.retry_authorized'})


def process_may_be_alive(pid):
    """Unknown/access-denied PID is busy; only confirmed absence is reclaimable."""
    if pid<=0: return True
    from dual_agent.locks import pid_alive
    return pid_alive(pid)


class ManagerStateAPI:
    @contextmanager
    def manager_effects(self,goal_id,owner):
        """Thread-local fence, checked inside each existing domain transaction.

        No transaction or lock spans a Provider call. Cancellation and a fresh
        model-derived mutation linearize on the same SQLite write lock.
        """
        token=_EFFECT_OWNER.set((self,goal_id,owner))
        try:yield
        finally:_EFFECT_OWNER.reset(token)

    def _manager_effect_check(self,c,operation,goal_id):
        fence=_EFFECT_OWNER.get()
        if operation not in _MODEL_EFFECTS or not fence or fence[0] is not self:return
        if goal_id!=fence[1]:raise ConflictError('Manager mutation targets another Goal')
        row=self._row(c,'manager_runs','goal_id',goal_id)
        if row['owner']!=fence[2] or row['signal']:
            raise ConflictError('Manager mutation fenced by owner/cancellation/pause')

    def manager_start(self, goal_id, configuration):
        self._open()
        identifier(goal_id)
        encoded = canonical(configuration)
        if len(encoded) > 64000: raise ValueError('Manager configuration too large')
        with self._db.transaction() as c:
            self._goal(c, goal_id)
            row = c.execute('SELECT * FROM manager_runs WHERE goal_id=?', (goal_id,)).fetchone()
            if row:
                state = json.loads(row['data'])
                if state['configuration_hash'] != digest(configuration):
                    raise ConflictError('Manager configuration changed; explicit new-goal workflow required')
                return self._manager_decode(row)
            state = dict(manager_run_id='manager-'+uuid.uuid4().hex, goal_id=goal_id,
                configuration=configuration, configuration_hash=digest(configuration),
                phase='READY', plan_revision=None, waiting_attempt=None, decisions=0,
                provider_calls=0, repairs={}, failures=[], last_action=None, stop_reason='',
                created_at=now(), updated_at=now(), host_epoch='', elapsed_seconds=0)
            c.execute('INSERT INTO manager_runs(goal_id,run_id,data,version) VALUES(?,?,?,0)',
                      (goal_id, state['manager_run_id'], canonical(state)))
            return {**state, 'version':0, 'signal':''}

    @staticmethod
    def _manager_decode(row):
        return {**json.loads(row['data']), 'version':row['version'], 'signal':row['signal']}

    def manager_state(self, goal_id):
        self._open()
        with self._db.read() as c:
            return self._manager_decode(self._row(c, 'manager_runs', 'goal_id', identifier(goal_id)))

    def manager_claim(self, goal_id, owner):
        self._open(); text(owner,100)
        with self._db.transaction() as c:
            row = self._row(c,'manager_runs','goal_id',identifier(goal_id))
            if row['owner']:
                # Access denied/unknown process liveness is conservatively busy.
                alive = process_may_be_alive(row['owner_pid'])
                if alive or row['owner_pid'] == os.getpid():
                    raise ConflictError('Another Manager tick owns this Goal')
            c.execute('UPDATE manager_runs SET owner=?,owner_pid=? WHERE goal_id=?', (owner,os.getpid(),goal_id))
            return self._manager_decode(row)

    def manager_save(self, goal_id, owner, state):
        self._open()
        with self._db.transaction() as c:
            row = self._row(c,'manager_runs','goal_id',identifier(goal_id))
            if row['owner'] != owner or row['version'] != state['version']:
                raise ConflictError('Stale Manager owner/version')
            previous=json.loads(row['data'])
            if any(state[k] != previous[k] for k in ('manager_run_id','goal_id','configuration','configuration_hash','created_at')):
                raise ConflictError('Manager identity/configuration is immutable')
            data={k:v for k,v in state.items() if k not in {'version','signal'}}
            data['updated_at']=now()
            c.execute('UPDATE manager_runs SET data=?,version=version+1 WHERE goal_id=?', (canonical(data),goal_id))
            return {**data,'version':row['version']+1,'signal':row['signal']}

    def manager_release(self, goal_id, owner):
        with self._db.transaction() as c:
            c.execute("UPDATE manager_runs SET owner='',owner_pid=0 WHERE goal_id=? AND owner=?", (goal_id,owner))

    def manager_signal(self, goal_id, signal):
        if signal not in {'PAUSE','CANCEL',''}: raise ValueError('Invalid Manager signal')
        with self._db.transaction() as c:
            row=self._row(c,'manager_runs','goal_id',identifier(goal_id))
            if row['signal']=='CANCEL' and signal!='CANCEL': raise TransitionError('Cancellation cannot be withdrawn')
            c.execute('UPDATE manager_runs SET signal=? WHERE goal_id=?',(signal,goal_id))

    def manager_action(self, goal_id, owner, action_id, details):
        """Append intent, then one immutable final record; no model authority."""
        identifier(action_id)
        if len(canonical(details))>160000: raise ValueError('Manager audit record too large')
        with self._db.transaction() as c:
            row=self._row(c,'manager_runs','goal_id',identifier(goal_id))
            if row['owner']!=owner: raise ConflictError('Stale Manager action owner')
            saved=c.execute('SELECT data FROM manager_actions WHERE run_id=? AND action_id=?',(row['run_id'],action_id)).fetchone()
            if saved:
                old=json.loads(saved[0])
                if old==details: return old
                if old['status']!='INTENT' or details['status'] not in {'APPLIED','REJECTED'}:
                    raise ConflictError('Manager audit is immutable after finalization')
                if old['kind']!=details['kind']: raise ConflictError('Manager action kind changed')
                details={**details,'intent':old}
                c.execute('UPDATE manager_actions SET data=? WHERE run_id=? AND action_id=?',(canonical(details),row['run_id'],action_id))
            else:
                if details['status']!='INTENT': raise ConflictError('Manager action requires intent first')
                c.execute('INSERT INTO manager_actions VALUES(?,?,?)',(row['run_id'],action_id,canonical(details)))
            return details

    def manager_actions(self, goal_id):
        with self._db.read() as c:
            row=self._row(c,'manager_runs','goal_id',identifier(goal_id))
            return [dict(action_id=r[0],**json.loads(r[1])) for r in c.execute(
                'SELECT action_id,data FROM manager_actions WHERE run_id=? ORDER BY rowid',(row['run_id'],))]
