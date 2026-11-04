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

    def enqueue_price_job(self,guild_id,source_id,revision,user_id,channel_id,body,attempt_ids):
        with self.transaction():
            ident=self.db.execute('''INSERT INTO delivery_jobs
                (guild_id,source_id,revision,user_id,channel_id,body,attempt_ids,created_at,ready_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',(guild_id,source_id,revision,user_id,channel_id,body,json.dumps(attempt_ids),timestamp(),timestamp())).lastrowid
            self.db.executemany('UPDATE price_delivery_attempts SET message_group=? WHERE id=?',[(f'job:{ident}',a) for a in attempt_ids])
            return ident

    def claim_price_job(self,guild_id):
        with self.transaction():
            row=self.db.execute("SELECT * FROM delivery_jobs WHERE guild_id=? AND state='queued' AND ready_at<=? ORDER BY id LIMIT 1",(guild_id,timestamp())).fetchone()
            if not row:return None
            self.db.execute("UPDATE delivery_jobs SET state='sending',tries=tries+1 WHERE id=?",(row['id'],))
            result=dict(row);result['attempt_ids']=json.loads(result['attempt_ids']);return result

    def finish_price_job(self,number,state,error_type=None):
        with self.transaction():
            row=self.db.execute('SELECT attempt_ids FROM delivery_jobs WHERE id=?',(number,)).fetchone()
            self.db.execute('UPDATE delivery_jobs SET state=?,error_type=? WHERE id=?',(state,error_type,number))
            if row:
                for attempt in json.loads(row['attempt_ids']):self.finish_price_delivery(attempt,state,error_type)

    def defer_price_job(self,number,error_type):
        self.db.execute("UPDATE delivery_jobs SET state='queued',ready_at=?,error_type=? WHERE id=?",
            (timestamp(datetime.now(UTC)+timedelta(seconds=30)),error_type,number))

    def recover_price_jobs(self,guild_id):
        for row in self.db.execute("SELECT id FROM delivery_jobs WHERE guild_id=? AND state='sending'",(guild_id,)).fetchall():
            self.finish_price_job(row['id'],'uncertain','ProcessInterrupted')

    def current_price_job(self,job):
        current=self.db.execute('SELECT * FROM price_dm_current WHERE guild_id=?',(job['guild_id'],)).fetchone()
        return bool(current and current['source_id']==job['source_id'] and current['revision']==job['revision']
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
