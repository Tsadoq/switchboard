CREATE TABLE self_wakes (
    id INTEGER PRIMARY KEY,
    principal TEXT NOT NULL,
    thread_id INTEGER NOT NULL REFERENCES threads (id),
    due_at REAL NOT NULL,
    note TEXT NOT NULL,
    created_at REAL NOT NULL,
    fired_at REAL,
    cancelled_at REAL,
    CHECK (fired_at IS NULL OR cancelled_at IS NULL)
);

CREATE INDEX self_wakes_pending ON self_wakes (due_at) WHERE fired_at IS NULL AND cancelled_at IS NULL;
CREATE INDEX self_wakes_by_principal ON self_wakes (principal) WHERE fired_at IS NULL AND cancelled_at IS NULL;
