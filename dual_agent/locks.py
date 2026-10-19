"""用目录互斥和锁文件防止两个任务同时写入重叠路径。"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from dual_agent.models import dump_json, now_iso
from dual_agent.safety import paths_overlap


class LockError(RuntimeError):
    """写入锁注册表不可用。"""


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = open_process(0x1000, 0, pid)
        if handle:
            close_handle(handle)
            return True
        # Access denied (including a less privileged host inspecting another
        # host) is not proof of death. Never remove a live lock/mutex merely
        # because Windows cannot disclose the owner process to this caller.
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: absent PID.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # Unknown/permission denied must preserve the owner's lock.
    return True


@dataclass
class LockHandle:
    manager: WriteLockManager
    path: Path
    task_id: str
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.manager.release(self)
        self.released = True


class WriteLockManager:
    _thread_gate = threading.Lock()

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.mutex_dir = self.root / "registry.mutex"

    def try_acquire(self, task_id: str, write_paths: list[Path]) -> tuple[LockHandle | None, str]:
        with self._thread_gate:
            return self._try_acquire(task_id, write_paths)

    def _try_acquire(self, task_id: str, write_paths: list[Path]) -> tuple[LockHandle | None, str]:
        if not write_paths:
            raise LockError("写入锁需要至少一条路径")
        self._acquire_mutex()
        handle_path: Path | None = None
        try:
            self._drop_dead_locks()
            for lock_path in self.root.glob("*.lock"):
                data = _read_lock(lock_path)
                if data is None:
                    continue
                held = [Path(item) for item in data.get("paths") or []]
                if any(paths_overlap(wanted, held_path) for wanted in write_paths for held_path in held):
                    owner = data.get("task_id") or "未知任务"
                    return None, f"路径与正在运行的任务“{owner}”重叠"
            handle_path = self.root / f"{uuid.uuid4().hex}.lock"
            dump_json(
                handle_path,
                {
                    "task_id": task_id,
                    "paths": [str(path) for path in write_paths],
                    "pid": os.getpid(),
                    "created_at": now_iso(),
                },
            )
        finally:
            self._release_mutex()
        if handle_path is None:
            return None, "未能创建写入锁"
        return LockHandle(self, handle_path, task_id), ""

    def release(self, handle: LockHandle) -> None:
        with self._thread_gate:
            self._acquire_mutex()
            try:
                handle.path.unlink(missing_ok=True)
            finally:
                self._release_mutex()

    def _drop_dead_locks(self) -> None:
        for lock_path in self.root.glob("*.lock"):
            data = _read_lock(lock_path)
            pid = data.get("pid") if isinstance(data, dict) else None
            if not isinstance(pid, int) or not pid_alive(pid):
                lock_path.unlink(missing_ok=True)

    def _acquire_mutex(self) -> None:
        deadline = time.time() + 15
        while True:
            try:
                self.mutex_dir.mkdir()
            except FileExistsError:
                if self._mutex_stale():
                    self._force_clear_mutex()
                    continue
                if time.time() > deadline:
                    raise LockError("等待写入锁注册表超时")
                time.sleep(0.02)
                continue
            try:
                (self.mutex_dir / "pid").write_text(str(os.getpid()), encoding="ascii")
            except OSError:
                self._force_clear_mutex()
                continue
            return

    def _release_mutex(self) -> None:
        pid_file = self.mutex_dir / "pid"
        try:
            owner = int(pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            owner = None
        if owner not in (None, os.getpid()):
            return
        self._force_clear_mutex()

    def _mutex_stale(self) -> bool:
        try:
            age = time.time() - self.mutex_dir.stat().st_mtime
        except OSError:
            return True
        pid_file = self.mutex_dir / "pid"
        if not pid_file.exists():
            return age > 5
        try:
            pid = int(pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return True
        return not pid_alive(pid)

    def _force_clear_mutex(self) -> None:
        pid_file = self.mutex_dir / "pid"
        for _ in range(10):
            try:
                pid_file.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.02)
        try:
            self.mutex_dir.rmdir()
        except OSError:
            return


class ResourceLockGroup:
    def __init__(self, manager: "ResourceLockManager", path: Path, task_id: str) -> None:
        self.manager = manager
        self.path = path
        self.task_id = task_id
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.manager.release(self)
        self.released = True


class ResourceLockManager:
    """命名资源锁。只做调度声明，不连接 MySQL、Redis、Milvus 或端口。"""

    _thread_gate = threading.Lock()

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.mutex_dir = self.root / "registry.mutex"

    def try_acquire_all(self, task_id: str, names: list[str]) -> tuple[ResourceLockGroup | None, str]:
        cleaned = [name.strip() for name in names if name.strip()]
        if not cleaned:
            raise LockError("资源锁需要至少一个名称")
        if len(set(name.casefold() for name in cleaned)) != len(cleaned):
            raise LockError("同一任务的资源锁名称重复")
        with self._thread_gate:
            return self._try_acquire_all(task_id, cleaned)

    def _try_acquire_all(self, task_id: str, names: list[str]) -> tuple[ResourceLockGroup | None, str]:
        self._acquire_mutex()
        handle_path: Path | None = None
        try:
            self._drop_dead_locks()
            wanted = {name.casefold() for name in names}
            for lock_path in self.root.glob("*.lock"):
                data = _read_lock(lock_path)
                if data is None:
                    continue
                held = {str(item).casefold() for item in data.get("resources") or []}
                overlap = wanted & held
                if overlap:
                    owner = data.get("task_id") or "未知任务"
                    label = "、".join(sorted(overlap))
                    return None, f"资源锁“{label}”被任务“{owner}”占用"
            handle_path = self.root / f"{uuid.uuid4().hex}.lock"
            dump_json(
                handle_path,
                {
                    "task_id": task_id,
                    "resources": names,
                    "pid": os.getpid(),
                    "created_at": now_iso(),
                },
            )
        finally:
            self._release_mutex()
        if handle_path is None:
            return None, "未能创建资源锁"
        return ResourceLockGroup(self, handle_path, task_id), ""

    def release(self, handle: ResourceLockGroup) -> None:
        with self._thread_gate:
            self._acquire_mutex()
            try:
                handle.path.unlink(missing_ok=True)
            finally:
                self._release_mutex()

    def _drop_dead_locks(self) -> None:
        for lock_path in self.root.glob("*.lock"):
            data = _read_lock(lock_path)
            pid = data.get("pid") if isinstance(data, dict) else None
            if not isinstance(pid, int) or not pid_alive(pid):
                lock_path.unlink(missing_ok=True)

    def _acquire_mutex(self) -> None:
        deadline = time.time() + 15
        while True:
            try:
                self.mutex_dir.mkdir()
            except FileExistsError:
                if self._mutex_stale():
                    self._force_clear_mutex()
                    continue
                if time.time() > deadline:
                    raise LockError("等待资源锁注册表超时")
                time.sleep(0.02)
                continue
            try:
                (self.mutex_dir / "pid").write_text(str(os.getpid()), encoding="ascii")
            except OSError:
                self._force_clear_mutex()
                continue
            return

    def _release_mutex(self) -> None:
        pid_file = self.mutex_dir / "pid"
        try:
            owner = int(pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            owner = None
        if owner not in (None, os.getpid()):
            return
        self._force_clear_mutex()

    def _mutex_stale(self) -> bool:
        try:
            age = time.time() - self.mutex_dir.stat().st_mtime
        except OSError:
            return True
        pid_file = self.mutex_dir / "pid"
        if not pid_file.exists():
            return age > 5
        try:
            pid = int(pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return True
        return not pid_alive(pid)

    def _force_clear_mutex(self) -> None:
        pid_file = self.mutex_dir / "pid"
        for _ in range(10):
            try:
                pid_file.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.02)
        try:
            self.mutex_dir.rmdir()
        except OSError:
            return


def default_resource_lock_dir() -> Path:
    from dual_agent import PROJECT_ROOT

    return PROJECT_ROOT / "runs" / "locks" / "resources"


def _read_lock(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data
