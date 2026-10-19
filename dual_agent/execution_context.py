"""Backend-only run reservation/lifecycle capability, never a TaskConfig DTO."""
from __future__ import annotations
from pathlib import Path
import threading
from typing import Callable

from dual_agent.models import TaskConfig, ConfigError
from dual_agent.safety import finalize_declared, is_reparse_point, same_path


class ExecutionContext:
    def __init__(self, runs: Path, task_id: str, schedule_id: str, run_id: str,
                 observer: Callable, preparer: Callable | None = None):
        self.runs, self.task_id, self.schedule_id, self.run_id = Path(runs), task_id, schedule_id, run_id
        self.observer = observer
        self.preparer = preparer
        self.started = False
        self.preparing = False
        self.result = None
        self.error = ''
        self._claimed = False
        self._guard = threading.Lock()

    def claim(self, config: TaskConfig) -> Path:
        with self._guard:
            if self._claimed or config.task_id != self.task_id or config.schedule_id != self.schedule_id:
                raise ConfigError('Reserved execution identity is mismatched or already consumed')
            if not same_path(Path(config.runs_dir), self.runs):
                raise ConfigError('Reserved run belongs to another deployment')
            parts = self.run_id.split('/')
            if len(parts) != 2 or parts[0] != self.task_id or any(p in {'', '.', '..'} for p in parts):
                raise ConfigError('Invalid reserved run identity')
            folder = self.runs / self.run_id
            if not folder.is_dir() or is_reparse_point(folder) or not same_path(finalize_declared(str(folder)), folder):
                raise ConfigError('Reserved run directory is missing or redirected')
            # Exclusive marker is durable. A restart cannot consume this run again.
            with (folder / 'execution.claim').open('x', encoding='utf8') as stream:
                stream.write(self.schedule_id)
                stream.flush()
                __import__('os').fsync(stream.fileno())
            self._claimed = True
            return folder

    def entered(self):
        self.started = True  # Set only after Coordinator has obtained its execution locks.

    def prepare(self, config, run_dir):
        self.preparing = True
        try:
            if self.preparer is not None:
                self.preparer(config, run_dir)
        finally:
            self.preparing = False

    def observe(self, config, state, run_dir, write_lock, resource_lock):
        self.observer(config, state, run_dir, write_lock, resource_lock)

    def finished(self, result):
        self.result = result  # Direct in-process Coordinator result, never rehydrated JSON.
