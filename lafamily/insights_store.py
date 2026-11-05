"""Прозрачность начислений, неизменяемые ведомости, история цен и диагностика."""
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta

from lafamily.errors import UserError
from lafamily.modules.events.time import timestamp


class InsightsStore:
    def init_insights(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS payout_sheets (
                id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                actor_id TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
                bank INTEGER NOT NULL, rows_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'prepared',
                created_at TEXT NOT NULL, approved_at TEXT, paid_at TEXT);
            CREATE TABLE IF NOT EXISTS price_history (
                guild_id TEXT NOT NULL, source_id TEXT NOT NULL, revision TEXT NOT NULL,
                captured_at TEXT NOT NULL, item TEXT NOT NULL, buyer TEXT NOT NULL,
                base INTEGER NOT NULL, bonus INTEGER,
                PRIMARY KEY(guild_id,source_id,revision,item,buyer));
            CREATE INDEX IF NOT EXISTS price_history_period ON price_history(guild_id,item,captured_at);
            CREATE TABLE IF NOT EXISTS price_history_sources (
                guild_id TEXT NOT NULL,source_id TEXT NOT NULL,revision TEXT NOT NULL,captured_at TEXT NOT NULL,
                PRIMARY KEY(guild_id,source_id));
            CREATE TABLE IF NOT EXISTS health_baseline (
                guild_id TEXT PRIMARY KEY, reports INTEGER NOT NULL, members INTEGER NOT NULL,
                active_points INTEGER NOT NULL, observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health_notices (
                guild_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, sent_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS report_recovery (
                guild_id TEXT NOT NULL,report_id INTEGER NOT NULL,PRIMARY KEY(guild_id,report_id));
        ''')

    def points_history(self, guild_id, user_id, offset=0, limit=15):
        return [dict(row) for row in self.db.execute('''SELECT r.id,r.type_name,r.occurred_at,
            r.cancelled_at,r.cancel_reason,r.points-r.win_bonus AS base_points,r.win_bonus,
            CASE WHEN r.mvp_id=m.user_id THEN r.mvp_bonus ELSE 0 END AS mvp_points,
            r.points+CASE WHEN r.mvp_id=m.user_id THEN r.mvp_bonus ELSE 0 END AS awarded,
            (SELECT actor_id FROM audit_log a WHERE a.guild_id=r.guild_id AND a.action='report.cancelled'
                AND json_extract(a.details,'$.id')=r.id ORDER BY a.id LIMIT 1) AS cancelled_by
            FROM reports r JOIN report_members m ON m.report_id=r.id
            WHERE r.guild_id=? AND m.user_id=? ORDER BY r.occurred_at DESC,r.id DESC LIMIT ? OFFSET ?''',
            (guild_id,user_id,limit,offset))]

    def participant_profile(self, guild_id, user_id, period):
        row = self.db.execute('''SELECT COALESCE(SUM(r.points+CASE WHEN r.mvp_id=m.user_id THEN r.mvp_bonus ELSE 0 END),0) AS total,
            COALESCE(SUM(CASE WHEN r.occurred_at>=? AND r.occurred_at<? THEN r.points+
                CASE WHEN r.mvp_id=m.user_id THEN r.mvp_bonus ELSE 0 END ELSE 0 END),0) AS week,
            COUNT(*) AS events,COALESCE(SUM(r.outcome='win'),0) AS wins,
            COALESCE(SUM(r.mvp_id=m.user_id),0) AS mvps
            FROM reports r JOIN report_members m ON m.report_id=r.id
            WHERE r.guild_id=? AND m.user_id=? AND r.cancelled_at IS NULL''', (*period,guild_id,user_id)).fetchone()
        return dict(row)

    def similar_reports(self, draft):
        when = datetime.fromisoformat(draft.occurred_at.replace('Z','+00:00'))
        rows = self.db.execute('''SELECT id FROM reports WHERE guild_id=? AND type_name=?
            AND cancelled_at IS NULL AND request_id<>? AND occurred_at>=? AND occurred_at<=?
            ORDER BY id DESC LIMIT 30''', (draft.guild_id,draft.type_name,draft.request_id,
                timestamp(when-timedelta(minutes=30)),timestamp(when+timedelta(minutes=30))))
        members = set(draft.members)
        return [row['id'] for row in rows if members.intersection(
            r['user_id'] for r in self.db.execute('SELECT user_id FROM report_members WHERE report_id=?',(row['id'],)))]

    def duplicate_report_key(self, draft):
        matches = self.similar_reports(draft)
        if not matches:
            return '', matches
        data = asdict(draft)
        data.pop('duplicate_ack', None)
        return hashlib.sha256(json.dumps([data,matches],sort_keys=True).encode()).hexdigest(), matches

    def create_payout_sheet(self, guild_id, actor_id, request_id, period, bank):
        from lafamily.modules.events.payouts import allocate_bank
        with self.transaction():
            existing = self.db.execute('SELECT id FROM payout_sheets WHERE guild_id=? AND request_id=?',
                                       (guild_id,request_id)).fetchone()
            if existing:
                return self.payout_sheet(guild_id,existing['id'])
            rows = allocate_bank(bank,self.statistics(guild_id,period,limit=-1))
            ident = self.db.execute('''INSERT INTO payout_sheets
                (guild_id,actor_id,request_id,start_at,end_at,bank,rows_json,created_at) VALUES (?,?,?,?,?,?,?,?)''',
                (guild_id,actor_id,request_id,*period,bank,json.dumps(rows),timestamp())).lastrowid
            self.audit(guild_id,actor_id,'payout.prepared',{'id':ident,'bank':bank})
            return self.payout_sheet(guild_id,ident)

    def payout_sheet(self, guild_id, number):
        row = self.db.execute('SELECT * FROM payout_sheets WHERE guild_id=? AND id=?',(guild_id,number)).fetchone()
        if not row:
            raise UserError('Ведомость не найдена.')
        data = dict(row)
        data['rows'] = json.loads(data.pop('rows_json'))
        return data

    def payout_sheets(self, guild_id, offset=0):
        return [dict(row) for row in self.db.execute('''SELECT id,start_at,end_at,bank,state FROM payout_sheets
            WHERE guild_id=? ORDER BY id DESC LIMIT 15 OFFSET ?''',(guild_id,offset))]

    def transition_payout(self, guild_id, actor_id, number, target):
        previous = {'approved':'prepared','paid':'approved'}.get(target)
        if previous is None:
            raise UserError('Недопустимый статус ведомости.')
        with self.transaction():
            sheet = self.payout_sheet(guild_id,number)
            if sheet['state'] == target:
                return sheet
            if sheet['state'] != previous:
                raise UserError('Сначала подготовьте и утвердите ведомость; выплаченную менять нельзя.')
            if target == 'approved':
                other = self.db.execute("SELECT id FROM payout_sheets WHERE guild_id=? AND start_at=? AND end_at=? "
                    "AND id<>? AND state IN ('approved','paid')",(guild_id,sheet['start_at'],sheet['end_at'],number)).fetchone()
                if other:
                    raise UserError(f"За этот период уже утверждена ведомость #{other['id']}.")
            column = 'approved_at' if target == 'approved' else 'paid_at'
            self.db.execute(f'UPDATE payout_sheets SET state=?,{column}=? WHERE id=?',(target,timestamp(),number))
            self.audit(guild_id,actor_id,'payout.'+target,{'id':number})
            return self.payout_sheet(guild_id,number)

    def save_price_history(self, guild_id, source_id, revision, report):
        rows = []
        now = timestamp()
        for buyer, prices in report.buyers.items():
            rows.extend((guild_id,source_id,revision,now,item,str(buyer),row.base,row.bonus) for item,row in prices.items())
        rows.extend((guild_id,source_id,revision,now,item,'продукты',row.base,row.bonus) for item,row in report.products.items())
        with self.transaction():
            self.db.executemany('INSERT OR IGNORE INTO price_history VALUES (?,?,?,?,?,?,?,?)',rows)
            self.db.execute('''INSERT INTO price_history_sources VALUES (?,?,?,?)
                ON CONFLICT(guild_id,source_id) DO UPDATE SET revision=excluded.revision''',
                (guild_id,source_id,revision,now))

    def price_history_data(self, guild_id, item, start, end):
        # Последняя редакция каждого исходника, лучшая цена дня; изменения сохраняются в сырой истории.
        return [dict(row) for row in self.db.execute('''SELECT date(s.captured_at,'+3 hours') AS day,
            MIN(h.base) AS minimum,MAX(h.base) AS maximum,COUNT(DISTINCT h.source_id) AS reports
            FROM price_history h JOIN price_history_sources s ON s.guild_id=h.guild_id AND s.source_id=h.source_id AND s.revision=h.revision
            WHERE h.guild_id=? AND h.item=? AND s.captured_at>=? AND s.captured_at<?
            GROUP BY day ORDER BY day''',(guild_id,item,start,end))]

    def health_snapshot(self, guild_id):
        row = self.db.execute('''SELECT COUNT(*) AS reports,COUNT(DISTINCT m.user_id) AS members,
            COALESCE(SUM(CASE WHEN r.cancelled_at IS NULL THEN r.points+
                CASE WHEN r.mvp_id=m.user_id THEN r.mvp_bonus ELSE 0 END ELSE 0 END),0) AS active_points
            FROM reports r LEFT JOIN report_members m ON m.report_id=r.id WHERE r.guild_id=?''',(guild_id,)).fetchone()
        result = dict(row)
        result['reports'] = self.db.execute('SELECT COUNT(*) FROM reports WHERE guild_id=?',(guild_id,)).fetchone()[0]
        old = self.db.execute('SELECT * FROM health_baseline WHERE guild_id=?',(guild_id,)).fetchone()
        result['warnings'] = []
        if old and (result['reports'] < old['reports'] or result['members'] < old['members']):
            result['warnings'].append('Число отчётов или участников истории уменьшилось: проверьте источник БД и восстановление.')
        # Не затираем прежний максимум при подозрительном уменьшении.
        if not result['warnings']:
            self.db.execute('INSERT OR REPLACE INTO health_baseline VALUES (?,?,?,?,?)',
                            (guild_id,result['reports'],result['members'],result['active_points'],timestamp()))
        result['integrity'] = self.db.execute('PRAGMA quick_check(1)').fetchone()[0]
        if result['integrity'] != 'ok':
            result['warnings'].append('Проверка целостности SQLite не пройдена.')
        result['backup'] = self.database_backup_settings(guild_id)
        result['operations'] = self.unfinished_operations(guild_id)
        return result

    def diagnostic_channels(self,guild_id):
        result=[]
        for table,column,label,needs in (
            ('settings','channel_id','Отчёты МП','embed_links read_message_history'),
            ('price_settings','channel_id','Цены','embed_links read_message_history manage_messages'),
            ('database_backup_settings','channel_id','Бэкапы','attach_files read_message_history'),
            ('admin_report_settings','channel_id','Админские отчёты',''),
            ('release_settings','channel_id','Обновления','embed_links read_message_history'),
            ('announcement_settings','channel_id','Картинки анонсов','embed_links attach_files read_message_history'),
            ('onboarding_settings','entry_id','Форма вступления','embed_links'),
            ('onboarding_settings','review_id','Проверка заявок','embed_links'),
            ('announcement_templates','channel_id','Анонсы','embed_links')):
            rows=self.db.execute(f'SELECT DISTINCT {column} AS id FROM {table} WHERE guild_id=? AND {column} IS NOT NULL',(guild_id,))
            result.extend({'id':r['id'],'label':label,'needs':needs.split()} for r in rows)
        return result

    def unfinished_operations(self, guild_id):
        result = []
        for row in self.db.execute('SELECT id,cancelled_at FROM reports WHERE guild_id=? AND message_id IS NULL ORDER BY id DESC LIMIT 20',(guild_id,)):
            result.append({'kind':'report','id':row['id'],'state':'не опубликован','safe':True})
        for row in self.db.execute("SELECT source_id,state FROM price_publications WHERE guild_id=? AND state<>'published' LIMIT 20",(guild_id,)):
            result.append({'kind':'prices','id':row['source_id'],'state':row['state'],'safe':False})
        for row in self.db.execute('''SELECT a.id,a.state FROM price_delivery_attempts a JOIN price_delivery_runs r ON r.id=a.run_id
            WHERE r.guild_id=? AND a.state<>'sent' ORDER BY a.id DESC LIMIT 20''',(guild_id,)):
            result.append({'kind':'delivery','id':row['id'],'state':row['state'],'safe':False})
        for row in self.db.execute("SELECT message_id,notification_state FROM onboarding_requests WHERE guild_id=? AND (message_id LIKE 'pending:%' OR (approved<>0 AND notification_state IN ('pending','sending','failed','uncertain'))) LIMIT 20",(guild_id,)):
            result.append({'kind':'onboarding','id':row['message_id'],'state':row['notification_state'],'safe':False})
        for row in self.db.execute('SELECT id FROM planned_events WHERE guild_id=? AND message_id IS NULL ORDER BY id DESC LIMIT 20',(guild_id,)):
            result.append({'kind':'signup','id':row['id'],'state':'не привязана; проверьте канал','safe':False})
        return result

    def health_notice(self,guild_id):
        row=self.db.execute('SELECT fingerprint FROM health_notices WHERE guild_id=?',(guild_id,)).fetchone()
        return row[0] if row else None

    def save_health_notice(self,guild_id,fingerprint):
        self.db.execute('INSERT OR REPLACE INTO health_notices VALUES (?,?,?)',(guild_id,fingerprint,timestamp()))

    def claim_report_recovery(self,guild_id,number):
        return bool(self.db.execute('INSERT OR IGNORE INTO report_recovery VALUES (?,?)',(guild_id,number)).rowcount)

    def release_report_recovery(self,guild_id,number):
        self.db.execute('DELETE FROM report_recovery WHERE guild_id=? AND report_id=?',(guild_id,number))
