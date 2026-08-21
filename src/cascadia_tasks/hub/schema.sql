PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    node        TEXT,
    backend     TEXT,
    tags        TEXT NOT NULL DEFAULT '[]',
    token_hash  TEXT,
    last_seen   REAL NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id              TEXT PRIMARY KEY,
    topic           TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    open            INTEGER NOT NULL DEFAULT 0,
    policy_json     TEXT NOT NULL DEFAULT '{}',
    archived        INTEGER NOT NULL DEFAULT 0,
    archived_reason TEXT,
    dm_key          TEXT UNIQUE,
    floor_holder    TEXT,
    next_seq        INTEGER NOT NULL DEFAULT 1,
    message_count   INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    last_activity   REAL NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS room_members (
    room_id       TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    agent         TEXT NOT NULL,
    ring_pos      INTEGER NOT NULL DEFAULT 0,
    last_read_seq INTEGER NOT NULL DEFAULT 0,
    joined_at     REAL NOT NULL,
    PRIMARY KEY (room_id, agent)
);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    room_id       TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    sender        TEXT NOT NULL,
    body          TEXT NOT NULL,
    data_json     TEXT,
    reply_to      TEXT,
    mentions_json TEXT NOT NULL DEFAULT '[]',
    client_msg_id TEXT,
    created_at    REAL NOT NULL,
    UNIQUE (room_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idem
    ON messages (sender, client_msg_id) WHERE client_msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_room_seq ON messages (room_id, seq);

CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    spec         TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    state        TEXT NOT NULL,
    assignee     TEXT,
    selector     TEXT,
    priority     INTEGER NOT NULL DEFAULT 0,
    timeout_s    REAL NOT NULL,
    dedupe_key   TEXT UNIQUE,
    room_id      TEXT REFERENCES rooms(id) ON DELETE SET NULL,
    progress_pct REAL,
    progress_msg TEXT,
    result_json  TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    claimed_at   REAL,
    finished_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks (state, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks (assignee, state);

CREATE TABLE IF NOT EXISTS task_events (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events (task_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    name       TEXT,
    mime       TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
