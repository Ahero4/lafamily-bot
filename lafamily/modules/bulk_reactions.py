"""Массовая обработка истории сообщений сервера."""
import asyncio
from dataclasses import dataclass
import logging

import discord
from aiohttp import ClientError
from discord import app_commands
from discord.http import Route

from lafamily.errors import UserError
from lafamily.modules.reaction_subscriptions import parse_emoji, register_subscriptions
from lafamily.permissions import check_access

logger = logging.getLogger(__name__)


@dataclass
class Result:
    confirmed: int = 0
    failed: int = 0
    indexing: bool = False

    def summary(self):
        summary = (f'Реакция подтверждена на сообщениях: {self.confirmed}. '
                   f'Не удалось поставить: {self.failed}.')
        if self.indexing:
            summary += ' Discord ещё индексирует историю. В результатах могут отсутствовать старые сообщения.'
        return summary


async def search_page(http, route, params):
    for attempt in range(6):
        try:
            page = await http.request(route, params=params)
        except discord.Forbidden as error:
            raise UserError('Discord запретил поиск. Проверьте доступ к истории и настройку Message Content Intent.') from error
        except discord.HTTPException as error:
            raise UserError(f'Поиск Discord недоступен (HTTP {error.status}, код {error.code}).') from error
        if not isinstance(page, dict):
            raise UserError('Discord вернул неожиданный ответ поиска.')
        if page.get('code') != 110000:
            return page
        # discord.py returns the JSON body for all 2xx responses, including 202.
        delay = page.get('retry_after', 0)
        if attempt == 5 or not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
            raise UserError('Discord ещё готовит поисковый индекс. Повторите команду позже.')
        await asyncio.sleep(delay or 1)


async def apply_reactions(client, guild_id, player_id, emoji, before, result):
    route = Route('GET', '/guilds/{guild_id}/messages/search', guild_id=guild_id)
    cursor = before
    while True:
        page = await search_page(client.http, route, {
            'author_id': [str(player_id)], 'max_id': str(cursor), 'limit': 25,
            'sort_by': 'timestamp', 'sort_order': 'desc', 'include_nsfw': 'true',
        })
        result.indexing |= bool(page.get('doing_deep_historical_index'))
        groups = page.get('messages')
        if not isinstance(groups, list):
            raise UserError('Discord вернул результат поиска без списка сообщений.')
        if not groups:
            if page.get('total_results', 0) > 0:
                raise UserError('Поиск Discord сообщил о результатах, но не вернул сообщения. Повторите позже.')
            return

        hits = {}
        for group in groups:
            if not isinstance(group, list):
                raise UserError('Discord вернул некорректную группу сообщений.')
            for raw in group:
                try:
                    if (raw.get('hit') is False or int(raw.get('guild_id', guild_id)) != guild_id
                            or int(raw['author']['id']) != player_id):
                        continue
                    message_id, channel_id = int(raw['id']), int(raw['channel_id'])
                except (AttributeError, KeyError, TypeError, ValueError) as error:
                    raise UserError('Discord вернул некорректные данные сообщения.') from error
                if 0 < message_id < cursor and channel_id > 0:
                    hits[message_id] = channel_id
        if not hits:
            raise UserError('Поиск Discord не вернул более ранних сообщений выбранного участника.')
        for message_id, channel_id in hits.items():
            channel = client.get_partial_messageable(channel_id, guild_id=guild_id)
            try:
                await channel.get_partial_message(message_id).add_reaction(emoji)
            except discord.HTTPException as error:
                result.failed += 1
                if error.code == 10014 and emoji.id is None:
                    raise UserError('Discord не распознал эмодзи.') from error
            else:
                result.confirmed += 1
        # Search omits reactions and can return short pages. Advance by ID, independent of offsets and totals.
        cursor = min(hits)


async def finish(interaction, text):
    if not interaction.is_expired():
        try:
            await interaction.edit_original_response(content=text)
            return
        except discord.HTTPException:
            pass
    try:
        await interaction.user.send(text)
    except discord.HTTPException:
        logger.warning('Не удалось доставить личный итог операции.')


def register_bulk_reactions(tree, guild_id, store):
    group = app_commands.Group(name='реакции', description='Управление реакциями', guild_only=True,
                               default_permissions=discord.Permissions(administrator=True))
    tree.add_command(group, guild=discord.Object(id=guild_id))
    running = False

    @group.command(name='массово', description='Поставить реакцию на сообщения участника',
                   extras={'access': 'admin', 'hidden': True})
    @app_commands.describe(игрок='Участник сервера', реакция='Вставьте один эмодзи, например 🐰')
    async def bulk(interaction: discord.Interaction, игрок: discord.Member, реакция: app_commands.Range[str, 1, 100]):
        nonlocal running
        check_access(interaction, None, 'admin')
        if interaction.guild_id != guild_id or игрок.guild.id != guild_id:
            raise UserError('Выберите участника сервера семьи.')
        emoji = parse_emoji(реакция)
        if running:
            raise UserError('Обработка уже идёт. Дождитесь завершения.')
        running = True
        try:
            await interaction.response.send_message('Ищу сообщения участника. Если это займёт больше 15 минут, '
                                                    'итог придёт в личные сообщения, если они открыты.', ephemeral=True)
            result = Result()
            try:
                await apply_reactions(tree.client, guild_id, игрок.id, emoji, interaction.id, result)
            except (ClientError, OSError, discord.RateLimited):
                text = f'Связь с Discord прервалась. Повторите команду позже. {result.summary()}'
            except UserError as error:
                text = f'Обработка остановлена. {error} {result.summary()}'
            else:
                text = result.summary()
            await finish(interaction, text)
        finally:
            running = False

    register_subscriptions(group, store, guild_id)
