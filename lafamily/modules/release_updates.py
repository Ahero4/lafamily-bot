"""Публикация пропущенных выпусков из истории версий в выбранный канал."""
import asyncio
from io import BytesIO

import discord

from lafamily.async_database import database_call
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from lafamily.release import RELEASES

TITLE_PREFIX = 'LaFamily Bot • обновление '


def check_channel(channel, guild):
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        raise UserError('Выберите текстовый канал этого сервера.')
    permissions = channel.permissions_for(guild.me)
    if not (permissions.view_channel and permissions.send_messages and permissions.embed_links
            and permissions.read_message_history):
        raise UserError('Боту нужны просмотр канала, отправка сообщений, вставка ссылок и чтение истории сообщений.')


class ReleaseUpdates:
    def __init__(self, client, store, guild_id):
        self.client, self.store, self.guild_id = client, store, guild_id
        self.lock = asyncio.Lock()

    async def _existing(self, channel):
        versions = {}
        # Читаем всю историю до отправки: неполный результат не должен создавать дубли.
        async for message in channel.history(limit=None):
            if message.author.id != self.client.user.id:
                continue
            for embed in message.embeds:
                title = embed.title or ''
                if title.startswith(TITLE_PREFIX):
                    version = title[len(TITLE_PREFIX):]
                    if version in RELEASES:
                        versions[version] = str(message.id)
        return versions

    async def _publish(self, channel, existing):
        guild_id, channel_id = str(self.guild_id), str(channel.id)
        result = {'published': [], 'existing': [], 'blocked': []}
        for version, notes in RELEASES.items():
            if version in existing:
                await database_call(self.store, 'save_release_delivery', guild_id, channel_id,
                                    version, existing[version])
                result['existing'].append(version)
                continue
            # Учёт по каналу: опубликованное в другом канале не блокирует новый.
            # pending/uncertain без найденного поста не повторяем при неоднозначной отправке.
            if not await database_call(self.store, 'claim_release_delivery', guild_id, channel_id, version):
                result['blocked'].append(version)
                continue
            try:
                embed = discord.Embed(title=TITLE_PREFIX + version, description=notes[:4000], colour=0x43B581)
                files = []
                if len(notes) > 4000:
                    files = [discord.File(BytesIO(notes.encode('utf-8')), filename=f'update-{version}.md')]
                message = await channel.send(embed=embed, files=files,
                                             allowed_mentions=discord.AllowedMentions.none())
                await database_call(self.store, 'save_release_delivery', guild_id, channel_id, version, str(message.id))
                result['published'].append(version)
            except Exception as error:
                await database_call(self.store, 'uncertain_release_delivery', guild_id, channel_id, version)
                log_error(f'Не подтверждена отправка выпуска {version}; автоматический повтор заблокирован', error)
                raise UserError(f'Успешно отправлено выпусков: {len(result["published"])}. '
                                f'Отправка {version} не подтверждена. Проверьте канал и журнал. '
                                'Повтор команды продолжит остальные выпуски без дублирования найденных постов.') from error
        return result

    async def configure(self, interaction, channel):
        async with self.lock:
            check_channel(channel, interaction.guild)
            existing = await self._existing(channel)
            await database_call(self.store, 'configure_release_channel', str(self.guild_id),
                                str(interaction.user.id), str(channel.id))
            return await self._publish(channel, existing)

    async def announce(self):
        async with self.lock:
            try:
                settings = await database_call(self.store, 'release_channel', str(self.guild_id))
                if not settings:
                    return
                channel = await self.client.fetch_channel(int(settings['channel_id']))
                if channel.guild.id != self.guild_id:
                    raise UserError('Канал обновлений принадлежит другому серверу.')
                check_channel(channel, channel.guild)
                existing = await self._existing(channel)
                result = await self._publish(channel, existing)
                if result['blocked']:
                    raise UserError('Есть неподтверждённые публикации. Проверьте канал и журнал.')
            except Exception as error:
                log_error('Не удалось опубликовать все обновления; проверьте канал и права', error)


def register_release_updates(tree, service, guild_id):
    @tree.command(name='обновления-канал', description='Выбрать канал и опубликовать недостающие версии бота',
                  guild=discord.Object(id=guild_id), extras={'access': 'admin'})
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    @discord.app_commands.rename(channel='канал')
    async def settings(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, 'admin')
        result = await service.configure(interaction, channel)
        warning = (' Неподтверждённые ранее отправки: ' + ', '.join(result['blocked']) + '. Проверьте канал и журнал.'
                   if result['blocked'] else '')
        await interaction.edit_original_response(
            content=f'Канал обновлений: {channel.mention}. Опубликовано: {len(result["published"])}. '
                    f'Уже было в канале: {len(result["existing"])}.{warning}',
            allowed_mentions=discord.AllowedMentions.none())
