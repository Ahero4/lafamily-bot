import unittest
from datetime import UTC, datetime

from lafamily.database import Store


class PriceRetentionTests(unittest.TestCase):
    def test_cutoff_protects_active_runs_and_reused_ids(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        db = store.db
        old = '2026-01-01T00:00:00.000Z'
        cutoff = '2026-01-02T00:00:00.000Z'
        for run, guild, date, job_state, report_state in [
                (1,'1',old,'sent','sent'), (2,'1',cutoff,'sent','sent'),
                (3,'2',old,'sent','sent'), (4,'1',old,'queued','pending'), (5,'1',old,'sent','pending')]:
            db.execute('INSERT INTO price_history_sources VALUES (?,?,?,?)', (guild,str(run),'v1',date))
            db.execute('INSERT INTO price_history VALUES (?,?,?,?,?,?,?,?)', (guild,str(run),'v1',date,'item','1',10,None))
            db.execute('INSERT INTO price_delivery_runs VALUES (?,?,?,?,?,?,?,NULL)',
                       (run,guild,'channel',str(run),date,1,report_state))
            db.execute('INSERT INTO price_delivery_attempts(id,run_id,user_id,item,threshold,state,message_group) '
                       'VALUES (?,?,?,?,?,?,?)',(run,run,'42','item','max','pending' if job_state=='queued' else 'sent',f'job:{run}'))
            db.execute('INSERT INTO delivery_jobs(id,guild_id,source_id,revision,user_id,channel_id,body,attempt_ids,'
                       'state,created_at,ready_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (run,guild,str(run),'v1','42','channel','text',f'[{run}]',job_state,date,date))
        db.execute("INSERT INTO price_subscriptions(guild_id,user_id,item,notified,notified_attempt_id) VALUES ('1','42','item',1,1)")
        db.execute('CREATE TABLE price_dm_messages(attempt_id INTEGER PRIMARY KEY REFERENCES price_delivery_attempts(id))')
        db.executemany('INSERT INTO price_dm_messages VALUES (?)', [(1,), (2,)])
        for _ in range(2):
            store.prune_price_history('1', datetime(2026,2,1,tzinfo=UTC))
        for table in ('price_delivery_runs','delivery_jobs','price_delivery_attempts'):
            self.assertEqual([row[0] for row in db.execute(f'SELECT id FROM {table} ORDER BY id')],[2,3,4,5])
        for table in ('price_history','price_history_sources'):
            self.assertEqual([row[0] for row in db.execute(f'SELECT source_id FROM {table} ORDER BY source_id')],['2','3'])
        self.assertEqual(tuple(db.execute('SELECT notified,notified_attempt_id FROM price_subscriptions').fetchone()),(1,None))
        self.assertEqual([row[0] for row in db.execute('SELECT attempt_id FROM price_dm_messages')], [2])
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
