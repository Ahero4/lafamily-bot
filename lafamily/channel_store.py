"""Канал с автоматической очисткой нельзя совмещать с другими модулями."""
import json

from lafamily.errors import UserError


class ChannelStore:
    def family_guide_posts(self, guild_id):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM family_guide_posts WHERE guild_id=?', (guild_id,))]

    def select_family_guide_channel(self, guild_id, channel_id):
        with self.transaction():
            self.ensure_channel_separation(guild_id, channel_id)
            self.db.execute("""UPDATE family_guide_posts SET status='retiring'
                WHERE guild_id=? AND channel_id<>? AND status='target'""", (guild_id, channel_id))
            self.db.execute("""INSERT INTO family_guide_posts(guild_id,channel_id,status) VALUES (?,?,'target')
                ON CONFLICT(guild_id,channel_id) DO UPDATE SET status='target' WHERE status='retiring'""",
                (guild_id, channel_id))

    def begin_family_guide_search(self, guild_id, channel_id, last_seen_id):
        with self.transaction():
            self.db.execute("""UPDATE family_guide_posts SET search_after=COALESCE(search_after,?)
                WHERE guild_id=? AND channel_id=?""", (last_seen_id, guild_id, channel_id))
            return self.db.execute("SELECT search_after FROM family_guide_posts WHERE guild_id=? AND channel_id=?",
                                   (guild_id, channel_id)).fetchone()[0]

    def bind_family_guide(self, guild_id, channel_id, message_id):
        with self.transaction():
            self.db.execute("""UPDATE family_guide_posts SET status='retiring'
                WHERE guild_id=? AND channel_id<>? AND status='active'""", (guild_id, channel_id))
            changed = self.db.execute("""UPDATE family_guide_posts SET status='active',message_id=?,search_after=NULL
                WHERE guild_id=? AND channel_id=? AND status IN ('active','target')""",
                (message_id, guild_id, channel_id))
            if not changed.rowcount:
                raise UserError('Канал памятки уже изменён. Откройте выбор канала заново.')

    def finish_family_guide_move(self, guild_id, channel_id):
        self.db.execute("DELETE FROM family_guide_posts WHERE guild_id=? AND channel_id=? AND status='retiring'",
                        (guild_id, channel_id))

    def ensure_channel_separation(self, guild_id, channel_id, purpose='other'):
        if channel_id is None:
            return
        channel_id = str(channel_id)
        if purpose != 'prices':
            prices = self.price_settings(guild_id)
            if prices and prices['channel_id'] == channel_id:
                raise UserError('Этот канал очищается модулем цен. Выберите отдельный канал для других функций.')
            return
        # Проверка выполняется внутри транзакции настройки: порядок команд не важен.
        if self.db.execute("SELECT 1 FROM family_guide_posts WHERE guild_id=? AND channel_id=?",
                           (guild_id, channel_id)).fetchone():
            raise UserError('Здесь размещается семейная памятка. Для цен нужен отдельный канал.')
        uses = (
            ('settings', 'channel_id'), ('onboarding_settings', 'entry_id'),
            ('onboarding_settings', 'review_id'), ('announcement_settings', 'channel_id'),
            ('announcement_templates', 'channel_id'), ('release_settings', 'channel_id'),
            ('admin_report_settings', 'channel_id'), ('database_backup_settings', 'channel_id'),
            ('onboarding_requests', 'review_channel_id'),
            ('reports', 'channel_id'), ('release_deliveries', 'channel_id'),
            ('planned_events', 'channel_id'), ('recurring_notices', 'channel_id'),
            ('reaction_role_settings', 'channel_id'),
        )
        for table, column in uses:
            if self.db.execute(f'SELECT 1 FROM {table} WHERE guild_id=? AND {column}=? LIMIT 1',
                               (guild_id, channel_id)).fetchone():
                raise UserError('Канал уже используется другим модулем. Для цен нужен отдельный канал с очисткой.')
        # Картинка может оставаться в прежнем хранилище после смены его настройки.
        for row in self.db.execute('SELECT image_ref FROM announcement_templates WHERE guild_id=? AND image_ref IS NOT NULL', (guild_id,)):
            if str(json.loads(row['image_ref'])['channel']) == channel_id:
                raise UserError('В этом канале хранятся картинки анонсов. Для цен выберите отдельный канал.')
