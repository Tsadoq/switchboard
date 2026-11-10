ALTER TABLE threads ADD COLUMN topic_id INTEGER REFERENCES threads (id);
ALTER TABLE wakes ADD COLUMN thread_id INTEGER;
ALTER TABLE wakes ADD COLUMN topic_id INTEGER;

UPDATE wakes
SET thread_id = (SELECT posts.thread_id FROM posts WHERE wakes.source_ref = 'post:' || posts.id)
WHERE source_ref LIKE 'post:%';

UPDATE wakes
SET topic_id = (SELECT COALESCE(threads.topic_id, threads.id) FROM threads WHERE threads.id = wakes.thread_id)
WHERE thread_id IS NOT NULL;
