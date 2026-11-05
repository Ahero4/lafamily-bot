"""Текущий источник и редакция цен для очереди рассылки."""


class PriceDmStore:
    def init_price_dms(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS price_dm_current (
                guild_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision TEXT NOT NULL)
        """)

    def activate_price_dm_revision(self, guild_id, source_id, revision):
        """Сохранить текущую редакцию, если источник ещё актуален."""
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
        return True
