"""Durable snapshot delivery and command receipts for the family portal."""
import json
import time
from datetime import UTC, datetime

from lafamily.errors import UserError
from lafamily.modules.events.time import week_range
from lafamily.portal_profile import profile_snapshots


def check_command_deadline(command):
    try:
        expires = datetime.fromisoformat(command['expiresAt'])
        if expires.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise UserError('Некорректный срок запроса портала.') from None
    if datetime.now(UTC) >= expires:
        raise UserError('Срок запроса истёк. Повторите запись на портале.')


class PortalStore:
    def init_portal(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS portal_snapshots (
                guild TEXT, kind TEXT, key TEXT, data TEXT NOT NULL, revision INTEGER NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild,kind,key));
            CREATE TABLE IF NOT EXISTS portal_results (
                guild TEXT, id TEXT, data TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild,id));
            CREATE TABLE IF NOT EXISTS portal_cards (
                guild TEXT, event INTEGER, revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(guild,event));
            CREATE INDEX IF NOT EXISTS portal_pending ON portal_snapshots(guild,kind,sent,revision,key);
            CREATE INDEX IF NOT EXISTS portal_unsent_results ON portal_results(guild,sent);
            CREATE TABLE IF NOT EXISTS portal_dirty_plans (event INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS portal_profile_clock (id INTEGER PRIMARY KEY, rev INTEGER NOT NULL);
            INSERT OR IGNORE INTO portal_profile_clock VALUES(1,0);
        """)
        if 'revision' not in {row['name'] for row in self.db.execute('PRAGMA table_info(portal_cards)')}:
            self.db.execute('ALTER TABLE portal_cards ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')
        # The bot writes through one SQLite worker. Connection-local triggers track its writes
        # without putting executable triggers in portable database backups.
        for table in ('planned_events', 'planned_signups', 'reports', 'report_members'):
            for action in ('INSERT', 'UPDATE', 'DELETE'):
                ref = 'OLD' if action == 'DELETE' else 'NEW'
                if table in ('reports', 'report_members'):
                    mark = 'UPDATE portal_profile_clock SET rev=rev+1 WHERE id=1'
                else:
                    col = 'id' if table == 'planned_events' else 'event_id'
                    mark = f'INSERT OR IGNORE INTO portal_dirty_plans VALUES({ref}.{col})'
                self.db.execute(f'CREATE TEMP TRIGGER IF NOT EXISTS portal_{table}_{action} AFTER {action} ON {table} BEGIN {mark}; END')
        self._portal_state = {}

    def portal_remember(self, guild, kind, key, value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        revision = time.time_ns() // 1_000_000
        self.db.execute("""INSERT INTO portal_snapshots(guild,kind,key,data,revision) VALUES(?,?,?,?,?)
            ON CONFLICT(guild,kind,key) DO UPDATE SET data=excluded.data,
            revision=max(portal_snapshots.revision+1,excluded.revision),sent=0
            WHERE excluded.data<>portal_snapshots.data""", (guild, kind, key, encoded, revision))

    def portal_scan(self, guild, channels, drafts):
        allowed = frozenset(channels)
        state = self._portal_state.get(guild)
        full = state is None or state[0] != allowed
        current = {event['id']: event for event in drafts}
        week = week_range()[0]
        clock = self.db.execute('SELECT rev FROM portal_profile_clock WHERE id=1').fetchone()[0]
        dirty = [r[0] for r in self.db.execute('SELECT event FROM portal_dirty_plans')]
        profiles_changed = full or (week, clock) != state[2]
        if not (full or dirty or current != state[1] or profiles_changed):
            return
        with self.transaction():
            if full:
                ids = {r[0] for r in self.db.execute('SELECT id FROM planned_events WHERE guild_id=?', (guild,))}
                cached = [r[0] for r in self.db.execute("SELECT key FROM portal_snapshots WHERE guild=? AND kind='events'", (guild,))]
                ids.update(int(key) for key in cached if key.isdigit())
                old_drafts = {key for key in cached if key.startswith('draft:')}
            else:
                ids = set(dirty)
                old_drafts = state[1].keys()
            for number in ids:
                row = self.db.execute('SELECT channel_id,message_id FROM planned_events WHERE guild_id=? AND id=?',
                                      (guild, number)).fetchone()
                key = str(number)
                if not row or not row['message_id'] or row['channel_id'] not in allowed:
                    self.portal_close_event(guild, key)
                    continue
                event = self.planned_event(guild, number)
                self.portal_remember(guild, 'events', key, dict(
                    id=key, kind='planned', name=event['name'], startsAt=event['starts_at'],
                    capacity=event['capacity'], closed=bool(event['closed']),
                    going=event['going'], reserve=event['reserve'], maybe=event['maybe']))
            for key in old_drafts - current.keys():
                self.portal_close_event(guild, key)
            for key,event in current.items():
                if full or state[1].get(key) != event:
                    self.portal_remember(guild, 'events', key, event)
            if profiles_changed:
                self.portal_profiles(guild)
            self.db.executemany('DELETE FROM portal_dirty_plans WHERE event=?', [(n,) for n in dirty])
        self._portal_state[guild] = (allowed, current, (week, clock))

    def portal_close_event(self, guild, key):
        row = self.db.execute("SELECT data FROM portal_snapshots WHERE guild=? AND kind='events' AND key=?", (guild,key)).fetchone()
        if row:
            value = json.loads(row[0])
            if not value['closed']:
                value['closed'] = True
                self.portal_remember(guild, 'events', key, value)

    def portal_profiles(self, guild):
        profiles = profile_snapshots(self, guild)
        known = {p['discordId'] for p in profiles}
        for profile in profiles:
            self.portal_remember(guild, 'profiles', profile['discordId'], profile)
        week_start = week_range()[0]
        for row in self.db.execute("SELECT key,data FROM portal_snapshots WHERE guild=? AND kind='profiles'", (guild,)):
            if row['key'] not in known:
                zero = dict.fromkeys(('total','week','events','wins','mvps','weekEvents','weekWins','weekMvps'), 0)
                zero.update(discordId=row['key'], weekStart=week_start)
                self.portal_remember(guild, 'profiles', row['key'], zero)

    def portal_resend(self, guild):
        # Bootstrap an empty/recovered portal from durable source data.
        self.db.execute('UPDATE portal_snapshots SET sent=0 WHERE guild=?', (guild,))

    def portal_batch(self, guild):
        batch = {'guildId': guild, 'events': [], 'profiles': [], 'results': []}
        for kind in ('events', 'profiles'):
            for row in self.db.execute("""SELECT data,revision FROM portal_snapshots WHERE guild=? AND kind=?
                    AND sent=0 ORDER BY revision,key LIMIT 10""", (guild, kind)):
                batch[kind].append(dict(json.loads(row['data']), revision=row['revision']))
        for row in self.db.execute('SELECT data FROM portal_results WHERE guild=? AND sent=0 ORDER BY rowid LIMIT 25', (guild,)):
            batch['results'].append(json.loads(row[0]))
        return batch

    def portal_ack(self, batch):
        if not any(batch[key] for key in ('events','profiles','results')):
            return
        guild = batch['guildId']
        with self.transaction():
            for kind, key in [('events','id'), ('profiles','discordId')]:
                for row in batch[kind]:
                    self.db.execute('UPDATE portal_snapshots SET sent=1 WHERE guild=? AND kind=? AND key=? AND revision=?',
                                    (guild, kind, row[key], row['revision']))
            for row in batch['results']:
                self.db.execute('UPDATE portal_results SET sent=1 WHERE guild=? AND id=?', (guild, row['id']))

    def portal_replay(self, guild, command_id):
        return bool(self.db.execute('UPDATE portal_results SET sent=0 WHERE guild=? AND id=?', (guild, command_id)).rowcount)

    def portal_result(self, guild, command_id, error=None):
        result = {'id': command_id, 'ok': error is None}
        if error:
            result['error'] = error
        self.db.execute('INSERT OR IGNORE INTO portal_results(guild,id,data) VALUES(?,?,?)',
                        (guild, command_id, json.dumps(result, ensure_ascii=False)))

    def portal_signup(self, guild, cmd):
        with self.transaction():
            if self.portal_replay(guild, cmd['id']):
                return
            number = int(cmd['eventId'])
            try:
                check_command_deadline(cmd)
                self._signup_planned(guild, number, cmd['discordId'], cmd['choice'])
            except UserError as error:
                self.portal_result(guild, cmd['id'], str(error))
                return
            self.db.execute("""INSERT INTO portal_cards(guild,event) VALUES(?,?)
                ON CONFLICT(guild,event) DO UPDATE SET revision=portal_cards.revision+1""", (guild, number))
            self.portal_result(guild, cmd['id'])

    def portal_deleted_cards(self, guild, ids):
        with self.transaction():
            self.db.executemany('UPDATE planned_events SET closed=1 WHERE guild_id=? AND message_id=? AND closed=0',
                                [(guild, str(message)) for message in ids])

    def portal_active_cards(self, guild, channels):
        from lafamily.modules.events.time import timestamp
        return [dict(row) for row in self.db.execute("""SELECT id,message_id,channel_id FROM planned_events
            WHERE guild_id=? AND message_id IS NOT NULL AND closed=0 AND starts_at>?""", (guild, timestamp()))
                if row['channel_id'] in channels]

    def portal_cards(self, guild):
        return [r[0] for r in self.db.execute('SELECT event FROM portal_cards WHERE guild=? ORDER BY event', (guild,))]

    def portal_card_revision(self, guild, number):
        row = self.db.execute('SELECT revision FROM portal_cards WHERE guild=? AND event=?', (guild,number)).fetchone()
        return row[0] if row else None

    def portal_card_done(self, guild, number, revision):
        self.db.execute('DELETE FROM portal_cards WHERE guild=? AND event=? AND revision=?', (guild,number,revision))
