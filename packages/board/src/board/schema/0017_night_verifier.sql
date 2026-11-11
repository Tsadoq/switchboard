CREATE TABLE night_verifier_state (
    box TEXT PRIMARY KEY,
    journal_id TEXT NOT NULL,
    counter INTEGER NOT NULL DEFAULT 0 CHECK(counter >= 0)
);
CREATE TABLE night_verifier_bindings (
    run_id TEXT PRIMARY KEY REFERENCES night_runs(id),
    journal_id TEXT NOT NULL,
    counter INTEGER NOT NULL,
    boot_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    cgroup TEXT NOT NULL,
    captured_at REAL NOT NULL
);
CREATE TABLE night_verifier_receipts (
    receipt_id TEXT PRIMARY KEY,
    box TEXT NOT NULL,
    journal_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at REAL NOT NULL
);
