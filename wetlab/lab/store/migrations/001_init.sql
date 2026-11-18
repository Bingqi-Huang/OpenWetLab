-- Initial schema for WetLab island store (docs/spec/04 section 5.13).

CREATE TABLE missions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    input_text TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('real', 'virtual')),
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    conclusion TEXT
);

CREATE TABLE experiments (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    n INTEGER NOT NULL,
    task_json TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT,
    UNIQUE (mission_id, n)
);

CREATE TABLE protocols (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    version INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    rehearsal_json TEXT,
    text_md TEXT,
    status TEXT NOT NULL,
    submitted_at REAL NOT NULL,
    decided_by TEXT,
    decided_at REAL,
    reject_comment TEXT,
    UNIQUE (experiment_id, version)
);

CREATE TABLE steps (
    protocol_id TEXT NOT NULL REFERENCES protocols(id),
    n INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content_json TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL,
    finished_at REAL,
    result_json TEXT,
    wait_deadline REAL,
    PRIMARY KEY (protocol_id, n)
);

CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    protocol_id TEXT REFERENCES protocols(id),
    step_n INTEGER,
    device TEXT NOT NULL,
    action TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    started_at REAL NOT NULL,
    elapsed_s REAL NOT NULL,
    CHECK (step_n IS NULL OR protocol_id IS NOT NULL),
    FOREIGN KEY (protocol_id, step_n) REFERENCES steps(protocol_id, n)
);

CREATE INDEX idx_operations_device_action_seq
    ON operations (device, action, seq DESC);

CREATE INDEX idx_operations_protocol_step
    ON operations (protocol_id, step_n, seq ASC);

CREATE TABLE labware (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    position TEXT UNIQUE
);

CREATE TABLE labware_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    labware_id TEXT NOT NULL REFERENCES labware(id),
    at REAL NOT NULL,
    what TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    operation_id TEXT REFERENCES operations(id) DEFERRABLE INITIALLY DEFERRED,
    by TEXT NOT NULL
);

CREATE INDEX idx_labware_events_labware_id
    ON labware_events (labware_id, id ASC);

CREATE TABLE device_state (
    device TEXT PRIMARY KEY,
    props_json TEXT NOT NULL,
    error TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE freeze (
    frozen INTEGER NOT NULL CHECK (frozen IN (0, 1)),
    reason TEXT,
    operation_id TEXT REFERENCES operations(id) DEFERRABLE INITIALLY DEFERRED,
    at REAL
);

INSERT INTO freeze (frozen, reason, operation_id, at)
VALUES (0, NULL, NULL, NULL);

CREATE TRIGGER freeze_no_insert
BEFORE INSERT ON freeze
WHEN (SELECT COUNT(*) FROM freeze) >= 1
BEGIN
    SELECT RAISE(ABORT, 'freeze table must contain exactly one row');
END;

CREATE TRIGGER freeze_no_delete
BEFORE DELETE ON freeze
BEGIN
    SELECT RAISE(ABORT, 'freeze row cannot be deleted');
END;

CREATE TABLE commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    at REAL NOT NULL,
    who TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_id TEXT,
    payload_json TEXT NOT NULL,
    handled_at REAL,
    outcome TEXT
);

CREATE INDEX idx_commands_unhandled
    ON commands (id ASC) WHERE handled_at IS NULL;

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    ref_id TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE cursors (
    name TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL DEFAULT 0 CHECK (last_event_id >= 0)
);
