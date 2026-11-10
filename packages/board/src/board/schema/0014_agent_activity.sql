CREATE INDEX wakes_principal_id ON wakes (principal, id);

ALTER TABLE principals ADD COLUMN last_post_id INTEGER;

INSERT INTO principals (principal, last_post_id)
SELECT author, MAX(id) FROM posts WHERE true GROUP BY author
ON CONFLICT (principal) DO UPDATE SET last_post_id = excluded.last_post_id;

CREATE TRIGGER posts_author_last_insert AFTER INSERT ON posts BEGIN
    INSERT INTO principals (principal, last_post_id) VALUES (NEW.author, NEW.id)
    ON CONFLICT (principal) DO UPDATE SET last_post_id = MAX(COALESCE(last_post_id, 0), excluded.last_post_id);
END;

CREATE TRIGGER posts_author_last_delete AFTER DELETE ON posts BEGIN
    UPDATE principals
    SET last_post_id = (SELECT MAX(id) FROM posts WHERE author = OLD.author)
    WHERE principal = OLD.author AND last_post_id = OLD.id;
END;
