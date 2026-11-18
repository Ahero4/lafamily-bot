"""Массовая обработка истории сообщений сервера."""
from dataclasses import dataclass
import logging

import discord
from discord import app_commands

from lafamily.errors import UserError
from lafamily.modules.reaction_roles import emoji_key
from lafamily.permissions import check_access

logger = logging.getLogger(__name__)


@dataclass
class Result:
    added: int = 0
    existing: int = 0
    failed: int = 0
    unavailable: int = 0

    def summary(self):
        return (f'Добавлено реакций: {self.added}. Уже стояло: {self.existing}. '
                f'Не удалось поставить: {self.failed}. Недоступных каналов или списков веток: {self.unavailable}.')


async def destinations(guild, me, result):
    try:
        active = await guild.active_threads()
    except discord.HTTPException:
        result.unavailable += 1
        active = guild.threads
    for thread in active:
        yield thread
    for channel in guild.channels:
        if isinstance(channel, discord.abc.Messageable):
            yield channel
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            continue
        permissions = channel.permissions_for(me)
        if not permissions.view_channel or not permissions.read_message_history:
            if isinstance(channel, discord.ForumChannel):
                result.unavailable += 1
            continue
        archives = [channel.archived_threads(limit=None)]
        if isinstance(channel, discord.TextChannel) and not channel.is_news():
            archives.append(channel.archived_threads(limit=None, private=True, joined=not permissions.manage_threads))
        for archive in archives:
            try:
                async for thread in archive:
                    yield thread
            except discord.HTTPException:
                result.unavailable += 1


async def apply_reactions(guild, player_id, emoji, before, result):
    key = emoji_key(emoji)
    visited = set()
    me = guild.me
    if me is None:
        raise UserError('Discord не предоставил данные бота на сервере.')
    async for channel in destinations(guild, me, result):
        if channel.id in visited:
            continue
        visited.add(channel.id)
        try:
            permissions = channel.permissions_for(me)
        except discord.ClientException:
            # A thread can outlive its parent in the local channel cache.
            result.unavailable += 1
            continue
        if not permissions.view_channel or not permissions.read_message_history:
            result.unavailable += 1
            continue
        try:
            async for message in channel.history(limit=None, before=before):
                if message.author.id != player_id:
                    continue
                matching = next((r for r in message.reactions
                                 if emoji_key(discord.PartialEmoji.from_str(str(r.emoji))) == key), None)
                if matching is not None and matching.me:
                    result.existing += 1
                    continue
                if not permissions.add_reactions and matching is None:
                    result.failed += 1
                    continue
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException as error:
                    result.failed += 1
                    if error.code == 10014 and emoji.id is None:
                        raise UserError('Discord не распознал эмодзи.') from error
                    if error.code == 50083:  # Discord refuses writes to archived threads.
                        result.unavailable += 1
                        break
                else:
                    result.added += 1
        except discord.HTTPException:
            result.unavailable += 1


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


def register_bulk_reactions(tree, guild_id):
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
        emoji = discord.PartialEmoji.from_str(реакция.strip())
        if emoji.id is None and (not emoji.name or emoji.name.isascii() or any(c.isspace() for c in emoji.name)):
            raise UserError('Вставьте один эмодзи.')
        if running:
            raise UserError('Обработка уже идёт. Дождитесь завершения.')
        running = True
        try:
            await interaction.response.send_message('Обрабатываю историю. Если это займёт больше 15 минут, '
                                                    'итог придёт в личные сообщения, если они открыты.', ephemeral=True)
            result = Result()
            try:
                await apply_reactions(interaction.guild, игрок.id, emoji, discord.Object(id=interaction.id), result)
            except UserError as error:
                text = f'Обработка остановлена. {error} {result.summary()}'
            else:
                text = result.summary()
            await finish(interaction, text)
        finally:
            running = False
