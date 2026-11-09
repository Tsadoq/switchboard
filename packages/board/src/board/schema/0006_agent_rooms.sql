ALTER TABLE subforums ADD COLUMN created_by TEXT;
ALTER TABLE subforums ADD COLUMN created_at REAL;
ALTER TABLE subforums ADD COLUMN archived_at REAL;

CREATE TABLE room_events (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL REFERENCES subforums (slug),
    action TEXT NOT NULL CHECK (action IN ('create', 'archive', 'unarchive')),
    principal TEXT NOT NULL,
    at REAL NOT NULL
);

CREATE INDEX subforums_created_by ON subforums (created_by, created_at);
