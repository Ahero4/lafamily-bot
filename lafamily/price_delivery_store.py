"""Журнал доставки DM скупщика; методы вызываются в потоке DatabaseWorker."""
from lafamily.errors import UserError
from lafamily.modules.events.time import timestamp


class PriceDeliveryStore:
    def init_price_delivery(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS admin_report_settings (
                guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS price_delivery_runs (
                id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                source_id TEXT NOT NULL, created_at TEXT NOT NULL, subscribers INTEGER NOT NULL,
                admin_state TEXT NOT NULL DEFAULT 'pending', admin_message_id TEXT);
            CREATE TABLE IF NOT EXISTS price_delivery_attempts (
                id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES price_delivery_runs(id),
                user_id TEXT NOT NULL, item TEXT NOT NULL, threshold TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', error_type TEXT, completed_at TEXT,
                UNIQUE(run_id,user_id,item));
            CREATE INDEX IF NOT EXISTS price_delivery_period ON price_delivery_runs(guild_id,created_at);
        """)

    def admin_report_channel(self, guild_id):
        row = self.db.execute('SELECT channel_id FROM admin_report_settings WHERE guild_id=?', (guild_id,)).fetchone()
        return row['channel_id'] if row else None

    def configure_admin_report_channel(self, guild_id, actor_id, channel_id):
        with self.transaction():
            if channel_id is None:
                self.db.execute('DELETE FROM admin_report_settings WHERE guild_id=?', (guild_id,))
            else:
                self.db.execute('INSERT INTO admin_report_settings VALUES (?,?) ON CONFLICT(guild_id) '
                                'DO UPDATE SET channel_id=excluded.channel_id', (guild_id, channel_id))
            self.audit(guild_id, actor_id, 'admin.report.channel', {'channel_id': channel_id})

    def track_price_alerts(self, guild_id, source_id, alerts):
        """Внутри транзакции claim: флаги и pending-попытки фиксируются атомарно."""
        if not alerts:
            return alerts
        channel = self.price_settings(guild_id)
        count = self.db.execute('SELECT COUNT(DISTINCT user_id) FROM price_subscriptions WHERE guild_id=?',
                                (guild_id,)).fetchone()[0]
        run_id = self.db.execute('INSERT INTO price_delivery_runs '
            '(guild_id,channel_id,source_id,created_at,subscribers) VALUES (?,?,?,?,?)',
            (guild_id, channel['channel_id'] if channel else '', source_id, timestamp(), count)).lastrowid
        for alert in alerts:
            attempt_id = self.db.execute('INSERT INTO price_delivery_attempts '
                '(run_id,user_id,item,threshold) VALUES (?,?,?,?)',
                (run_id, alert['user_id'], alert['item'], alert['threshold'])).lastrowid
            alert.update(run_id=run_id, attempt_id=attempt_id)
        return alerts

    def finish_price_delivery(self, attempt_id, state, error_type=None):
        if state not in {'sent', 'forbidden', 'not_found', 'failed', 'uncertain'}:
            raise ValueError('Invalid delivery state')
        self.db.execute("UPDATE price_delivery_attempts SET state=?,error_type=?,completed_at=? "
                        "WHERE id=? AND state='pending'", (state, error_type, timestamp(), attempt_id))

    def finish_price_admin_report(self, run_id, state, message_id=None):
        self.db.execute("UPDATE price_delivery_runs SET admin_state=?,admin_message_id=? WHERE id=?",
                        (state, message_id, run_id))

    def price_delivery_summary(self, guild_id, run_id):
        row = self.db.execute('''SELECT r.*, COUNT(a.id) AS messages,
            COUNT(DISTINCT a.user_id) AS recipients,
            COUNT(DISTINCT CASE WHEN a.state='sent' THEN a.user_id END) AS delivered,
            SUM(a.state='sent') AS sent,
            SUM(a.state IN ('forbidden','not_found','failed')) AS failed,
            SUM(a.state='uncertain') AS uncertain, SUM(a.state='pending') AS pending
            FROM price_delivery_runs r JOIN price_delivery_attempts a ON a.run_id=r.id
            WHERE r.guild_id=? AND r.id=? GROUP BY r.id''', (guild_id, run_id)).fetchone()
        return dict(row) if row else None

    def price_delivery_data(self, guild_id, start, end, details=False):
        rows = self.db.execute('''SELECT date(r.created_at,'+3 hours') AS day,
            COUNT(DISTINCT r.id) AS runs, COUNT(DISTINCT a.user_id) AS recipients,
            COUNT(DISTINCT CASE WHEN a.state='sent' THEN a.user_id END) AS delivered,
            COUNT(a.id) AS messages, SUM(a.state='sent') AS sent,
            SUM(a.state IN ('forbidden','not_found','failed')) AS failed,
            SUM(a.state='uncertain') AS uncertain, SUM(a.state='pending') AS pending
            FROM price_delivery_runs r JOIN price_delivery_attempts a ON a.run_id=r.id
            WHERE r.guild_id=? AND r.created_at>=? AND r.created_at<?
            GROUP BY day ORDER BY day''', (guild_id, start, end)).fetchall()
        daily = [dict(row) for row in rows]
        for day in daily:
            day['subscribers'] = self.db.execute("SELECT subscribers FROM price_delivery_runs "
                "WHERE guild_id=? AND date(created_at,'+3 hours')=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (guild_id, day['day'])).fetchone()[0]
        attempts = []
        if details:
            attempts = [dict(row) for row in self.db.execute('''SELECT r.id AS run_id, r.created_at,
                r.source_id, r.channel_id, r.subscribers, r.admin_state, a.user_id, a.item, a.threshold,
                a.state, a.error_type, a.completed_at FROM price_delivery_runs r
                JOIN price_delivery_attempts a ON a.run_id=r.id
                WHERE r.guild_id=? AND r.created_at>=? AND r.created_at<?
                ORDER BY r.created_at,r.id,a.id LIMIT 50001''', (guild_id,start,end))]
            if len(attempts) > 50000:
                raise UserError('Выгрузка превышает 50 000 попыток. Уменьшите период.')
        return {'daily': daily, 'attempts': attempts}
