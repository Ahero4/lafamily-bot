"""Журнал доставки ЛС скупщика; методы вызываются в потоке DatabaseWorker."""
from datetime import UTC, datetime, timedelta
import logging

from lafamily.delivery_failure import stored_reason
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

        if 'error_reason' not in {row['name'] for row in self.db.execute('PRAGMA table_info(price_delivery_attempts)')}:
            self.db.execute('ALTER TABLE price_delivery_attempts ADD COLUMN error_reason TEXT')

    def prune_price_history(self, guild_id, now=None):
        cutoff = timestamp((now or datetime.now(UTC)) - timedelta(days=30))
        with self.transaction():
            self.db.execute('DELETE FROM price_history_sources WHERE guild_id=? AND captured_at<?', (guild_id, cutoff))
            self.db.execute('''DELETE FROM price_history WHERE guild_id=? AND NOT EXISTS (
                SELECT 1 FROM price_history_sources s WHERE s.guild_id=price_history.guild_id
                AND s.source_id=price_history.source_id)''', (guild_id,))

            # Snapshot eligible runs before deleting their attempts and grouped job payloads.
            self.db.execute('CREATE TEMP TABLE expired_price_runs(id INTEGER PRIMARY KEY)')
            self.db.execute('''INSERT INTO expired_price_runs
                SELECT id FROM price_delivery_runs WHERE guild_id=? AND created_at<? AND admin_state!='sending'
                EXCEPT SELECT a.run_id FROM delivery_jobs j JOIN price_delivery_attempts a
                  ON a.id=json_extract(j.attempt_ids,'$[0]') JOIN price_delivery_runs r ON r.id=a.run_id
                WHERE j.guild_id=? AND (j.state IN ('queued','sending') OR j.created_at>=? OR r.admin_state='pending')
                EXCEPT SELECT a.run_id FROM price_delivery_attempts a JOIN price_delivery_runs r ON r.id=a.run_id
                WHERE r.guild_id=? AND a.completed_at>=?''', (guild_id, cutoff, guild_id, cutoff, guild_id, cutoff))
            self.db.execute('''DELETE FROM delivery_jobs WHERE guild_id=? AND created_at<?
                AND state NOT IN ('queued','sending') AND NOT EXISTS (
                    SELECT 1 FROM price_delivery_attempts a WHERE a.id=json_extract(delivery_jobs.attempt_ids,'$[0]')
                    AND a.run_id NOT IN expired_price_runs)''', (guild_id, cutoff))
            self.db.execute('''UPDATE price_subscriptions SET notified_attempt_id=NULL WHERE guild_id=?
                AND notified_attempt_id IN (SELECT id FROM price_delivery_attempts WHERE run_id IN expired_price_runs)''',
                (guild_id,))
            # 0.6.0 stored delivered DM IDs for a retired cleanup queue.
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_dm_messages'").fetchone():
                self.db.execute('''DELETE FROM price_dm_messages WHERE attempt_id IN
                    (SELECT id FROM price_delivery_attempts WHERE run_id IN expired_price_runs)''')
            self.db.execute('DELETE FROM price_delivery_attempts WHERE run_id IN expired_price_runs')
            self.db.execute('DELETE FROM price_delivery_runs WHERE id IN expired_price_runs')
            self.db.execute('DROP TABLE expired_price_runs')

    def admin_report_channel(self, guild_id):
        row = self.db.execute('SELECT channel_id FROM admin_report_settings WHERE guild_id=?', (guild_id,)).fetchone()
        return row['channel_id'] if row else None

    def configure_admin_report_channel(self, guild_id, actor_id, channel_id):
        with self.transaction():
            if channel_id is None:
                self.db.execute('DELETE FROM admin_report_settings WHERE guild_id=?', (guild_id,))
            else:
                self.ensure_channel_separation(guild_id, channel_id)
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

    def finish_price_delivery(self, attempt_id, state, error_type=None, error_reason=None):
        if state not in {'sent', 'forbidden', 'not_found', 'failed', 'uncertain'}:
            raise ValueError('Invalid delivery state')
        reason = error_reason or stored_reason(error_type, state)
        updated = self.db.execute("UPDATE price_delivery_attempts SET state=?,error_type=?,error_reason=?,completed_at=? "
                                  "WHERE id=? AND state='pending'", (state, error_type, reason, timestamp(), attempt_id))
        if updated.rowcount and reason:
            logging.getLogger(__name__).warning('ЛС скупщика, попытка %s (%s): %s', attempt_id, state, reason)

    def finish_price_admin_report(self, run_id, state, message_id=None):
        self.db.execute("UPDATE price_delivery_runs SET admin_state=?,admin_message_id=? WHERE id=?",
                        (state, message_id, run_id))

    def claim_price_admin_report(self, guild_id, run_id):
        result = self.db.execute("UPDATE price_delivery_runs SET admin_state='sending' "
            "WHERE guild_id=? AND id=? AND admin_state='pending'", (guild_id, run_id))
        return bool(result.rowcount)

    def price_delivery_summary(self, guild_id, run_id):
        row = self.db.execute('''SELECT r.*, COUNT(a.id) AS messages,
            COUNT(DISTINCT a.user_id) AS recipients,
            COUNT(DISTINCT CASE WHEN a.state='sent' THEN a.user_id END) AS delivered,
            SUM(a.state='sent') AS sent,
            SUM(a.state IN ('forbidden','not_found','failed')) AS failed,
            SUM(a.state='uncertain') AS uncertain, SUM(a.state='pending') AS pending
            FROM price_delivery_runs r JOIN (SELECT MIN(id) AS id,run_id,user_id,
                CASE WHEN SUM(state='pending')>0 THEN 'pending'
                     WHEN SUM(state='uncertain')>0 THEN 'uncertain'
                     WHEN SUM(state='sent')>0 THEN 'sent' ELSE MIN(state) END AS state
                FROM price_delivery_attempts GROUP BY run_id,COALESCE(message_group,'attempt-'||id)) a ON a.run_id=r.id
            WHERE r.guild_id=? AND r.id=?
            GROUP BY r.id''', (guild_id, run_id)).fetchone()
        return dict(row) if row else None

    def price_delivery_reasons(self, guild_id, run_id=None, *, period=None):
        condition, values = ('r.id=?', (run_id,)) if period is None else ('r.created_at>=? AND r.created_at<?', period)
        # Select one outcome per actual DM, with the same precedence as the delivery counters.
        rows = self.db.execute(f'''WITH messages AS (
            SELECT a.error_reason, a.error_type, a.state, ROW_NUMBER() OVER (
                PARTITION BY a.run_id, COALESCE(a.message_group,'attempt-'||a.id)
                ORDER BY CASE a.state WHEN 'pending' THEN 0 WHEN 'uncertain' THEN 1 WHEN 'sent' THEN 2 ELSE 3 END,
                         a.state, a.id) AS priority
            FROM price_delivery_attempts a JOIN price_delivery_runs r ON r.id=a.run_id
            WHERE r.guild_id=? AND {condition})
            SELECT error_reason,error_type,state,COUNT(*) AS messages FROM messages
            WHERE priority=1 AND state NOT IN ('sent','pending')
            GROUP BY error_reason,error_type,state ORDER BY messages DESC,error_type LIMIT 7''', (guild_id, *values))
        return [(r['error_reason'] or stored_reason(r['error_type'],r['state']), r['messages']) for r in rows]

    def price_delivery_data(self, guild_id, start, end, details=False):
        rows = self.db.execute('''SELECT date(r.created_at,'+3 hours') AS day,
            COUNT(DISTINCT r.id) AS runs, COUNT(DISTINCT a.user_id) AS recipients,
            COUNT(DISTINCT CASE WHEN a.state='sent' THEN a.user_id END) AS delivered,
            COUNT(a.id) AS messages, SUM(a.state='sent') AS sent,
            SUM(a.state IN ('forbidden','not_found','failed')) AS failed,
            SUM(a.state='uncertain') AS uncertain, SUM(a.state='pending') AS pending
            FROM price_delivery_runs r JOIN (SELECT MIN(id) AS id,run_id,user_id,
                CASE WHEN SUM(state='pending')>0 THEN 'pending'
                     WHEN SUM(state='uncertain')>0 THEN 'uncertain'
                     WHEN SUM(state='sent')>0 THEN 'sent' ELSE MIN(state) END AS state
                FROM price_delivery_attempts GROUP BY run_id,COALESCE(message_group,'attempt-'||id)) a ON a.run_id=r.id
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
                a.state, a.error_type, a.error_reason, a.completed_at, a.message_group FROM price_delivery_runs r
                JOIN price_delivery_attempts a ON a.run_id=r.id
                WHERE r.guild_id=? AND r.created_at>=? AND r.created_at<?
                ORDER BY r.created_at,r.id,a.id LIMIT 50001''', (guild_id,start,end))]
            for attempt in attempts:
                if not attempt['error_reason']:
                    attempt['error_reason'] = stored_reason(attempt['error_type'], attempt['state'])
            if len(attempts) > 50000:
                raise UserError('Выгрузка превышает 50 000 попыток. Уменьшите период.')
        return {'daily': daily, 'attempts': attempts}
