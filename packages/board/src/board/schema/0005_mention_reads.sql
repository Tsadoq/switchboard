CREATE TABLE mention_reads (
    post_id INTEGER NOT NULL,
    principal TEXT NOT NULL,
    read_at REAL NOT NULL,
    PRIMARY KEY (post_id, principal),
    FOREIGN KEY (post_id, principal) REFERENCES mentions (post_id, principal) ON DELETE CASCADE
);
