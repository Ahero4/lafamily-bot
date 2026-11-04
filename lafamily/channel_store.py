"""Канал с автоматической очисткой нельзя совмещать с другими модулями."""
import json

from lafamily.errors import UserError


class ChannelStore:
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
        uses = (
            ('settings', 'channel_id'), ('onboarding_settings', 'entry_id'),
            ('onboarding_settings', 'review_id'), ('announcement_settings', 'channel_id'),
            ('announcement_templates', 'channel_id'), ('release_settings', 'channel_id'),
            ('admin_report_settings', 'channel_id'), ('database_backup_settings', 'channel_id'),
            ('onboarding_requests', 'review_channel_id'),
            ('reports', 'channel_id'), ('release_deliveries', 'channel_id'),
            ('planned_events', 'channel_id'),
        )
        for table, column in uses:
            if self.db.execute(f'SELECT 1 FROM {table} WHERE guild_id=? AND {column}=? LIMIT 1',
                               (guild_id, channel_id)).fetchone():
                raise UserError('Канал уже используется другим модулем. Для цен нужен отдельный канал с очисткой.')
        # Картинка может оставаться в прежнем хранилище после смены его настройки.
        for row in self.db.execute('SELECT image_ref FROM announcement_templates WHERE guild_id=? AND image_ref IS NOT NULL', (guild_id,)):
            if str(json.loads(row['image_ref'])['channel']) == channel_id:
                raise UserError('В этом канале хранятся картинки анонсов. Для цен выберите отдельный канал.')
