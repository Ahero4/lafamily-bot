"""Настройки и сохранённое расписание шестичасовых бэкапов."""
from datetime import UTC, datetime, timedelta

from lafamily.database_archives import snapshot
from lafamily.modules.events.time import timestamp


class BackupStore:
    def init_database_backups(self):
        self.db.execute('''CREATE TABLE IF NOT EXISTS database_backup_settings (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, next_at TEXT,
            last_attempt TEXT, state TEXT NOT NULL DEFAULT 'idle', message_id TEXT)''')
        if 'last_success_at' not in {r['name'] for r in self.db.execute('PRAGMA table_info(database_backup_settings)')}:
            self.db.execute('ALTER TABLE database_backup_settings ADD COLUMN last_success_at TEXT')
            self.db.execute("UPDATE database_backup_settings SET last_success_at=last_attempt WHERE state='sent' AND message_id IS NOT NULL")

    def database_backup_settings(self, guild_id):
        row = self.db.execute('SELECT * FROM database_backup_settings WHERE guild_id=?', (guild_id,)).fetchone()
        return dict(row) if row else None

    def configure_database_backups(self, guild_id, actor_id, channel_id, now=None):
        now = now or datetime.now(UTC)
        with self.transaction():
            self.ensure_channel_separation(guild_id, channel_id)
            self.db.execute('''INSERT INTO database_backup_settings(guild_id,channel_id,next_at) VALUES (?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,
                next_at=excluded.next_at,state='idle',last_attempt=NULL''',
                (guild_id, channel_id, timestamp(now) if channel_id else None))
            self.audit(guild_id,actor_id,'database.backups.configured',{'channel_id':channel_id})

    def claim_database_backup(self, guild_id, now=None, force=False):
        now = now or datetime.now(UTC)
        with self.transaction():
            settings = self.database_backup_settings(guild_id)
            if not settings or not settings['channel_id']:
                return None
            if not force and settings['next_at'] and settings['next_at'] > timestamp(now):
                return None
            attempt = timestamp(now)
            self.db.execute("UPDATE database_backup_settings SET next_at=?,last_attempt=?,state='pending' WHERE guild_id=?",
                            (timestamp(now+timedelta(hours=6)),attempt,guild_id))
            return {**settings,'last_attempt':attempt}

    def finish_database_backup(self, guild_id, attempt, state, message_id=None):
        self.db.execute('UPDATE database_backup_settings SET state=?,message_id=? WHERE guild_id=? AND last_attempt=?',
                        (state,message_id,guild_id,attempt))
        if state=='sent':
            self.db.execute('UPDATE database_backup_settings SET last_success_at=? WHERE guild_id=? AND last_attempt=?',
                            (timestamp(),guild_id,attempt))

    def export_database(self, target):
        snapshot(self.db,target)
