"""Отдельный реестр личных сообщений скупщика, без поиска среди остальных ЛС."""


class PriceDmStore:
    def init_price_dms(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS price_dm_current (
                guild_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS price_dm_messages (
                attempt_id INTEGER PRIMARY KEY REFERENCES price_delivery_attempts(id),
                bot_id TEXT NOT NULL, channel_id TEXT NOT NULL, message_id TEXT NOT NULL,
                revision TEXT NOT NULL, content_hash TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0, cleanup_error TEXT,
                UNIQUE(bot_id,channel_id,message_id));
        """)

    def activate_price_dm_revision(self, guild_id, source_id, revision):
        """Старая ручная обработка не делает актуальные ЛС устаревшими."""
        with self.transaction():
            current = self.db.execute('SELECT source_id FROM price_dm_current WHERE guild_id=?',
                                      (guild_id,)).fetchone()
            cursor = self.db.execute('SELECT source_id FROM price_alert_cursor WHERE guild_id=?',
                                     (guild_id,)).fetchone()
            publications = self.db.execute("SELECT source_id FROM price_publications "
                "WHERE guild_id=? AND state='published'", (guild_id,)).fetchall()
            if any(int(row['source_id']) > int(source_id)
                   for row in [current, cursor, *publications] if row):
                return False
            self.db.execute('INSERT INTO price_dm_current VALUES (?,?,?) ON CONFLICT(guild_id) '
                'DO UPDATE SET source_id=excluded.source_id,revision=excluded.revision',
                (guild_id, source_id, revision))
            self.db.execute('''UPDATE price_dm_messages SET stale=1 WHERE stale=0 AND attempt_id IN (
                SELECT a.id FROM price_delivery_attempts a JOIN price_delivery_runs r ON r.id=a.run_id
                WHERE r.guild_id=? AND (r.source_id<>? OR price_dm_messages.revision<>?))''',
                (guild_id, source_id, revision))
        return True

    def remember_price_dm(self, attempt_id, bot_id, channel_id, message_id, revision, content_hash):
        # Даже если актуальные цены успели смениться, сообщение попадает в очередь удаления.
        self.db.execute('''INSERT INTO price_dm_messages
            (attempt_id,bot_id,channel_id,message_id,revision,content_hash,stale)
            SELECT a.id,?,?,?,?,?, CASE WHEN c.source_id=r.source_id AND c.revision=? THEN 0 ELSE 1 END
            FROM price_delivery_attempts a JOIN price_delivery_runs r ON r.id=a.run_id
            LEFT JOIN price_dm_current c ON c.guild_id=r.guild_id WHERE a.id=?''',
            (bot_id, channel_id, message_id, revision, content_hash, revision, attempt_id))

    def stale_price_dms(self, guild_id):
        return [dict(row) for row in self.db.execute('''SELECT m.*,a.user_id FROM price_dm_messages m
            JOIN price_delivery_attempts a ON a.id=m.attempt_id JOIN price_delivery_runs r ON r.id=a.run_id
            WHERE r.guild_id=? AND m.stale=1 ORDER BY m.attempt_id''', (guild_id,))]

    def finish_price_dm_cleanup(self, attempt_id, error_type=None, *, protected=False):
        if error_type is None:
            self.db.execute('DELETE FROM price_dm_messages WHERE attempt_id=?', (attempt_id,))
        else:
            self.db.execute('UPDATE price_dm_messages SET cleanup_error=?,stale=? WHERE attempt_id=?',
                            (error_type, 2 if protected else 1, attempt_id))
