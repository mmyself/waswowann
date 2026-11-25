import sqlite3


def move_observation_contents(conn: sqlite3.Connection, target_id: str, source_ids: list[str], updated_at_ms: int) -> None:
    target = conn.execute('SELECT * FROM observations WHERE id=?', (target_id,)).fetchone()
    title, category, environment = target['title'], target['category'], target['environment']
    descriptions = [target['description']] if target['description'] else []
    for source_id in source_ids:
        source = conn.execute('SELECT * FROM observations WHERE id=?', (source_id,)).fetchone()
        if source['description'] and source['description'] not in descriptions:
            descriptions.append(source['description'])
        if not title or title == 'Unbekannt' or title.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic')):
            title = source['title'] or title
        if category == 'Unbekannt':
            category = source['category']
        if environment == 'unknown':
            environment = source['environment']
        conn.execute('UPDATE media SET obs_id=? WHERE obs_id=?', (target_id, source_id))
        conn.execute('UPDATE gemini_notes SET obs_id=? WHERE obs_id=?', (target_id, source_id))
        # A running worker still has the old observation ID; duplicate queued jobs cannot both move.
        conn.execute('''UPDATE processing_jobs AS source
            SET status='failed', error_code='OBSERVATION_MERGED', updated_at_ms=?
            WHERE obs_id=? AND (status='running' OR (status='queued' AND EXISTS (
                SELECT 1 FROM processing_jobs AS target WHERE target.obs_id=? AND target.kind=source.kind
                AND target.input_key=source.input_key AND target.status IN ('queued', 'running')
            )))''', (updated_at_ms, source_id, target_id))
        conn.execute('''UPDATE gemini_notes SET status='error', error_code='OBSERVATION_MERGED'
            WHERE status IN ('queued', 'running') AND id IN (
                SELECT note_id FROM processing_jobs WHERE obs_id=? AND error_code='OBSERVATION_MERGED'
            )''', (source_id,))
        conn.execute('UPDATE processing_jobs SET obs_id=? WHERE obs_id=?', (target_id, source_id))
        conn.execute('DELETE FROM observations WHERE id=?', (source_id,))
    conn.execute('UPDATE observations SET title=?, category=?, environment=?, description=?, updated_at_ms=? WHERE id=?',
                 (title, category, environment, '\n\n'.join(descriptions), updated_at_ms, target_id))
