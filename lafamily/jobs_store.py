"""Сохраняемая очередь рассылки; неоднозначные отправки не повторяются."""
import json
from datetime import UTC,datetime,timedelta
from lafamily.modules.events.time import timestamp


class JobsStore:
    def init_jobs(self):
        self.db.executescript('''CREATE TABLE IF NOT EXISTS delivery_jobs (
            id INTEGER PRIMARY KEY,guild_id TEXT NOT NULL,source_id TEXT NOT NULL,revision TEXT NOT NULL,
            user_id TEXT NOT NULL,channel_id TEXT NOT NULL,body TEXT NOT NULL,attempt_ids TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',created_at TEXT NOT NULL,ready_at TEXT NOT NULL,
            tries INTEGER NOT NULL DEFAULT 0,error_type TEXT);
            CREATE INDEX IF NOT EXISTS delivery_jobs_ready ON delivery_jobs(guild_id,state,ready_at);''')
        if 'message_group' not in {r['name'] for r in self.db.execute('PRAGMA table_info(price_delivery_attempts)')}:
            self.db.execute('ALTER TABLE price_delivery_attempts ADD COLUMN message_group TEXT')
        if 'notified_attempt_id' not in {r['name'] for r in self.db.execute('PRAGMA table_info(price_subscriptions)')}:
            self.db.execute('ALTER TABLE price_subscriptions ADD COLUMN notified_attempt_id INTEGER')
        if 'notifications_complete' not in {r['name'] for r in self.db.execute('PRAGMA table_info(price_publications)')}:
            self.db.execute('ALTER TABLE price_publications ADD COLUMN notifications_complete INTEGER NOT NULL DEFAULT 1')

    def finish_price_notifications(self, guild_id, source_id):
        self.db.execute('UPDATE price_publications SET notifications_complete=1 WHERE guild_id=? AND source_id=?',
                        (guild_id, source_id))

    def enqueue_price_job(self,guild_id,source_id,revision,user_id,channel_id,body,attempt_ids):
        with self.transaction():
            return self._enqueue_price_job(guild_id, source_id, revision, user_id, channel_id, body, attempt_ids)

    def _enqueue_price_job(self, guild_id, source_id, revision, user_id, channel_id, body, attempt_ids):
        ident = self.db.execute('''INSERT INTO delivery_jobs
            (guild_id,source_id,revision,user_id,channel_id,body,attempt_ids,created_at,ready_at)
            VALUES (?,?,?,?,?,?,?,?,?)''', (guild_id, source_id, revision, user_id, channel_id,
            body, json.dumps(attempt_ids), timestamp(), timestamp())).lastrowid
        self.db.executemany('UPDATE price_delivery_attempts SET message_group=? WHERE id=?',
                            [(f'job:{ident}', attempt) for attempt in attempt_ids])
        return ident

    def _queue_claimed_price_alerts(self, guild_id, source_id, alerts, *, revision, channel_id, lines):
        users = {}
        for alert in alerts:
            users.setdefault(alert['user_id'], []).append(alert)
        for user_id, items in users.items():
            body, attempts = [], []
            for alert in items:
                line = lines[alert['item']][alert['threshold']]
                if len(line) > 1650:
                    raise ValueError('Строка уведомления превышает лимит ЛС')
                if body and len('\n'.join([*body, line])) > 1650:
                    self._enqueue_price_job(guild_id, source_id, revision, user_id, channel_id,
                                            '\n'.join(body), attempts)
                    body, attempts = [], []
                body.append(line)
                attempts.append(alert['attempt_id'])
            if body:
                self._enqueue_price_job(guild_id, source_id, revision, user_id, channel_id,
                                        '\n'.join(body), attempts)

    def _expire_queued_price_jobs(self, guild_id, source_id, revision):
        cutoff = timestamp(datetime.now(UTC) - timedelta(hours=1))
        rows = self.db.execute("SELECT id FROM delivery_jobs WHERE guild_id=? AND state='queued' "
            "AND (source_id<>? OR revision<>? OR created_at<?)", (guild_id, source_id, revision, cutoff)).fetchall()
        for row in rows:
            self._finish_price_job(row['id'], 'failed', 'ExpiredPrice')

    def claim_price_job(self,guild_id):
        with self.transaction():
            row=self.db.execute("SELECT * FROM delivery_jobs WHERE guild_id=? AND state='queued' AND ready_at<=? ORDER BY id LIMIT 1",(guild_id,timestamp())).fetchone()
            if not row:return None
            self.db.execute("UPDATE delivery_jobs SET state='sending',tries=tries+1 WHERE id=?",(row['id'],))
            result=dict(row);result['attempt_ids']=json.loads(result['attempt_ids']);return result

    def finish_price_job(self,number,state,error_type=None):
        with self.transaction():
            self._finish_price_job(number, state, error_type)

    def _finish_price_job(self, number, state, error_type=None):
        row = self.db.execute('SELECT * FROM delivery_jobs WHERE id=?', (number,)).fetchone()
        if not row or row['state'] not in ('queued', 'sending'):
            return
        self.db.execute('UPDATE delivery_jobs SET state=?,error_type=? WHERE id=?', (state, error_type, number))
        for attempt in json.loads(row['attempt_ids']):
            self.finish_price_delivery(attempt, state, error_type)
            if state == 'failed' and error_type == 'ExpiredPrice':
                # Повторяем только первый сигнал, который заведомо не отправлялся.
                self.db.execute('UPDATE price_subscriptions SET notified=0,notified_attempt_id=NULL '
                    'WHERE guild_id=? AND notified_attempt_id=?', (row['guild_id'], attempt))

    def price_job_body(self, job):
        """Перепроверить подписки непосредственно перед сетевой отправкой."""
        with self.transaction():
            modes = self.price_subscription_modes(job['guild_id'], job['user_id'])
            body = []
            for attempt_id, line in zip(job['attempt_ids'], job['body'].splitlines(), strict=True):
                attempt = self.db.execute('SELECT item,threshold FROM price_delivery_attempts WHERE id=?',
                                          (attempt_id,)).fetchone()
                if attempt and modes.get(attempt['item']) == attempt['threshold']:
                    body.append(line)
                else:
                    self.finish_price_delivery(attempt_id, 'failed', 'SubscriptionChanged')
            return '\n'.join(body)

    def defer_price_job(self,number,error_type):
        self.db.execute("UPDATE delivery_jobs SET state='queued',ready_at=?,error_type=? WHERE id=?",
            (timestamp(datetime.now(UTC)+timedelta(seconds=30)),error_type,number))

    def recover_price_jobs(self,guild_id):
        for row in self.db.execute("SELECT id FROM delivery_jobs WHERE guild_id=? AND state='sending'",(guild_id,)).fetchall():
            self.finish_price_job(row['id'],'uncertain','ProcessInterrupted')
        self.db.execute("UPDATE price_delivery_runs SET admin_state='uncertain' "
                        "WHERE guild_id=? AND admin_state='sending'", (guild_id,))

    def current_price_job(self,job):
        current=self.db.execute('SELECT * FROM price_dm_current WHERE guild_id=?',(job['guild_id'],)).fetchone()
        settings = self.price_settings(job['guild_id'])
        return bool(current and current['source_id']==job['source_id'] and current['revision']==job['revision']
                    and settings and settings['channel_id'] == job['channel_id']
                    and timestamp(datetime.now(UTC)-timedelta(hours=1))<=job['created_at'])

    def price_job_run(self,number):
        row=self.db.execute('SELECT attempt_ids FROM delivery_jobs WHERE id=?',(number,)).fetchone()
        return self.db.execute('SELECT run_id FROM price_delivery_attempts WHERE id=?',(json.loads(row['attempt_ids'])[0],)).fetchone()[0]

    def queued_admin_reports(self,guild_id):
        return [dict(r) for r in self.db.execute('''SELECT r.* FROM price_delivery_runs r
            WHERE r.guild_id=? AND r.admin_state='pending'
            AND r.id IN (SELECT a.run_id FROM delivery_jobs j JOIN price_delivery_attempts a
                ON a.id=json_extract(j.attempt_ids,'$[0]') WHERE j.guild_id=?)
            AND NOT EXISTS(SELECT 1 FROM price_delivery_attempts a WHERE a.run_id=r.id AND a.state='pending')
            ORDER BY r.id LIMIT 10''',(guild_id,guild_id))]
