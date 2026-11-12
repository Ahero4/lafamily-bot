"""Persistent personal gardens; hourly boundaries are UTC integer hours."""
from datetime import UTC, datetime
from lafamily.errors import UserError

CROPS = {'cabbage': ('Капуста', 4), 'pumpkin': ('Тыква', 5),
         'mandarin': ('Мандарины', 6), 'pineapple': ('Ананасы', 7)}


def resolve_now(now=None):
    return now() if callable(now) else now if now is not None else datetime.now(UTC)


def hour_at(now=None):
    return int(resolve_now(now).timestamp()) // 3600


class GardenStore:
    def init_garden(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS garden_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
          request_id TEXT NOT NULL, crop TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 9),
          planted_hour INTEGER NOT NULL, checked_hour INTEGER NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
          processed_hour INTEGER, fertilized INTEGER NOT NULL DEFAULT 0, ripe_hour INTEGER, collected INTEGER NOT NULL DEFAULT 0,
          UNIQUE(guild_id,user_id,request_id,crop));
        CREATE TABLE IF NOT EXISTS garden_requests(guild_id TEXT,user_id TEXT,request_id TEXT,
          PRIMARY KEY(guild_id,user_id,request_id));
        CREATE TABLE IF NOT EXISTS garden_reminder_targets(guild_id TEXT NOT NULL,user_id TEXT NOT NULL,
          event_key TEXT NOT NULL,batch_id INTEGER NOT NULL,needs_process INTEGER NOT NULL,
          needs_fertilize INTEGER NOT NULL,PRIMARY KEY(guild_id,user_id,event_key,batch_id));
        CREATE TABLE IF NOT EXISTS garden_deliveries(guild_id TEXT,user_id TEXT,kind TEXT,event_key TEXT,
          state TEXT NOT NULL, PRIMARY KEY(guild_id,user_id,kind,event_key));
        ''')

    def _garden_rows(self, guild, user):
        return [dict(r) for r in self.db.execute('SELECT * FROM garden_batches WHERE guild_id=? AND user_id=? AND collected=0 ORDER BY id', (guild,user))]

    def _garden_advance(self, guild, user, hour):
        for row in self._garden_rows(guild,user):
            if row['ripe_hour'] is not None or hour <= row['checked_hour']:
                continue
            # Only the first boundary and the boundary following a saved processing can count.
            boundaries = {row['planted_hour'] + 1}
            if row['processed_hour'] is not None:
                boundaries.add(row['processed_hour'] + 1)
            progress = row['progress']
            ripe = None
            for boundary in sorted(boundaries):
                if row['checked_hour'] < boundary <= hour:
                    progress += 1
                    if progress >= CROPS[row['crop']][1]:
                        ripe = boundary
                        break
            self.db.execute('UPDATE garden_batches SET progress=?, checked_hour=?, ripe_hour=? WHERE id=?',
                            (progress,hour,ripe,row['id']))

    def garden_snapshot(self, guild, user, now=None):
        with self.transaction():
            self._garden_advance(guild,user,hour_at(now))
            return self._garden_rows(guild,user)

    def garden_plant(self, guild, user, request, quantities, now=None, *, remaining=None):
        if set(quantities) - set(CROPS) or any(type(v) is not int or v < 0 or v > 9 for v in quantities.values()) or not 1 <= sum(quantities.values()) <= 9:
            raise UserError('Укажите целые количества растений: суммарно от 1 до 9.')
        remaining = {} if remaining is None else remaining
        if not isinstance(remaining, dict) or any(crop not in CROPS or not quantities.get(crop) or type(value) is not int or not 0 <= value <= CROPS[crop][1] for crop,value in remaining.items()):
            raise UserError('Остаток PayDay: целое число от 0 до срока культуры, только для посаженных растений.')
        with self.transaction():
            now = resolve_now(now)
            hour = hour_at(now)
            self._garden_advance(guild,user,hour)
            if self.db.execute('SELECT 1 FROM garden_requests WHERE guild_id=? AND user_id=? AND request_id=?',(guild,user,request)).fetchone():
                return
            if sum(r['quantity'] for r in self._garden_rows(guild,user)) + sum(quantities.values()) > 9:
                raise UserError('В саду всего 9 мест. Сначала соберите созревшие растения.')
            self.db.execute('INSERT INTO garden_requests VALUES(?,?,?)',(guild,user,request))
            for crop, quantity in quantities.items():
                if quantity:
                    self.db.execute('INSERT INTO garden_batches(guild_id,user_id,request_id,crop,quantity,planted_hour,checked_hour,progress,ripe_hour) VALUES(?,?,?,?,?,?,?,?,?)',
                                    (guild,user,request,crop,quantity,hour-1 if crop in remaining else hour,hour,
                                     CROPS[crop][1]-remaining[crop] if crop in remaining else 0,
                                     hour if remaining.get(crop) == 0 else None))

    def garden_action(self, guild, user, action, batch_id=None, now=None):
        with self.transaction():
            now = resolve_now(now)
            hour = hour_at(now)
            self._garden_advance(guild,user,hour)
            rows = self._garden_rows(guild,user)
            selected = [r for r in rows if batch_id is None or r['id']==batch_id]
            if not selected:
                raise UserError('Посадка уже собрана или не принадлежит вам. Обновите сад.')
            if action == 'process':
                selected = [r for r in selected if r['ripe_hour'] is None and r['planted_hour'] < hour]
                for row in selected:
                    self.db.execute('UPDATE garden_batches SET processed_hour=? WHERE id=?',(hour,row['id']))
            elif action == 'fertilize':
                selected = [r for r in selected if r['ripe_hour'] is None and not r['fertilized']]
                for row in selected:
                    self.db.execute('UPDATE garden_batches SET fertilized=1 WHERE id=?',(row['id'],))
            elif action == 'collect':
                selected = [r for r in selected if r['ripe_hour'] is not None]
                for row in selected:
                    self.db.execute('UPDATE garden_batches SET collected=1 WHERE id=?',(row['id'],))
            else:
                raise UserError('Неизвестное действие сада.')
            if not selected:
                raise UserError('Для выбранных посадок это действие сейчас не требуется.')
            return selected

    def garden_owners(self, guild):
        return [r[0] for r in self.db.execute('SELECT DISTINCT user_id FROM garden_batches WHERE guild_id=? AND collected=0',(guild,))]

    @staticmethod
    def garden_needs(row, hour):
        growing = row['ripe_hour'] is None
        return (growing and row['planted_hour'] < hour and row['processed_hour'] != hour,
                growing and not row['fertilized'])

    def garden_pending(self, guild, user, now=None):
        now = now or datetime.now(UTC)
        rows = self.garden_snapshot(guild,user,now)
        events = [('ripe',str(r['id'])) for r in rows if r['ripe_hour'] is not None]
        hour = hour_at(now)
        if now.minute >= 50 and any(any(self.garden_needs(r,hour)) for r in rows):
            events.append(('process',str(hour)))
        return [(kind,key) for kind,key in events if not self.db.execute(
            'SELECT 1 FROM garden_deliveries WHERE guild_id=? AND user_id=? AND kind=? AND event_key=?',
            (guild,user,kind,key)).fetchone()]

    def garden_claim(self, guild, user, kind, key, now=None):
        with self.transaction():
            now = resolve_now(now)
            hour = hour_at(now)
            self._garden_advance(guild,user,hour)
            rows = self._garden_rows(guild,user)
            if kind == 'process':
                rows = [r for r in rows if any(self.garden_needs(r,hour))]
                if str(hour)!=key or now.minute < 50:
                    return []
            elif kind == 'ripe':
                rows = [r for r in rows if str(r['id'])==key and r['ripe_hour'] is not None]
            else:
                raise UserError('Неизвестный тип уведомления сада.')
            if not rows:
                return []
            result=self.db.execute('INSERT OR IGNORE INTO garden_deliveries VALUES(?,?,?,?,?)',(guild,user,kind,key,'sending'))
            if not result.rowcount:
                return []
            if kind == 'process':
                for row in rows:
                    process,fertilize=self.garden_needs(row,hour)
                    self.db.execute('INSERT INTO garden_reminder_targets VALUES(?,?,?,?,?,?)',
                        (guild,user,key,row['id'],int(process),int(fertilize)))
            return rows

    def garden_reminder_action(self,guild,user,key,action,now=None):
        if action not in ('process','fertilize'):
            raise UserError('Неизвестное действие сада.')
        with self.transaction():
            now = resolve_now(now)
            hour = hour_at(now)
            delivery=self.db.execute("SELECT state FROM garden_deliveries WHERE guild_id=? AND user_id=? AND kind='process' AND event_key=?",
                                     (guild,user,key)).fetchone()
            if not delivery or delivery['state'] not in ('sending','sent','uncertain'):
                raise UserError('Это напоминание больше недоступно. Откройте /сад.')
            if action == 'process' and str(hour)!=key:
                raise UserError('Час этого напоминания уже прошёл. Откройте /сад для обработки в текущем часу. Если обработали в игре, но забыли отметку, выберите «Исправить PayDay».')
            targets=list(self.db.execute('SELECT * FROM garden_reminder_targets WHERE guild_id=? AND user_id=? AND event_key=?',
                                         (guild,user,key)))
            if not targets:
                raise UserError('Посадки этого напоминания не найдены. Откройте /сад.')
            self._garden_advance(guild,user,hour)
            flags={r['batch_id']:r for r in targets}
            updated=0
            for row in self._garden_rows(guild,user):
                target=flags.get(row['id'])
                if not target or row['ripe_hour'] is not None:
                    continue
                if action == 'process' and target['needs_process'] and row['processed_hour'] != hour:
                    self.db.execute('UPDATE garden_batches SET processed_hour=? WHERE id=?',(hour,row['id']))
                    updated+=1
                elif action == 'fertilize' and target['needs_fertilize'] and not row['fertilized']:
                    self.db.execute('UPDATE garden_batches SET fertilized=1 WHERE id=?',(row['id'],))
                    updated+=1
            return updated

    def garden_correct_remaining(self,guild,user,batch_id,remaining,expected,request,now=None):
        if type(remaining) is not int or not 0 <= remaining <= max(c[1] for c in CROPS.values()):
            raise UserError('Осталось PayDay: целое число от 0 до срока культуры.')
        with self.transaction():
            hour=hour_at(resolve_now(now))
            self._garden_advance(guild,user,hour)
            request='garden-correction:'+request
            if self.db.execute('SELECT 1 FROM garden_requests WHERE guild_id=? AND user_id=? AND request_id=?',
                               (guild,user,request)).fetchone():
                return False
            row=next((r for r in self._garden_rows(guild,user) if r['id']==batch_id),None)
            if not row or row['ripe_hour'] is not None:
                raise UserError('Исправлять можно только растущую посадку. Обновите /сад.')
            required=CROPS[row['crop']][1]
            if remaining > required:
                raise UserError(f'Осталось PayDay: от 0 до {required}.')
            if any(row[key]!=expected.get(key) for key in ('progress','checked_hour')):
                raise UserError('Посадка изменилась. Обновите /сад и откройте исправление снова.')
            self.db.execute('UPDATE garden_batches SET progress=?,checked_hour=?,planted_hour=?,ripe_hour=? WHERE id=?',
                (required-remaining,hour,min(row['planted_hour'],hour-1),hour if remaining==0 else None,batch_id))
            self.db.execute('INSERT INTO garden_requests VALUES(?,?,?)',(guild,user,request))
            return True

    def garden_delivery_result(self,guild,user,kind,key,state):
        self.db.execute('UPDATE garden_deliveries SET state=? WHERE guild_id=? AND user_id=? AND kind=? AND event_key=?',
                        (state,guild,user,kind,key))
