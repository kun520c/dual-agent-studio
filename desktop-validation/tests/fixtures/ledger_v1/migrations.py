"""Transactional, repeatable schema installation. No historical run migration."""
import sqlite3
from .model import GoalStatus, WorkStatus, AttemptStatus, SchemaError

SCHEMA_VERSION = 1


def choices(enum):
    return ','.join("'" + e.value + "'" for e in enum)


DDL = [
    'CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)',
    f'''CREATE TABLE goals (
        goal_id TEXT PRIMARY KEY, title TEXT NOT NULL, original_goal TEXT NOT NULL,
        constraints_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ({choices(GoalStatus)})),
        revision INTEGER NOT NULL CHECK(revision>=0), active_plan_revision INTEGER,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(goal_id,active_plan_revision) REFERENCES plans(goal_id,revision) DEFERRABLE INITIALLY DEFERRED)''',
    '''CREATE TABLE plans (plan_id TEXT UNIQUE NOT NULL, goal_id TEXT NOT NULL REFERENCES goals(goal_id),
        revision INTEGER NOT NULL CHECK(revision>0), parent_revision INTEGER, data TEXT NOT NULL,
        PRIMARY KEY(goal_id,revision),
        FOREIGN KEY(goal_id,parent_revision) REFERENCES plans(goal_id,revision),
        CHECK((revision=1 AND parent_revision IS NULL) OR parent_revision=revision-1))''',
    f'''CREATE TABLE work_items (work_item_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL, ordinal INTEGER NOT NULL, data TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ({choices(WorkStatus)})), generation INTEGER NOT NULL DEFAULT 0,
        current_attempt_id TEXT REFERENCES attempts(attempt_id) DEFERRABLE INITIALLY DEFERRED,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(goal_id,plan_revision,work_item_id), UNIQUE(goal_id,plan_revision,ordinal),
        FOREIGN KEY(goal_id,plan_revision) REFERENCES plans(goal_id,revision))''',
    '''CREATE TABLE dependencies (goal_id TEXT NOT NULL, plan_revision INTEGER NOT NULL,
        work_item_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
        PRIMARY KEY(work_item_id,dependency_id), CHECK(work_item_id<>dependency_id),
        FOREIGN KEY(goal_id,plan_revision,work_item_id) REFERENCES work_items(goal_id,plan_revision,work_item_id),
        FOREIGN KEY(goal_id,plan_revision,dependency_id) REFERENCES work_items(goal_id,plan_revision,work_item_id))''',
    f'''CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
        attempt_number INTEGER NOT NULL CHECK(attempt_number>0), generation INTEGER NOT NULL CHECK(generation>0),
        status TEXT NOT NULL CHECK(status IN ({choices(AttemptStatus)})), run_id TEXT NOT NULL UNIQUE,
        data TEXT NOT NULL, started_at TEXT, finished_at TEXT, failure_reason TEXT NOT NULL DEFAULT '',
        UNIQUE(work_item_id,attempt_number), UNIQUE(work_item_id,generation))''',
    '''CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
        data TEXT NOT NULL, verification_state TEXT NOT NULL CHECK(verification_state IN ('RECORDED','VALID','INVALIDATED')),
        invalidation_reason TEXT NOT NULL DEFAULT '')''',
    '''CREATE TABLE decisions (decision_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL, work_item_id TEXT REFERENCES work_items(work_item_id),
        attempt_id TEXT REFERENCES attempts(attempt_id), data TEXT NOT NULL,
        FOREIGN KEY(goal_id,plan_revision) REFERENCES plans(goal_id,revision))''',
    '''CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
        goal_id TEXT NOT NULL REFERENCES goals(goal_id), goal_revision INTEGER NOT NULL, kind TEXT NOT NULL,
        payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(goal_id,goal_revision))''',
    '''CREATE TABLE requests (idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response TEXT NOT NULL)''',
    'CREATE INDEX work_ready ON work_items(goal_id,plan_revision,status,ordinal)',
    'CREATE INDEX dependency_target ON dependencies(dependency_id)',
    'CREATE INDEX attempt_work ON attempts(work_item_id,status)',
    'CREATE INDEX evidence_attempt ON evidence(attempt_id)',
    'CREATE INDEX decision_goal ON decisions(goal_id,plan_revision)',
    '''CREATE TRIGGER immutable_plan_update BEFORE UPDATE ON plans BEGIN SELECT RAISE(ABORT,'Plans are immutable'); END''',
    '''CREATE TRIGGER immutable_plan_delete BEFORE DELETE ON plans BEGIN SELECT RAISE(ABORT,'Plans are immutable'); END''',
    '''CREATE TRIGGER immutable_goal_input BEFORE UPDATE OF original_goal,constraints_json ON goals
        BEGIN SELECT RAISE(ABORT,'Original goal is immutable'); END''',
    '''CREATE TRIGGER immutable_work_spec BEFORE UPDATE OF data,goal_id,plan_revision,ordinal ON work_items
        BEGIN SELECT RAISE(ABORT,'Work specification is immutable'); END''',
    '''CREATE TRIGGER immutable_events_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Events are append-only'); END''',
    '''CREATE TRIGGER immutable_events_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Events are append-only'); END''',
]


def migrate(connection: sqlite3.Connection, timestamp: str) -> None:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version not in {0, SCHEMA_VERSION}:
        raise SchemaError(f'Unsupported GoalLedger schema {version}; supported {SCHEMA_VERSION}')
    if version == 0:
        existing = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if existing:
            raise SchemaError('Refusing to overwrite an unversioned nonempty database')
        for statement in DDL:
            connection.execute(statement)
        connection.execute('INSERT INTO schema_migrations VALUES (?,?)', (SCHEMA_VERSION, timestamp))
        connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    elif [r[0] for r in connection.execute('SELECT version FROM schema_migrations ORDER BY version')] != [1]:
        raise SchemaError('Schema version and migration history disagree')
