CREATE TABLE night_context_reads (
    run_id TEXT PRIMARY KEY REFERENCES night_runs(id),
    reads INTEGER NOT NULL DEFAULT 0 CHECK (reads >= 0),
    bytes INTEGER NOT NULL DEFAULT 0 CHECK (bytes >= 0)
);
