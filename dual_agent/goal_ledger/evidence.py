"""Safe evidence references and the trusted-backend assessment boundary.

Saved JSON alone is never a verification authority. Phase B must install a
source backed by observed Coordinator outcomes and current workspace evidence.
There is deliberately no DTO constructor for an EvidenceAssessment.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
from typing import Protocol

from dual_agent.safety import finalize_declared, is_inside, is_reparse_point
from .model import Attempt, EvidenceAssessment, EvidenceError
from .schemas import relative_path

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
ARTIFACTS = {'verification.json', 'result.json', 'workspace_snapshot.json', 'worktree-status.json'}


class EvidenceSource(Protocol):
    """Backend capability; implementations must fail closed when freshness is unknown.

    An assessment must bind current workspace HEAD/snapshot, run, attempt and
    bytes. Never implement this by trusting model fields or saved JSON alone.
    The source is reconsulted at acceptance, readiness and recovery, including
    after restart. No source means references are untrusted and cannot complete.
    """
    def assess(self, attempt: Attempt, relative: str, sha256: str) -> EvidenceAssessment | None: ...


def file_hash(root: Path, run_id: str, relative: str) -> str:
    relative = relative_path(relative)
    if relative not in {run_id + '/' + name for name in ARTIFACTS}:
        raise EvidenceError('Evidence must be an allowlisted artifact of the bound run')
    candidate = root / relative
    try:
        current = root
        for part in relative.split('/'):
            current /= part
            if current.is_symlink() or is_reparse_point(current):
                raise EvidenceError('Evidence cannot traverse reparse points')
        safe = finalize_declared(str(candidate))
        if not is_inside(safe, root) or not safe.is_file():
            raise EvidenceError('Evidence is missing or outside the managed runs root')
        before = safe.stat()
        if before.st_size > MAX_EVIDENCE_BYTES:
            raise EvidenceError('Evidence exceeds 8 MiB reference limit')
        with safe.open('rb') as stream:
            # Check the opened handle as well as the declared path, as HistoryStore does.
            if os.name == 'nt':
                import ctypes
                import msvcrt
                from ctypes import wintypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                final = kernel.GetFinalPathNameByHandleW
                final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
                final.restype = wintypes.DWORD
                buffer = ctypes.create_unicode_buffer(32768)
                count = final(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
                if not count or count >= len(buffer):
                    raise EvidenceError('Cannot verify opened evidence handle')
                actual = buffer.value
                if actual.startswith('\\\\?\\UNC\\'):
                    actual = '\\\\' + actual[8:]
                elif actual.startswith('\\\\?\\'):
                    actual = actual[4:]
                if not is_inside(Path(actual), root):
                    raise EvidenceError('Opened evidence handle escapes runs root')
            else:
                actual = Path(os.readlink(f'/proc/self/fd/{stream.fileno()}'))
                if not is_inside(actual, root):
                    raise EvidenceError('Opened evidence handle escapes runs root')
            opened = os.fstat(stream.fileno())
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
            if signature(before) != signature(opened):
                raise EvidenceError('Evidence changed during open')
            data = stream.read(MAX_EVIDENCE_BYTES + 1)
            if len(data) > MAX_EVIDENCE_BYTES or signature(os.fstat(stream.fileno())) != signature(opened):
                raise EvidenceError('Evidence changed during read')
        if signature(safe.stat()) != signature(before):
            raise EvidenceError('Evidence was replaced during read')
        return hashlib.sha256(data).hexdigest()
    except (OSError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f'Cannot reference evidence: {exc}') from exc
