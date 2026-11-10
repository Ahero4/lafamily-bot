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

    @staticmethod
    def card_text(role_id, emoji):
        return (f'**Роль <@&{role_id}>**\n'
                f'Поставьте {emoji}, чтобы получить роль.\nСнимите свою реакцию {emoji}, чтобы снять роль.')

    async def configure(self, interaction, channel, role, grant, message_id=None):
        guild = interaction.guild
        if guild.id != self.guild_id or channel.guild.id != self.guild_id:
            raise UserError('Выберите канал настроенного сервера.')
        self.check_role(guild, role)
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.read_message_history and permissions.add_reactions):
            raise UserError('В канале боту нужны просмотр, чтение истории и добавление реакций.')
        if message_id is None and not permissions.send_messages:
            raise UserError('Боту нужно право отправлять сообщения в выбранном канале.')
        if message_id is not None and (not message_id.isascii() or not message_id.isdigit()
                                       or not 17 <= len(message_id) <= 20):
            raise UserError('Укажите ID сообщения из выбранного канала.')
        grant = discord.PartialEmoji.from_str(grant.strip())
        if not emoji_key(grant):
            raise UserError('Укажите эмодзи для получения роли.')

        async with self.lock:
            guild_id = str(self.guild_id)
            await database_call(self.store, 'ensure_channel_separation', guild_id, str(channel.id))
            if message_id is None:
                message = await channel.send(self.card_text(role.id, grant), allowed_mentions=discord.AllowedMentions.none())
            else:
                try:
                    message = await channel.fetch_message(int(message_id))
                except discord.NotFound as error:
                    raise UserError('Сообщение в выбранном канале не найдено.') from error
            # У Unicode-реакций discord.py возвращает строку, у серверных — объект эмодзи.
            own_reactions = {emoji_key(discord.PartialEmoji.from_str(str(reaction.emoji)))
                             for reaction in message.reactions if reaction.me}
            added = False
            try:
                if emoji_key(grant) not in own_reactions:
                    await message.add_reaction(grant)
                    added = True
                await database_call(self.store, 'configure_reaction_role', guild_id, str(interaction.user.id),
                                    str(channel.id), str(message.id), str(role.id), str(grant))
            except Exception as error:
                try:
                    if message_id is None:
                        await message.delete()
                    elif added:
                        await message.remove_reaction(grant, self.client.user)
                except Exception as cleanup_error:
                    log_error('Не удалось убрать неподключённую карточку реакций', cleanup_error)
                if isinstance(error, discord.HTTPException) and error.status == 400:
                    raise UserError('Discord не принял эмодзи. Выберите доступный эмодзи и повторите команду.') from error
                raise
        return message

    async def handle(self, payload, *, granting=True):
        payload_member = getattr(payload, 'member', None)
        if (payload.guild_id != self.guild_id or payload.user_id == self.client.user.id
                or (payload_member is not None and payload_member.bot)):
            return
        try:
            async with self.lock:
                settings = await database_call(self.store, 'reaction_role_settings', str(self.guild_id))
                if (not settings or settings['channel_id'] != str(payload.channel_id)
                        or settings['message_id'] != str(payload.message_id)):
                    return
                key = emoji_key(payload.emoji)
                grant_key = emoji_key(discord.PartialEmoji.from_str(settings['grant_emoji']))
                if key != grant_key:
                    return
                guild = self.client.get_guild(self.guild_id)
                if guild is None:
                    return
                role = guild.get_role(int(settings['role_id']))
                self.check_role(guild, role, granting=granting)
                member = payload_member or await guild.fetch_member(payload.user_id)
                if member.bot:
                    return
                change_role = member.add_roles if granting else member.remove_roles
                await change_role(role, reason='Роль по реакции на карточке МП')
        except discord.NotFound:
            return
        except Exception as error:
            log_error('Не удалось обработать реакцию для роли МП', error)

    async def upgrade_card(self):
        """Обновить карточку прежнего режима с двумя эмодзи при подключении."""
        try:
            async with self.lock:
                guild_id = str(self.guild_id)
                settings = await database_call(self.store, 'reaction_role_settings', guild_id)
                if not settings or not settings['revoke_emoji']:
                    return
                channel = await self.client.fetch_channel(int(settings['channel_id']))
                if channel.guild.id != self.guild_id:
                    return
                message = await channel.fetch_message(int(settings['message_id']))
                old_text = (f'**Роль <@&{settings["role_id"]}>**\n'
                            f'{settings["grant_emoji"]} Получить роль\n{settings["revoke_emoji"]} Снять роль')
                if message.author.id == self.client.user.id and message.content == old_text:
                    await message.edit(content=self.card_text(settings['role_id'], settings['grant_emoji']),
                                       allowed_mentions=discord.AllowedMentions.none())
                await message.remove_reaction(discord.PartialEmoji.from_str(settings['revoke_emoji']), self.client.user)
                await database_call(self.store, 'finish_reaction_role_upgrade', guild_id, settings['message_id'])
        except discord.NotFound:
            return
        except Exception as error:
            log_error('Не удалось обновить прежнюю карточку выдачи роли', error)


def register_reaction_roles(tree, service):
    group = command_group(tree, service.guild_id, 'роль', 'Настройка выдачи ролей')

    @group.command(name='выдача', description='Настроить получение и снятие роли по реакциям',
                   extras={'access': 'admin'})
    @app_commands.rename(channel='канал', role='роль', grant='эмодзи-получения', message_id='id-сообщения')
    @app_commands.describe(grant='Поставить реакцию — получить роль, снять реакцию — снять роль',
                           message_id='ID сообщения из выбранного канала. Без ID бот создаст карточку.')
    async def configure(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role,
                        grant: app_commands.Range[str, 1, 100], message_id: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, 'admin')
        message = await service.configure(interaction, channel, role, grant, message_id)
        await interaction.edit_original_response(content=f'Роль настроена. Открыть сообщение: {message.jump_url}',
                                                 allowed_mentions=discord.AllowedMentions.none())
