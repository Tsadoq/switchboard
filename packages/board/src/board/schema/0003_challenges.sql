DROP TABLE challenges;

CREATE TABLE challenges (
    challenge TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX challenges_expires_at ON challenges (expires_at);
