CREATE TABLE human_asks (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts (id),
    asker TEXT NOT NULL,
    ask TEXT NOT NULL,
    created_at REAL NOT NULL,
    closed_at REAL,
    closed_by TEXT,
    outcome TEXT,
    CHECK ((closed_at IS NULL) = (closed_by IS NULL) AND (closed_at IS NULL) = (outcome IS NULL))
);

CREATE UNIQUE INDEX human_asks_one_open_per_post ON human_asks (post_id) WHERE closed_at IS NULL;
CREATE INDEX human_asks_open ON human_asks (created_at) WHERE closed_at IS NULL;
CREATE INDEX human_asks_closed ON human_asks (closed_at) WHERE closed_at IS NOT NULL;
