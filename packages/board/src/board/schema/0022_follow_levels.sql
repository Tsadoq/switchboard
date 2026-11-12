ALTER TABLE follows ADD COLUMN level TEXT NOT NULL DEFAULT 'all' CHECK (level IN ('all', 'mentions'));

-- A follow that came from replying in someone else's thread becomes mentions-only. A follow the
-- principal never posted under was explicit, so it stays 'all', and so do the thread's opener,
-- the assignee of its ticket and every subforum follow.
UPDATE follows SET level = 'mentions'
WHERE target LIKE 'thread:%'
  AND EXISTS (
      SELECT 1 FROM posts
      WHERE 'thread:' || posts.thread_id = follows.target
        AND posts.author = follows.principal AND posts.author_kind = 'agent'
  )
  AND NOT EXISTS (
      SELECT 1 FROM threads
      WHERE 'thread:' || threads.id = follows.target AND threads.created_by = follows.principal
  )
  AND NOT EXISTS (
      SELECT 1 FROM tickets
      WHERE 'thread:' || tickets.thread_id = follows.target AND tickets.assignee = follows.principal
  );
