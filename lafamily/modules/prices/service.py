"""Сравнение цен и очистка выделенного канала после успешной публикации."""
import asyncio
import hashlib
import json
import logging
from dataclasses import asdict

import discord

from lafamily.async_database import database_call
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from .parser import extract_prices, price_dm_revision
from .notifications import notify_maximum_prices
from .queue import PriceQueue,enqueue_notifications
from .formatting import RENDER_VERSION, render_prices

logger = logging.getLogger(__name__)


def is_current_price_notice(message, bot_id, guild_id, channel_id, source_id):
    """Сохраняем только собственные уведомления, ссылающиеся на текущий источник цен."""
    author = getattr(message, 'author', None)
    content = getattr(message, 'content', '') or ''
    link = f'https://discord.com/channels/{guild_id}/{channel_id}/{source_id}'
    return (bot_id is not None and getattr(author, 'id', None) == bot_id
            and ('— достигнут справочный максимум!' in content or '— цена 95–100% от максимума!' in content)
            and content.splitlines()[-1:] == [link])


class PriceService:
    def __init__(self, client, store, guild_id):
        self.client, self.store, self.guild_id = client, store, guild_id
        self.lock = asyncio.Lock()
        self.queue = PriceQueue(client,store,guild_id)

    def require_enabled(self):
        if not self.client.intents.message_content:
            raise UserError('Для чтения пересылаемых цен включите Message Content Intent в Developer Portal → Bot, '
                            'добавьте DISCORD_MESSAGE_CONTENT_INTENT=true в .env и перезапустите бота.')

    def require_channel(self, channel, guild):
        if not isinstance(channel, discord.TextChannel):
            raise UserError('Для цен выберите обычный текстовый канал.')
        me = guild.me
        permissions = channel.permissions_for(me) if me else None
        if not permissions or not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            raise UserError('Боту нужны права просмотра канала цен, отправки сообщений и вставки ссылок.')

    async def process(self, message, *, notify=True):
        if not message.guild or message.guild.id != self.guild_id:
            return 'ignored'
        if self.client.user and message.author.id == self.client.user.id:
            return 'ignored'
        self.require_enabled()
        async with self.lock:
            settings = await database_call(self.store, 'price_settings', str(self.guild_id))
            if not settings or settings['channel_id'] != str(message.channel.id):
                return 'ignored'
            report = extract_prices(message)
            if report is None:
                latest = await database_call(self.store, 'latest_price_publication', str(self.guild_id), str(message.channel.id))
                if latest:
                    try:
                        await self.cleanup_channel(await message.channel.fetch_message(int(latest['source_id'])))
                    except discord.NotFound:
                        pass
                return 'unrecognized'
            self.require_channel(message.channel, message.guild)
            payload = render_prices(report)
            fingerprint = hashlib.sha256(json.dumps({"render_version": RENDER_VERSION, "report": asdict(report)}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            source_id, guild_id = str(message.id), str(self.guild_id)
            existing = await database_call(self.store, 'price_publication', guild_id, source_id)
            if existing:
                if existing['state'] != 'published':
                    raise UserError('Предыдущая отправка этого сообщения не подтверждена. Исходник сохранён; '
                                    'автоповтор отключён, чтобы не создавать дубли. Проверьте канал и журнал ошибок.')
                if existing['fingerprint'] == fingerprint:
                    await self.record_price_update(message, report, notify=False)
                    await self.cleanup_channel(message)
                    return 'already_done'
                # Если источник отредактирован, обновляем своё сообщение вместо новой публикации.
                try:
                    result = await message.channel.fetch_message(int(existing['result_id']))
                except discord.NotFound as error:
                    raise UserError('Обработанное сообщение удалено. Исходник сохранён; автоматический повтор не выполняется.') from error
                # Очистка старых embed-карточек обязательна при переходе на Components V2.
                await result.edit(content=None, embeds=[], **payload)
                await database_call(self.store, 'save_price_publication', guild_id, source_id, existing['result_id'], fingerprint)
                await self.record_price_update(message, report, notify=notify)
                await self.cleanup_channel(message)
                return 'updated'
            claimed = await database_call(self.store, 'claim_price_publication', guild_id, str(message.channel.id), source_id, fingerprint)
            if not claimed:
                return 'already_done'
            try:
                result = await message.channel.send(**payload)
            except Exception as error:
                if isinstance(error, discord.HTTPException) and 400 <= error.status < 500 and error.status != 408:
                    await database_call(self.store, 'release_failed_price_publication', guild_id, source_id)
                    raise UserError('Discord отклонил публикацию сравнения. Исправьте права или параметры и повторите /скупщик обработать. Исходник сохранён.') from error
                await database_call(self.store, 'uncertain_price_publication', guild_id, source_id)
                log_error('Не подтверждена публикация сравнения цен; исходник сохранён', error)
                raise UserError('Отправка сравнения не подтверждена. Исходное сообщение сохранено; повтор не выполняется во избежание дублей.') from error
            await database_call(self.store, 'save_price_publication', guild_id, source_id, str(result.id), fingerprint)
            await self.record_price_update(message, report, notify=notify)
            await self.cleanup_channel(message)
            return 'published'

    async def cleanup_channel(self, source):
        # Удаление возможно только после сохранения успешной публикации.
        try:
            await database_call(self.store, 'ensure_channel_separation', str(self.guild_id), str(source.channel.id), 'prices')
        except UserError:
            logger.warning('Очистка цен отключена: канал используется другим модулем. Перенастройте каналы.')
            return
        permissions = source.channel.permissions_for(source.guild.me)
        if not getattr(permissions, 'manage_messages', False) or not getattr(permissions, 'read_message_history', False):
            logger.warning('Очистка канала цен: нужны права управления сообщениями и чтения истории')
            return
        latest = await database_call(self.store, 'latest_price_publication', str(self.guild_id), str(source.channel.id))
        if not latest or int(latest['source_id']) != source.id:
            return  # Ручная обработка старого отчёта не удаляет свежие цены.
        keep = {int(latest['source_id']), int(latest['result_id'])}
        known = await database_call(self.store, 'price_message_ids', str(self.guild_id), str(source.channel.id))
        bot_id = self.client.user.id if self.client and self.client.user else None
        try:
            # Не затрагиваем сообщения, пришедшие во время обработки новой сводки.
            async for message in source.channel.history(limit=None):
                if message.id in keep or message.pinned:
                    continue
                if bot_id is not None and getattr(getattr(message, 'author', None), 'id', None) == bot_id and str(message.id) not in known:
                    notice_source = (getattr(message, 'content', '') or '').split('/')[-1]
                    if not (notice_source.isascii() and notice_source.isdigit() and is_current_price_notice(
                            message,bot_id,self.guild_id,source.channel.id,notice_source)):
                        continue  # Исторические карточки других модулей тоже сохраняются.
                if is_current_price_notice(message, self.client.user.id if self.client and self.client.user else None,
                                           self.guild_id, source.channel.id, latest['source_id']):
                    continue
                if message.id > int(latest['source_id']) and extract_prices(message) is not None:
                    continue  # Новый исходник ещё ждёт обработки.
                try:
                    current = await source.channel.fetch_message(message.id)
                    if not current.pinned:  # Повторная проверка закрепления непосредственно перед удалением.
                        await current.delete()
                except discord.NotFound:
                    continue
        except discord.DiscordException as error:
            log_error('Не удалось завершить очистку канала цен', error)

    async def record_price_update(self, message, report, *, notify):
        guild_id, source_id = str(self.guild_id), str(message.id)
        revision = price_dm_revision(report)
        if not await database_call(self.store, 'activate_price_dm_revision', guild_id, source_id, revision):
            return
        await database_call(self.store, 'save_price_history', guild_id, source_id, revision, report)
        if notify:
            await enqueue_notifications(self.store, message, report)
            await self.queue.drain()

    async def notify_subscribers(self, message, report):
        # Сбой личного уведомления не меняет результат уже опубликованного сравнения цен.
        try:
            await notify_maximum_prices(self.store, message, report)
        except Exception as error:
            log_error('Не подтверждена отправка уведомлений о максимальных ценах; автоповтор отключён', error)

    async def handle(self, message):
        if not self.client.intents.message_content:
            return
        try:
            await self.process(message)
        except UserError as error:
            logger.warning('%s', error)
        except Exception as error:
            log_error('Ошибка автоматического сравнения цен; исходник сохранён', error)

    async def handle_edit(self, payload):
        if payload.guild_id != self.guild_id or not self.client.intents.message_content:
            return
        settings = await database_call(self.store, 'price_settings', str(self.guild_id))
        if not settings or str(payload.channel_id) != settings['channel_id']:
            return
        try:
            channel = await self.client.fetch_channel(payload.channel_id)
            await self.handle(await channel.fetch_message(payload.message_id))
        except discord.NotFound:
            return
