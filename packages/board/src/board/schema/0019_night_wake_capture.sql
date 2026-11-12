CREATE TABLE night_wake_captures (
    receipt_id TEXT PRIMARY KEY REFERENCES night_verifier_receipts (receipt_id),
    run_id TEXT NOT NULL REFERENCES night_runs (id),
    captured_at REAL NOT NULL
);

CREATE TABLE night_captured_wakes (
    capture_id TEXT NOT NULL REFERENCES night_wake_captures (receipt_id),
    wake_id INTEGER NOT NULL REFERENCES wakes (id),
    PRIMARY KEY (capture_id, wake_id)
);

CREATE INDEX night_captured_wake_history ON night_captured_wakes (wake_id, capture_id);
CREATE INDEX night_captures_run ON night_wake_captures (run_id);
