"""Решение по заявке хранится отдельно от доставки уведомления и карточки."""
from lafamily.errors import UserError


class OnboardingStore:
    def init_onboarding(self):
        additions = {
            'name': "TEXT NOT NULL DEFAULT ''", 'static': "TEXT NOT NULL DEFAULT ''",
            'actor_id': 'TEXT', 'reason': "TEXT NOT NULL DEFAULT ''", 'review_channel_id': 'TEXT',
            # У старых решений результат DM неизвестен: повторно их не рассылаем.
            'notification_state': "TEXT NOT NULL DEFAULT 'legacy'",
        }
        with self.transaction():
            existing = {r['name'] for r in self.db.execute('PRAGMA table_info(onboarding_requests)')}
            for column, definition in additions.items():
                if column not in existing:
                    self.db.execute(f'ALTER TABLE onboarding_requests ADD COLUMN {column} {definition}')

    def onboarding_settings(self, guild_id):
        row = self.db.execute('SELECT * FROM onboarding_settings WHERE guild_id=?', (guild_id,)).fetchone()
        return dict(row) if row else None

    def configure_onboarding(self, guild_id, entry_id, review_id, role_id):
        with self.transaction():
            for channel in (entry_id, review_id):
                self.ensure_channel_separation(guild_id, channel)
            self.db.execute('INSERT OR REPLACE INTO onboarding_settings VALUES (?, ?, ?, ?)',
                            (guild_id, entry_id, review_id, role_id))

    def onboarding_request(self, message_id):
        row = self.db.execute('SELECT * FROM onboarding_requests WHERE message_id=?', (message_id,)).fetchone()
        return dict(row) if row else None

    def save_onboarding_request(self, message_id, guild_id, user_id, role_id, name='', static='', review_channel_id=None):
        with self.transaction():
            # Channel settings may change while Discord resolves the review channel.
            self.ensure_channel_separation(guild_id, review_channel_id)
            self.db.execute('''INSERT INTO onboarding_requests
                (message_id,guild_id,user_id,role_id,name,static,review_channel_id) VALUES (?,?,?,?,?,?,?)''',
                (message_id,guild_id,user_id,role_id,name,static,review_channel_id))

    def attach_onboarding_message(self, pending_id, message_id, guild_id, channel_id):
        with self.transaction():
            row = self.onboarding_request(pending_id)
            if not row or row['guild_id'] != guild_id or row['review_channel_id'] != channel_id:
                raise UserError('Заявка не найдена в этом канале.')
            self.db.execute('UPDATE onboarding_requests SET message_id=? WHERE message_id=?', (message_id,pending_id))

    def discard_pending_onboarding(self, pending_id):
        if not pending_id.startswith('pending:'):
            raise ValueError('Only unsent submissions can be discarded')
        self.db.execute('DELETE FROM onboarding_requests WHERE message_id=? AND approved=0', (pending_id,))

    def reject_onboarding(self, message_id, actor_id, reason):
        with self.transaction():
            row = self.onboarding_request(message_id)
            if not row or row['approved']:
                raise UserError('Заявка уже обработана или не найдена.')
            self.db.execute("UPDATE onboarding_requests SET approved=-1,actor_id=?,reason=?,notification_state='pending' WHERE message_id=?",
                            (actor_id,reason,message_id))
            self.audit(row['guild_id'],actor_id,'onboarding.rejected',{'message_id':message_id,'reason':reason})

    def approve_onboarding(self, message_id, actor_id, name=None, static=None):
        with self.transaction():
            row = self.onboarding_request(message_id)
            if not row or row['approved']:
                raise UserError('Заявка уже обработана или не найдена.')
            self.db.execute("""UPDATE onboarding_requests SET approved=1,actor_id=?,name=COALESCE(?,name),
                static=COALESCE(?,static),notification_state='pending' WHERE message_id=?""", (actor_id,name,static,message_id))
            self.audit(row['guild_id'],actor_id,'onboarding.approved',{'message_id':message_id,'user_id':row['user_id']})

    def claim_onboarding_notification(self, message_id):
        # До отправки фиксируем попытку: обрыв процесса не создаёт повторные DM.
        result = self.db.execute("""UPDATE onboarding_requests SET notification_state='sending'
            WHERE message_id=? AND approved<>0 AND notification_state IN ('pending','failed')""", (message_id,))
        return bool(result.rowcount)

    def finish_onboarding_notification(self, message_id, state):
        if state not in ('sent','failed','uncertain'):
            raise ValueError('Invalid onboarding notification state')
        self.db.execute("UPDATE onboarding_requests SET notification_state=? WHERE message_id=? AND notification_state='sending'",
                        (state,message_id))
