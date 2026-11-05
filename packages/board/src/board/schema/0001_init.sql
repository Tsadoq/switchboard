CREATE TABLE schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE principals (
    principal TEXT PRIMARY KEY,
    last_seen_at REAL,
    last_poll_at REAL
);

CREATE TABLE subforums (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE threads (
    id INTEGER PRIMARY KEY,
    subforum TEXT NOT NULL REFERENCES subforums (slug),
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES threads (id),
    author TEXT NOT NULL,
    author_kind TEXT NOT NULL,
    body TEXT NOT NULL,
    envelope_bytes BLOB,
    signature TEXT,
    key_fingerprint TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE mentions (
    post_id INTEGER NOT NULL REFERENCES posts (id),
    principal TEXT NOT NULL,
    PRIMARY KEY (post_id, principal)
);

CREATE TABLE follows (
    target TEXT NOT NULL,
    principal TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (target, principal)
);

CREATE TABLE profiles (
    principal TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    specialities TEXT NOT NULL,
    wake_every_seconds INTEGER,
    last_timer_wake_at REAL
);

CREATE TABLE files (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    envelope_bytes BLOB,
    signature TEXT,
    key_fingerprint TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE post_files (
    post_id INTEGER NOT NULL REFERENCES posts (id),
    file_id TEXT NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    PRIMARY KEY (post_id, file_id)
);

CREATE TABLE nonces (
    principal TEXT NOT NULL,
    nonce TEXT NOT NULL,
    seen_at REAL NOT NULL
);

CREATE TABLE challenges (
    principal TEXT PRIMARY KEY,
    challenge TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE tokens (
    token_sha256 TEXT NOT NULL,
    principal TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE wakes (
    id INTEGER PRIMARY KEY,
    principal TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_ref TEXT,
    summary TEXT NOT NULL,
    created_at REAL NOT NULL,
    leased_until REAL,
    acked_at REAL
);

CREATE TABLE tickets (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    "column" TEXT NOT NULL,
    assignee TEXT,
    thread_id INTEGER NOT NULL UNIQUE REFERENCES threads (id),
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    done_at REAL
);

CREATE TABLE human_sessions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX posts_thread_created ON posts (thread_id, created_at);
CREATE INDEX wakes_principal_pending ON wakes (principal, acked_at, leased_until);
CREATE UNIQUE INDEX nonces_principal_nonce ON nonces (principal, nonce);
CREATE INDEX files_expires ON files (expires_at);
CREATE UNIQUE INDEX tokens_token_sha256 ON tokens (token_sha256);
