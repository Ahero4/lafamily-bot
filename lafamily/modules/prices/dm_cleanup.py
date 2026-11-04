"""Удаление исключительно зарегистрированных ЛС скупщика, по точному ID и содержимому."""
import hashlib
import json
import logging
from dataclasses import asdict

import discord

from lafamily.async_database import database_call
from lafamily.diagnostics import log_error

logger = logging.getLogger(__name__)


def price_dm_revision(report):
    return hashlib.sha256(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def content_hash(content):
    return hashlib.sha256(content.encode()).hexdigest()


async def cleanup_price_dms(client, store, guild_id):
    channels = {}
    for row in await database_call(store, 'stale_price_dms', str(guild_id)):
        # Реестр может содержать сообщения другого экземпляра бота после переноса БД.
        if not client.user or str(client.user.id) != row['bot_id']:
            continue
        try:
            channel_id = int(row['channel_id'])
            if channel_id not in channels:
                channels[channel_id] = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            channel = channels[channel_id]
            if (not isinstance(channel, discord.DMChannel)
                    or channel.recipient is None or str(channel.recipient.id) != row['user_id']):
                await database_call(store, 'finish_price_dm_cleanup', row['attempt_id'],
                                    'ChannelMismatch', protected=True)
                continue
            message = await channel.fetch_message(int(row['message_id']))
            if (str(message.id) != row['message_id'] or str(message.channel.id) != row['channel_id']
                    or str(message.author.id) != row['bot_id']
                    or content_hash(message.content) != row['content_hash']):
                logger.warning('ЛС скупщика не удалено: сообщение не совпало с реестром')
                await database_call(store, 'finish_price_dm_cleanup', row['attempt_id'],
                                    'MessageMismatch', protected=True)
                continue
            await message.delete()
        except discord.NotFound:
            pass  # Уже удалено пользователем или предыдущей попыткой с неоднозначным ответом.
        except Exception as error:
            log_error('Не удалось удалить устаревшее ЛС скупщика; повтор при следующем отчёте цен', error)
            await database_call(store, 'finish_price_dm_cleanup', row['attempt_id'], type(error).__name__)
            continue
        # История успешной доставки остаётся неизменной: удаляется только запись реестра DM.
        await database_call(store, 'finish_price_dm_cleanup', row['attempt_id'])
