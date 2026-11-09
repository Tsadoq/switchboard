CREATE TABLE IF NOT EXISTS follow_optouts (
    target TEXT NOT NULL,
    principal TEXT NOT NULL,
    PRIMARY KEY (target, principal)
);

INSERT INTO follows (target, principal, created_at)
SELECT 'thread:' || thread_id, author, MIN(created_at)
FROM posts
WHERE author_kind = 'agent'
  AND NOT EXISTS (
      SELECT 1 FROM follow_optouts
      WHERE target = 'thread:' || posts.thread_id AND principal = posts.author
  )
GROUP BY thread_id, author
ON CONFLICT (target, principal) DO NOTHING;
