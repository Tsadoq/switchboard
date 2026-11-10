CREATE TABLE night_control (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1))
);
INSERT INTO night_control (singleton, enabled) VALUES (1, 0);

CREATE TABLE night_enrollment (
    principal TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);

CREATE TABLE night_budget (
    principal TEXT NOT NULL,
    local_date TEXT NOT NULL,
    starts_at REAL NOT NULL,
    ends_at REAL NOT NULL,
    spent REAL NOT NULL DEFAULT 0 CHECK (spent >= 0),
    reserved REAL NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    PRIMARY KEY (principal, local_date)
);

CREATE TABLE night_runs (
    id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    box TEXT NOT NULL,
    local_date TEXT NOT NULL,
    fence TEXT NOT NULL UNIQUE,
    admitted_at REAL NOT NULL,
    started_at REAL,
    deadline REAL NOT NULL,
    reserved REAL NOT NULL CHECK (reserved > 0),
    charged REAL,
    state TEXT NOT NULL CHECK (state IN ('admitted', 'running', 'stopping', 'quarantined', 'finished')),
    FOREIGN KEY (principal, local_date) REFERENCES night_budget (principal, local_date)
);
CREATE UNIQUE INDEX night_run_principal_active ON night_runs (principal) WHERE state != 'finished';
CREATE UNIQUE INDEX night_run_box_active ON night_runs (box) WHERE state != 'finished';
CREATE INDEX night_run_state ON night_runs (state);

CREATE TABLE night_initial_slots (
    principal TEXT NOT NULL,
    local_date TEXT NOT NULL,
    slot_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('fired', 'skipped')),
    PRIMARY KEY (principal, local_date)
);
