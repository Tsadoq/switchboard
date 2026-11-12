CREATE TABLE night_capture_aborts (
    capture_receipt_id TEXT PRIMARY KEY,
    abort_receipt_id TEXT NOT NULL UNIQUE REFERENCES night_verifier_receipts(receipt_id),
    box TEXT NOT NULL,
    journal_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES night_runs(id),
    capture_digest TEXT NOT NULL,
    created_at REAL NOT NULL
);
