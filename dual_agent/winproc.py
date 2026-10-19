"""Windows 子进程树。超时后先结束整棵进程，再允许协调器释放写入锁。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
import threading
from ctypes import wintypes
from dataclasses import dataclass

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class ProcessCapture:
    stdout: bytes
    stderr: bytes
    exit_code: int | None
    timed_out: bool
    processes_cleared: bool
    error_kind: str = ''


def _job_empty(job):
    # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: ActiveProcesses at offset 40.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    data = ctypes.create_string_buffer(48)
    if not kernel.QueryInformationJobObject(job, 1, data, len(data), None):
        return False
    return ctypes.c_uint32.from_buffer(data, 40).value == 0


def run_controlled(command, *, cwd, env, input_bytes, control, startup_timeout=5,
                   stdout_limit=192000, stderr_limit=16000):
    """Bounded no-shell capture for the Manager channel.

    Windows children start suspended, enter a non-inheritable kill-on-close
    Job, then resume. Job assignment failure never resumes the child. This is
    lifecycle containment, NOT filesystem/tool isolation. No raw output is
    logged here. Existing Executor capture behavior remains unchanged.
    """
    import signal
    control.check()
    started = time.monotonic()
    job = _create_kill_job() if os.name == 'nt' else None
    if os.name == 'nt' and not job:
        return ProcessCapture(b'', b'', None, False, True, 'job_unavailable')
    process = None
    readers = []
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    stream_error = threading.Event()
    reason = ''
    cleared = False
    try:
        process = subprocess.Popen(list(command), cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            **({'creationflags':_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP | 4}
               if os.name == 'nt' else {'start_new_session':True}))
        if os.name == 'nt':
            if not _assign_job(job, process.pid):
                process.kill(); process.wait(timeout=3)
                return ProcessCapture(b'', b'', process.returncode, False, True, 'job_assignment')
            if time.monotonic()-started > startup_timeout:
                reason = 'startup_timeout'
            else:
                dll = ctypes.WinDLL('ntdll')
                dll.NtResumeProcess.argtypes = [wintypes.HANDLE]
                dll.NtResumeProcess.restype = ctypes.c_long
                if dll.NtResumeProcess(int(process._handle)) != 0:
                    reason = 'resume_failed'
        elif time.monotonic()-started > startup_timeout:
            reason = 'startup_timeout'

        def drain(stream, index, limit):
            try:
                while True:
                    block = stream.read(4096)
                    if not block: break
                    left = limit-len(buffers[index])
                    buffers[index].extend(block[:max(0,left)])
                    if len(block)>left: overflow.set()
            except (OSError, ValueError): stream_error.set()
            finally: stream.close()

        def feed():
            try:
                process.stdin.write(input_bytes); process.stdin.flush()
            except (OSError, ValueError): pass
            finally: process.stdin.close()

        for stream,index,limit in ((process.stdout,0,stdout_limit),(process.stderr,1,stderr_limit)):
            thread=threading.Thread(target=drain,args=(stream,index,limit),daemon=True)
            thread.start(); readers.append(thread)
        writer=threading.Thread(target=feed,daemon=True);writer.start()
        while not reason:
            try: control.check()
            except Exception as exc:
                # Avoid dependency from the shared process module to Manager.
                reason = 'cancelled' if type(exc).__name__=='ProviderCancelled' else 'timeout'
                break
            if overflow.is_set(): reason='output_limit';break
            if process.poll() is not None: break
            time.sleep(.01)
        # Kill the entire containment group even after normal parent exit;
        # descendants may still hold stdout open or be running independently.
        if os.name == 'nt':
            _terminate_tree(job,process.pid)
        else:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: reason=reason or 'cleanup_unknown'
        for thread in [*readers,writer]: thread.join(timeout=1)
        if os.name == 'nt':
            end=time.monotonic()+3
            while time.monotonic()<end and not _job_empty(job): time.sleep(.02)
            cleared=_job_empty(job)
        else:
            # No parent-death guarantee on POSIX; real profiles remain closed.
            cleared=process.poll() is not None
        cleared=cleared and not any(t.is_alive() for t in [*readers,writer])
        if overflow.is_set(): reason=reason or 'output_limit'
        if stream_error.is_set(): reason=reason or 'stream_error'
        if not cleared: reason='cleanup_unknown'
        return ProcessCapture(bytes(buffers[0]),bytes(buffers[1]),process.returncode,
            reason in {'timeout','startup_timeout'},cleared,reason)
    finally:
        if process is not None:
            if os.name == 'nt': _terminate_tree(job,process.pid)
            elif process.poll() is None:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: pass
            for stream in (process.stdin,process.stdout,process.stderr):
                if stream and not stream.closed and not any(t.is_alive() for t in readers): stream.close()
        _close_handle(job)


def run_captured(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    input_bytes: bytes | None,
    timeout: float,
    on_stdout=None,
) -> ProcessCapture:
    if os.name != "nt":
        return _run_posix(command, cwd=cwd, env=env, input_bytes=input_bytes, timeout=timeout)
    return _run_windows(command, cwd=cwd, env=env, input_bytes=input_bytes, timeout=timeout, on_stdout=on_stdout)


def _run_posix(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    input_bytes: bytes | None,
    timeout: float,
) -> ProcessCapture:
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=cwd,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        return ProcessCapture(stdout or b"", stderr or b"", None, True, True)
    return ProcessCapture(completed.stdout or b"", completed.stderr or b"", completed.returncode, False, True)


def _run_windows(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    input_bytes: bytes | None,
    timeout: float,
    on_stdout=None,
) -> ProcessCapture:
    job = _create_kill_job()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
    )
    _assign_job(job, process.pid)
    chunks=[]; reader=None
    if on_stdout is not None:
        stream=process.stdout
        def drain():
            try:
                while True:
                    line=stream.readline()
                    if not line:break
                    chunks.append(line)
                    try:on_stdout(line)
                    except Exception:pass  # Observability never grants execution or changes success.
            finally:stream.close()
        reader=threading.Thread(target=drain,daemon=True)
        reader.start()
        process.stdout=None
    timed_out = False
    try:
        stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_tree(job, process.pid)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = b"", b""
    cleared = _wait_until_dead(process.pid)
    if reader:
        reader.join(timeout=5)
        stdout=b''.join(chunks)
    _close_handle(job)
    code = None if timed_out else process.returncode
    return ProcessCapture(stdout or b"", stderr or b"", code, timed_out, cleared)


def _create_kill_job() -> int | None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    ok = kernel.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        _close_handle(handle)
        return None
    return int(handle)


def _assign_job(job: int | None, pid: int) -> bool:
    if not job or pid <= 0:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    process = kernel.OpenProcess(access, False, pid)
    if not process:
        return False
    try:
        return bool(kernel.AssignProcessToJobObject(job, process))
    finally:
        _close_handle(process)


def _terminate_tree(job: int | None, root_pid: int) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if job:
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject(job, 1)
    for pid in _descendants(root_pid) | ({root_pid} if root_pid > 0 else set()):
        _terminate_pid(pid)


def _wait_until_dead(root_pid: int) -> bool:
    deadline = time.time() + 5
    while time.time() < deadline:
        alive = [pid for pid in _descendants(root_pid) if _pid_alive(pid)]
        if root_pid > 0 and _pid_alive(root_pid):
            alive.append(root_pid)
        if not alive:
            return True
        for pid in alive:
            _terminate_pid(pid)
        time.sleep(0.05)
    return False


def _descendants(root_pid: int) -> set[int]:
    if root_pid <= 0:
        return set()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return set()
    children: dict[int, list[int]] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            return set()
        while True:
            children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _close_handle(snapshot)
    found: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in found or pid == root_pid:
            continue
        found.add(pid)
        stack.extend(children.get(pid, []))
    return found


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        kernel.TerminateProcess(handle, 1)
    finally:
        _close_handle(handle)


def _pid_alive(pid: int) -> bool:
    """已退出但句柄尚未关闭的进程不算存活。STILL_ACTIVE 是 259。"""
    if pid <= 0:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        _close_handle(handle)


def _close_handle(handle: int | None) -> None:
    if not handle:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CloseHandle(handle)


class _IOCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]
