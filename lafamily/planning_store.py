"""Постоянная запись на МП; порядок резерва и лимиты защищены транзакцией."""
import re
from datetime import datetime, timedelta, UTC

from lafamily.errors import UserError
from lafamily.modules.events.time import timestamp


PLANNED_REMINDER_LEAD = timedelta(minutes=15)


class PlanningStore:
    def init_planning(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS planned_events (
                id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE, channel_id TEXT NOT NULL, message_id TEXT,
                name TEXT NOT NULL, starts_at TEXT NOT NULL, capacity INTEGER NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0, reminder_state TEXT);
            CREATE TABLE IF NOT EXISTS planned_signups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES planned_events(id),
                user_id TEXT NOT NULL, choice TEXT NOT NULL, UNIQUE(event_id,user_id));
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(planned_events)')}
        if 'reminder_state' not in columns:
            self.db.execute('ALTER TABLE planned_events ADD COLUMN reminder_state TEXT')

    def create_planned_event(self,guild_id,actor_id,request_id,channel_id,name,starts_at,capacity,now=None,
                             *, reminder=False):
        if not name.strip() or len(name)>80 or not 1<=capacity<=100:
            raise UserError('Название до 80 символов; мест от 1 до 100.')
        if datetime.fromisoformat(starts_at.replace('Z','+00:00'))<=(now or datetime.now(UTC)):
            raise UserError('Начало МП должно быть в будущем.')
        with self.transaction():
            self.ensure_channel_separation(guild_id,channel_id)
            self.db.execute('''INSERT OR IGNORE INTO planned_events
                (guild_id,actor_id,request_id,channel_id,name,starts_at,capacity,reminder_state)
                VALUES (?,?,?,?,?,?,?,?)''',
                (guild_id,actor_id,request_id,channel_id,name.strip(),starts_at,capacity,
                 'pending' if reminder else None))
            row=self.db.execute('SELECT id FROM planned_events WHERE guild_id=? AND request_id=?',(guild_id,request_id)).fetchone()
            if not row:raise UserError('Ключ записи уже используется другим сервером.')
            return self.planned_event(guild_id,row['id'])

    def planned_event(self,guild_id,number):
        row=self.db.execute('SELECT * FROM planned_events WHERE guild_id=? AND id=?',(guild_id,number)).fetchone()
        if not row:raise UserError('Запланированное МП не найдено.')
        result=dict(row)
        rows=[dict(r) for r in self.db.execute('SELECT user_id,choice FROM planned_signups WHERE event_id=? ORDER BY id',(number,))]
        going=[r['user_id'] for r in rows if r['choice']=='going']
        result.update(going=going[:result['capacity']],reserve=going[result['capacity']:],
                      maybe=[r['user_id'] for r in rows if r['choice']=='maybe'])
        return result

    def bind_planned_message(self,guild_id,number,message_id):
        self.db.execute('UPDATE planned_events SET message_id=? WHERE guild_id=? AND id=? AND message_id IS NULL',
                        (message_id,guild_id,number))

    def due_planned_reminders(self, guild_id, now):
        return [dict(row) for row in self.db.execute('''SELECT id,channel_id,message_id,name,starts_at
            FROM planned_events WHERE guild_id=? AND reminder_state='pending' AND closed=0
            AND message_id IS NOT NULL AND starts_at>? AND starts_at<=? ORDER BY starts_at,id''',
            (guild_id, timestamp(now), timestamp(now + PLANNED_REMINDER_LEAD)))]

    def claim_planned_reminder(self, guild_id, number, now):
        # До обращения к Discord резервируем попытку. После сбоя не повторяем неизвестную отправку.
        claimed = self.db.execute("""UPDATE planned_events SET reminder_state='uncertain'
            WHERE guild_id=? AND id=? AND reminder_state='pending' AND closed=0
            AND message_id IS NOT NULL AND starts_at>? AND starts_at<=?""",
            (guild_id, number, timestamp(now), timestamp(now + PLANNED_REMINDER_LEAD)))
        return bool(claimed.rowcount)

    def finish_planned_reminder(self, guild_id, number):
        self.db.execute("UPDATE planned_events SET reminder_state='sent' WHERE guild_id=? AND id=?",
                        (guild_id, number))

    def planned_by_message(self,guild_id,message_id):
        row=self.db.execute('SELECT id FROM planned_events WHERE guild_id=? AND message_id=?',(guild_id,message_id)).fetchone()
        if not row:raise UserError('Карточка записи недоступна. Используйте /мп запись состав.')
        return self.planned_event(guild_id,row['id'])

    def signup_planned(self,guild_id,number,user_id,choice):
        if choice not in ('going','maybe','no') or not re.fullmatch(r'[0-9]{17,20}',user_id):
            raise UserError('Некорректная запись участника.')
        with self.transaction():
            event=self.planned_event(guild_id,number)
            if event['closed'] or event['starts_at']<=timestamp():
                raise UserError('Запись закрыта: МП уже началось или закрыто организатором.')
            if choice=='no':
                self.db.execute('DELETE FROM planned_signups WHERE event_id=? AND user_id=?',(number,user_id))
            else:
                previous=self.db.execute('SELECT choice FROM planned_signups WHERE event_id=? AND user_id=?',(number,user_id)).fetchone()
                if previous and previous['choice']==choice:return event
                # Переход из «Возможно» в основной список занимает новое место в очереди.
                self.db.execute('DELETE FROM planned_signups WHERE event_id=? AND user_id=?',(number,user_id))
                self.db.execute('INSERT INTO planned_signups(event_id,user_id,choice) VALUES (?,?,?)',(number,user_id,choice))
            return self.planned_event(guild_id,number)

    def close_planned(self,guild_id,actor_id,number):
        with self.transaction():
            self.planned_event(guild_id,number)
            self.db.execute('UPDATE planned_events SET closed=1 WHERE guild_id=? AND id=?',(guild_id,number))
            self.audit(guild_id,actor_id,'planned.closed',{'id':number})
