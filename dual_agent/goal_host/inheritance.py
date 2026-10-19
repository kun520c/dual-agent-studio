"""Tracked-file patch inheritance in a new original-host Worktree, offline only.

No commits, refs, merge, checkout/reset or source writes. The dependent's scope
must cover inherited modifications. Linear cumulative patches are supported;
multi-parent integration and untracked/binary mode changes fail closed.
"""
from pathlib import Path
import hashlib
import time
from dual_agent.goal_ledger import EvidenceError, WorkStatus as W
from dual_agent.goal_ledger.schemas import relative_path, digest
from dual_agent.goal_ledger.db import exact_local_path
from dual_agent.worktree import _git
from dual_agent.safety import same_path, assert_no_escaping_links
from .identity import save


class EvidenceUnavailable(EvidenceError):
    """May be a short cooperative lease conflict; never sufficient to accept."""


def covered(path, scopes):
    return any(path.casefold()==s.casefold() or path.casefold().startswith(s.casefold()+'/') for s in scopes)


class DependencyPatch:
    def __init__(self, goal_host, work, mode):
        self.gh, self.work = goal_host, work
        if mode not in {'mock','offline_cli'}:
            raise EvidenceError('Dependency patch transfer is currently offline-only; production transfer is blocked')
        if len(work.dependencies)!=1:
            raise EvidenceError('Safe patch transfer currently requires one direct dependency')
        self.parent_id=work.dependencies[0]

    def _parent(self):
        s=self.gh.goals
        parent=s.get_work_item(self.parent_id)
        if parent.status!=W.COMPLETED or not parent.current_attempt_id:
            raise EvidenceError('Dependency is not accepted')
        if parent.goal_id!=self.work.goal_id or parent.plan_revision!=self.work.plan_revision:
            raise EvidenceError('Cross-plan dependency transfer is forbidden')
        # Includes the whole dependency closure and any explicitly invalidated evidence.
        if not s.work_completion_usable(parent.work_item_id):
            raise EvidenceUnavailable('Dependency evidence is currently unavailable or invalidated')
        a=s.get_attempt(parent.current_attempt_id)
        if not self.gh.verification_status(a.attempt_id)['available']:
            raise EvidenceUnavailable('Dependency has no current Coordinator attestation')
        return a

    def inspect(self):
        with self.gh.source.guard(self.work.goal_id):
            a=self._parent()
            repo=exact_local_path(Path(a.workspace_ref))
            assert_no_escaping_links(repo,repo)
            code, status, _ = _git(repo,['status','--porcelain','--untracked-files=all'],timeout=5)
            if code or any(line[:2] not in {' M','M ','MM'} for line in status.splitlines()):
                raise EvidenceError('Only modified existing tracked files can be inherited')
            code, names, _ = _git(repo,['diff','--name-only','-z','HEAD','--'],timeout=5)
            paths=[relative_path(p) for p in names.strip('\0\r\n').split('\0') if p]
            if code or not paths: raise EvidenceError('No inspectable dependency changes')
            if not all(covered(p,self.work.allowed_write_scope) for p in paths):
                raise EvidenceError('Inherited changes exceed dependent write scope')
            for path in paths:
                exact_local_path(repo/path)
                code, mode, _ = _git(repo,['ls-tree','HEAD','--',path],timeout=5)
                if code or not mode.startswith('100644 '): raise EvidenceError('Only regular tracked source files supported')
            code, patch, error=_git(repo,['diff','--binary','--no-ext-diff','--no-textconv','HEAD','--'],timeout=5)
            if code or not patch or 'GIT binary patch' in patch or '\nold mode ' in patch or '\nnew mode ' in patch:
                raise EvidenceError('Unsupported patch content: '+error)
            patch=patch if patch.endswith('\n') else patch+'\n'
            return a,patch,paths

    def prepare(self, mapping, config, run_dir):
        """Called after target write-lock acquisition, before Executor invocation."""
        deadline=time.monotonic()+min(3,config.timeout_seconds)
        while True:
            try:
                return self._prepare_locked(mapping,config,run_dir)
            except EvidenceUnavailable:
                if time.monotonic()>=deadline: raise
                # Release the failed source lease before waiting. Source
                # revocation/tampering still fails after this bounded retry.
                time.sleep(.05)

    def _prepare_locked(self, mapping, config, run_dir):
        with self.gh.source.guard(self.work.goal_id):
            a,patch,paths=self.inspect()
            target=exact_local_path(Path(config.workspace))
            if not same_path(target,Path(mapping['workspace_ref'])) or same_path(target,Path(a.workspace_ref)):
                raise EvidenceError('Unexpected inheritance target')
            if a.base_head!=mapping['base_head']:
                raise EvidenceError('Dependency baseline differs; safe integration requires reconciliation')
            code, head, _=_git(target,['rev-parse','HEAD'],timeout=5)
            code2, dirty, _=_git(target,['status','--porcelain'],timeout=5)
            if code or code2 or head.strip()!=a.base_head or dirty.strip():
                raise EvidenceError('Target baseline is dirty or stale')
            assert_no_escaping_links(target,target)
            file=exact_local_path(run_dir/'inheritance.patch')
            with file.open('x',encoding='utf8',newline='\n') as stream: stream.write(patch)
            # git apply reads a backend-owned patch path; output is never a command.
            code,_,error=_git(target,['apply','--check','--whitespace=nowarn',str(file)],timeout=5)
            if code: raise EvidenceError('Dependency patch conflicts: '+error)
            code,_,error=_git(target,['apply','--whitespace=nowarn',str(file)],timeout=5)
            if code: raise EvidenceError('Dependency patch failed: '+error)
            self._parent()  # Recheck explicit revocation immediately before Executor start.
            receipt=dict(kind='OFFLINE_TRACKED_PATCH',source_attempt=a.attempt_id,source_generation=a.generation,
                source_run=a.run_id,base_head=a.base_head,target_attempt=mapping['attempt_id'],target=str(target),
                changed_paths=paths,patch_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
                verification=self.gh.verification_status(a.attempt_id),summary_hash=digest([a.attempt_id,paths,patch]))
            save(run_dir/'inheritance.json',receipt)
            self.gh.source.require_dependencies(mapping['attempt_id'],[a.attempt_id])
