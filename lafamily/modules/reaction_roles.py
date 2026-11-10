"""Выдача и снятие роли по реакциям на привязанном сообщении."""

import asyncio

import discord
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.command_groups import command_group
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.permissions import require_access_async

def emoji_key(emoji):
    return emoji.id if emoji.id is not None else emoji.name.replace('\ufe0f', '')


class ReactionRoles:
    def __init__(self, client, store, guild_id):
        self.client, self.store, self.guild_id = client, store, guild_id
        self.lock = asyncio.Lock()

    @staticmethod
    def check_role(guild, role, *, granting=True):
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            raise UserError('Боту нужно право «Управлять ролями».')
        if role is None or role.guild.id != guild.id or not role.is_assignable():
            raise UserError('Выберите обычную роль ниже роли бота.')
        permissions = role.permissions
        if granting and (permissions.administrator or permissions.manage_roles or permissions.manage_guild):
            raise UserError('Для выдачи по реакции выберите роль без прав администратора и управления ролями или сервером.')

    async def configure(self, interaction, channel, role, grant, revoke, message_id=None):
        guild = interaction.guild
        if guild.id != self.guild_id or channel.guild.id != self.guild_id:
            raise UserError('Выберите канал настроенного сервера.')
        self.check_role(guild, role)
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.read_message_history and permissions.add_reactions
                and permissions.manage_messages):
            raise UserError('В канале боту нужны просмотр, чтение истории, добавление реакций и управление сообщениями.')
        if message_id is None and not permissions.send_messages:
            raise UserError('Боту нужно право отправлять сообщения в выбранном канале.')
        if message_id is not None and (not message_id.isascii() or not message_id.isdigit()
                                       or not 17 <= len(message_id) <= 20):
            raise UserError('Укажите ID сообщения из выбранного канала.')
        grant, revoke = (discord.PartialEmoji.from_str(value.strip()) for value in (grant, revoke))
        if not emoji_key(grant) or not emoji_key(revoke) or emoji_key(grant) == emoji_key(revoke):
            raise UserError('Укажите два разных эмодзи для получения и снятия роли.')

        async with self.lock:
            guild_id = str(self.guild_id)
            await database_call(self.store, 'ensure_channel_separation', guild_id, str(channel.id))
            if message_id is None:
                message = await channel.send(f'**Роль {role.mention}**\n'
                    f'{grant} Получить роль\n{revoke} Снять роль', allowed_mentions=discord.AllowedMentions.none())
            else:
                try:
                    message = await channel.fetch_message(int(message_id))
                except discord.NotFound as error:
                    raise UserError('Сообщение в выбранном канале не найдено.') from error
            # У Unicode-реакций discord.py возвращает строку, у серверных — объект эмодзи.
            own_reactions = {emoji_key(discord.PartialEmoji.from_str(str(reaction.emoji)))
                             for reaction in message.reactions if reaction.me}
            added = []
            try:
                for emoji in (grant, revoke):
                    if emoji_key(emoji) not in own_reactions:
                        await message.add_reaction(emoji)
                        added.append(emoji)
                await database_call(self.store, 'configure_reaction_role', guild_id, str(interaction.user.id),
                                    str(channel.id), str(message.id), str(role.id), str(grant), str(revoke))
            except Exception as error:
                try:
                    if message_id is None:
                        await message.delete()
                    else:
                        for emoji in added:
                            await message.remove_reaction(emoji, self.client.user)
                except Exception as cleanup_error:
                    log_error('Не удалось убрать неподключённую карточку реакций', cleanup_error)
                if isinstance(error, discord.HTTPException) and error.status == 400:
                    raise UserError('Discord не принял один из эмодзи. Выберите доступные эмодзи и повторите команду.') from error
                raise
        return message

    async def handle(self, payload):
        if (payload.guild_id != self.guild_id or payload.user_id == self.client.user.id
                or (payload.member is not None and payload.member.bot)):
            return
        try:
            async with self.lock:
                settings = await database_call(self.store, 'reaction_role_settings', str(self.guild_id))
                if (not settings or settings['channel_id'] != str(payload.channel_id)
                        or settings['message_id'] != str(payload.message_id)):
                    return
                key = emoji_key(payload.emoji)
                grant_key = emoji_key(discord.PartialEmoji.from_str(settings['grant_emoji']))
                revoke_key = emoji_key(discord.PartialEmoji.from_str(settings['revoke_emoji']))
                if key not in (grant_key, revoke_key):
                    return
                guild = self.client.get_guild(self.guild_id)
                if guild is None:
                    return
                role = guild.get_role(int(settings['role_id']))
                granting = key == grant_key
                self.check_role(guild, role, granting=granting)
                member = payload.member or await guild.fetch_member(payload.user_id)
                if member.bot:
                    return
                change_role = member.add_roles if granting else member.remove_roles
                await change_role(role, reason='Роль по реакции на карточке МП')
                channel = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
                message = channel.get_partial_message(payload.message_id)
                # Сбрасываем только реакцию участника, чтобы эту же кнопку можно было нажать снова.
                await message.remove_reaction(payload.emoji, member)
        except discord.NotFound:
            return
        except Exception as error:
            log_error('Не удалось обработать реакцию для роли МП', error)


def register_reaction_roles(tree, service):
    group = command_group(tree, service.guild_id, 'роль', 'Настройка выдачи ролей')

    @group.command(name='выдача', description='Настроить получение и снятие роли по реакциям',
                   extras={'access': 'admin'})
    @app_commands.rename(channel='канал', role='роль', grant='эмодзи-получения', revoke='эмодзи-снятия',
                         message_id='id-сообщения')
    @app_commands.describe(grant='Эмодзи для получения роли', revoke='Эмодзи для снятия роли',
                           message_id='ID сообщения из выбранного канала. Без ID бот создаст карточку.')
    async def configure(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role,
                        grant: app_commands.Range[str, 1, 100], revoke: app_commands.Range[str, 1, 100],
                        message_id: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, 'admin')
        message = await service.configure(interaction, channel, role, grant, revoke, message_id)
        await interaction.edit_original_response(content=f'Роль настроена. Открыть сообщение: {message.jump_url}',
                                                 allowed_mentions=discord.AllowedMentions.none())
