CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('global', 'entity')),
    scope_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    actions_json TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_scope_time
    ON chat_messages(scope_type, scope_id, created_at_ms, id);
