import logging
import re
from typing import Literal
import unicodedata

import discord
from aiohttp import ClientError
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.permissions import check_access

logger = logging.getLogger(__name__)
PAGE_SIZE = 6


def _unicode_emoji(value):
    # Flags and keycaps have no standalone pictographic base.
    if re.fullmatch(r'[\U0001f1e6-\U0001f1ff]{2}|[#*0-9]\ufe0f?\u20e3|🏴[\U000e0061-\U000e007a]+\U000e007f', value):
        return True
    for part in value.split('\u200d'):
        component = re.fullmatch(r'(.)(?:\ufe0f)?(?:[\U0001f3fb-\U0001f3ff])?', part)
        if component is None:
            return False
        base = component[1]
        if '\U0001f1e6' <= base <= '\U0001f1ff' or '🏻' <= base <= '🏿':
            return False
        if (base.isascii() or (unicodedata.category(base) not in {'So', 'Sm'}
                and base not in 'ℹ‼⁉〰〽' and not '\U0001f000' <= base <= '\U0001faff')):
            return False
    return True


def parse_emoji(value, *, subscription=False):
    value = value.strip()
    emoji = discord.PartialEmoji.from_str(value)
    if subscription:
        if emoji.id is None:
            valid = _unicode_emoji(value)
        else:
            canonical = str(emoji)
            token = canonical[1:-1]
            valid = value in {canonical, token, token.removeprefix(':')} and 0 < emoji.id < 2**64
        if not valid:
            raise UserError('Вставьте один эмодзи.')
    if emoji.id is None and (not emoji.name or emoji.name.isascii() or any(c.isspace() for c in emoji.name)):
        raise UserError('Вставьте один эмодзи.')
    return emoji


async def react_to_new_message(message, store, guild_id):
    if (message.guild is None or message.guild.id != guild_id
            or message.webhook_id is not None or message.author.bot):
        return
    try:
        value = await database_call(store, 'reaction_subscription', str(guild_id), str(message.author.id))
    except UserError:  # The worker can be frozen while a restore is starting.
        return
    if value is None:
        return
    try:
        await message.add_reaction(discord.PartialEmoji.from_str(value))
    except (discord.HTTPException, discord.RateLimited, ClientError, OSError) as error:
        logger.warning('Не удалось поставить реакцию по подписке (канал %s, код %s).',
                       message.channel.id, getattr(error, 'code', type(error).__name__))


def register_subscriptions(group, store, guild_id):
    @group.command(name='подписка', description='Управление реакциями на новые сообщения игрока',
                   extras={'access': 'admin', 'hidden': True})
    @app_commands.describe(действие='Сохранить создаёт подписку или заменяет её реакцию',
                           игрок='Выберите аккаунт по имени', реакция='Вставьте один эмодзи',
                           id_аккаунта='ID из списка для изменения или удаления без выбора игрока',
                           страница='Страница списка подписок')
    async def subscriptions(interaction: discord.Interaction,
                            действие: Literal['список', 'сохранить', 'удалить'] = 'список',
                            игрок: discord.Member | None = None, реакция: app_commands.Range[str, 1, 100] | None = None,
                            id_аккаунта: str | None = None, страница: app_commands.Range[int, 1, None] = 1):
        check_access(interaction, None, 'admin')
        if interaction.guild_id != guild_id:
            raise UserError('Команда доступна на сервере семьи.')
        server = str(guild_id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if действие == 'список':
            if игрок is not None or реакция is not None or id_аккаунта is not None:
                raise UserError('Для списка укажите только страницу.')
            rows, pages = await database_call(store, 'list_reaction_subscriptions', server, страница, PAGE_SIZE)
            if страница > pages:
                raise UserError(f'Страниц в списке: {pages}.')
            lines = [f'<@{player_id}> · `{player_id}` · {emoji}' for player_id, emoji in rows]
            text = (f'Подписки · {страница}/{pages}\n' + '\n'.join(lines)) if lines else 'Подписок пока нет.'
        else:
            if страница != 1 or (игрок is None) == (id_аккаунта is None):
                raise UserError('Для изменения выберите игрока или укажите один ID из списка.')
            if игрок is not None:
                if игрок.guild.id != guild_id or игрок.bot:
                    raise UserError('Выберите аккаунт игрока на этом сервере.')
                player_id = str(игрок.id)
            else:
                if not re.fullmatch(r'[1-9][0-9]{0,19}', id_аккаунта):
                    raise UserError('Скопируйте ID аккаунта из списка подписок.')
                player_id = id_аккаунта
            if действие == 'сохранить':
                if реакция is None:
                    raise UserError('Укажите реакцию для подписки.')
                emoji = str(parse_emoji(реакция, subscription=True))
                await database_call(store, 'save_reaction_subscription', server, player_id, emoji,
                                    existing_only=игрок is None)
                text = f'Подписка сохранена. Реакция на новые сообщения <@{player_id}> — {emoji}.'
            else:
                if реакция is not None:
                    raise UserError('Для удаления укажите только игрока или его ID.')
                removed = await database_call(store, 'delete_reaction_subscription', server, player_id)
                text = 'Подписка удалена.' if removed else 'У этого игрока нет подписки.'
        await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())
