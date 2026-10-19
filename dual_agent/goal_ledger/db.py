"""Short-lived SQLite connections; one BEGIN IMMEDIATE per domain mutation."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator

from dual_agent.safety import finalize_declared, is_reparse_point, same_path
from .migrations import migrate
from .model import StorageError, StorageBusyError


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def exact_local_path(path: Path) -> Path:
    if not path.is_absolute() or str(path).startswith('\\\\'):
        raise StorageError('Backend state root must be an absolute local path')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part in {'.', '..'} or ':' in part:
            raise StorageError('State path must be normalized')
        current /= part
        if is_reparse_point(current) or current.is_symlink():
            raise StorageError(f'State path cannot traverse a reparse point: {current}')
    if not same_path(finalize_declared(str(path)), path):
        raise StorageError('State path resolves to a different target')
    return path


class Database:
    """runs_root comes only from trusted backend configuration, never a proposal."""
    def __init__(self, runs_root: Path, *, busy_timeout_ms: int = 3000):
        if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 30000:
            raise StorageError('Invalid SQLite busy timeout')
        self.root = exact_local_path(Path(runs_root))
        self.path = self.root / 'studio-host' / 'goal-ledger' / 'ledger.sqlite3'
        self.busy_timeout_ms = busy_timeout_ms
        try:
            self._check_paths()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connection() as connection:
                # The WAL setting persists; FULL sync preserves committed transactions.
                if connection.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                    raise StorageError('GoalLedger requires SQLite WAL mode')
                connection.execute('BEGIN IMMEDIATE')
                migrate(connection, now())
                connection.commit()
        except OSError as exc:
            raise StorageError(f'Cannot initialize GoalLedger at {self.path}: {exc}') from exc
        problems = self.integrity_check()
        if problems:
            raise StorageError('GoalLedger integrity check failed: ' + '; '.join(problems))

    def _check_paths(self) -> None:
        for suffix in ('', '-wal', '-shm', '-journal'):
            exact_local_path(Path(str(self.path) + suffix))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            self._check_paths()
            connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute(f'PRAGMA busy_timeout={self.busy_timeout_ms}')
            yield connection
        except sqlite3.Error as exc:
            message = f'GoalLedger SQLite error at {self.path}: {exc}'
            if getattr(exc, 'sqlite_errorcode', 0) & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise StorageBusyError(message) from exc
            raise StorageError(message) from exc
        except OSError as exc:
            raise StorageError(f'GoalLedger path access error at {self.path}: {exc}') from exc
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute('BEGIN')
            yield connection

    def integrity_check(self) -> list[str]:
        with self.read() as connection:
            checks = [r[0] for r in connection.execute('PRAGMA integrity_check')]
            return [v for v in checks if v != 'ok'] + [str(tuple(r)) for r in connection.execute('PRAGMA foreign_key_check')]
