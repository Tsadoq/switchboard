ALTER TABLE threads ADD COLUMN last_post_at REAL;

UPDATE threads SET last_post_at = (SELECT MAX(created_at) FROM posts WHERE thread_id = threads.id);

CREATE INDEX threads_subforum_activity ON threads (subforum, last_post_at DESC, id DESC);

CREATE TRIGGER posts_activity_insert AFTER INSERT ON posts BEGIN
    UPDATE threads
    SET last_post_at = MAX(COALESCE(last_post_at, NEW.created_at), NEW.created_at)
    WHERE id = NEW.thread_id;
END;

CREATE TRIGGER posts_activity_delete AFTER DELETE ON posts BEGIN
    UPDATE threads
    SET last_post_at = (SELECT MAX(created_at) FROM posts WHERE thread_id = OLD.thread_id)
    WHERE id = OLD.thread_id;
END;

CREATE TRIGGER posts_activity_update AFTER UPDATE OF created_at, thread_id ON posts BEGIN
    UPDATE threads
    SET last_post_at = (SELECT MAX(created_at) FROM posts WHERE thread_id = threads.id)
    WHERE id IN (OLD.thread_id, NEW.thread_id);
END;
