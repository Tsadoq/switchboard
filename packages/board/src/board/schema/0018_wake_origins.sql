CREATE TABLE night_routing_days (
    principal TEXT NOT NULL,
    local_date TEXT NOT NULL,
    starts_at REAL NOT NULL,
    ends_at REAL NOT NULL,
    PRIMARY KEY (principal, local_date)
);

CREATE TABLE night_held_wakes (
    wake_id INTEGER PRIMARY KEY REFERENCES wakes (id),
    local_date TEXT NOT NULL,
    held_at REAL NOT NULL
);

ALTER TABLE wakes ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'budgeted'
    CHECK (origin_kind IN ('human', 'budgeted'));

UPDATE wakes SET origin_kind = 'human'
WHERE reason IN ('mention', 'follow') AND EXISTS (
    SELECT 1 FROM posts
    WHERE posts.id = CAST(substr(wakes.source_ref, 6) AS INTEGER)
      AND wakes.source_ref = 'post:' || CAST(posts.id AS TEXT)
      AND posts.author = 'human'
      AND posts.author_kind IN ('human', 'system')
      AND (wakes.thread_id IS NULL OR wakes.thread_id = posts.thread_id)
);
