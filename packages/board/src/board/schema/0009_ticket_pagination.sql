CREATE INDEX tickets_column_id ON tickets ("column", id);
CREATE INDEX tickets_column_done_id ON tickets ("column", done_at DESC, id DESC);
CREATE INDEX tickets_assignee_id ON tickets (assignee, id);
CREATE INDEX tickets_column_assignee_id ON tickets ("column", assignee, id);
