"""Persistent personal gardens; hourly boundaries are UTC integer hours."""
from datetime import UTC, datetime
from lafamily.errors import UserError

CROPS = {'cabbage': ('Капуста', 4), 'pumpkin': ('Тыква', 5),
         'mandarin': ('Мандарины', 6), 'pineapple': ('Ананасы', 7)}


def hour_at(now=None):
    return int((now or datetime.now(UTC)).timestamp()) // 3600


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

    def garden_plant(self, guild, user, request, quantities, now=None):
        if set(quantities) - set(CROPS) or any(type(v) is not int or v < 0 or v > 9 for v in quantities.values()) or not 1 <= sum(quantities.values()) <= 9:
            raise UserError('Укажите целые количества растений: суммарно от 1 до 9.')
        hour = hour_at(now)
        with self.transaction():
            self._garden_advance(guild,user,hour)
            if self.db.execute('SELECT 1 FROM garden_requests WHERE guild_id=? AND user_id=? AND request_id=?',(guild,user,request)).fetchone():
                return
            if sum(r['quantity'] for r in self._garden_rows(guild,user)) + sum(quantities.values()) > 9:
                raise UserError('В саду всего 9 мест. Сначала соберите созревшие растения.')
            self.db.execute('INSERT INTO garden_requests VALUES(?,?,?)',(guild,user,request))
            for crop, quantity in quantities.items():
                if quantity:
                    self.db.execute('INSERT INTO garden_batches(guild_id,user_id,request_id,crop,quantity,planted_hour,checked_hour) VALUES(?,?,?,?,?,?,?)',
                                    (guild,user,request,crop,quantity,hour,hour))

    def garden_action(self, guild, user, action, batch_id=None, now=None):
        hour = hour_at(now)
        with self.transaction():
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

    def garden_pending(self, guild, user, now=None):
        now = now or datetime.now(UTC)
        rows = self.garden_snapshot(guild,user,now)
        events = [('ripe',str(r['id'])) for r in rows if r['ripe_hour'] is not None]
        hour = hour_at(now)
        if now.minute >= 50 and any(r['ripe_hour'] is None and r['planted_hour'] < hour and r['processed_hour'] != hour for r in rows):
            events.append(('process',str(hour)))
        return [(kind,key) for kind,key in events if not self.db.execute(
            'SELECT 1 FROM garden_deliveries WHERE guild_id=? AND user_id=? AND kind=? AND event_key=?',
            (guild,user,kind,key)).fetchone()]

    def garden_claim(self, guild, user, kind, key, now=None):
        hour = hour_at(now)
        with self.transaction():
            self._garden_advance(guild,user,hour)
            rows = self._garden_rows(guild,user)
            if kind == 'process':
                rows = [r for r in rows if r['ripe_hour'] is None and r['planted_hour'] < hour and r['processed_hour'] != hour]
                if str(hour)!=key or (now or datetime.now(UTC)).minute < 50:
                    return []
            else:
                rows = [r for r in rows if str(r['id'])==key and r['ripe_hour'] is not None]
            if not rows:
                return []
            result=self.db.execute('INSERT OR IGNORE INTO garden_deliveries VALUES(?,?,?,?,?)',(guild,user,kind,key,'sending'))
            return rows if result.rowcount else []

    def garden_delivery_result(self,guild,user,kind,key,state):
        self.db.execute('UPDATE garden_deliveries SET state=? WHERE guild_id=? AND user_id=? AND kind=? AND event_key=?',
                        (state,guild,user,kind,key))
