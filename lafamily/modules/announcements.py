"""Независимые шаблоны и расписания анонсов за 15 минут до МП."""
import asyncio
import json
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

from lafamily.async_database import database_call
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.modules.events.time import MOSCOW, timestamp, display_date
from lafamily.permissions import require_access_async

DAYS = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')


def parse_clock(value):
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise UserError('Время: ЧЧ:ММ по Москве, например 20:00.')
    return value


def parse_days(value):
    parts = value.strip().lower()
    if parts == 'ежедневно':
        return list(range(7))
    selected = set(re.split(r'[,\s]+', parts))
    if not selected or not selected.issubset(DAYS):
        raise UserError('Дни: пн,вт,ср,чт,пт,сб,вс или ежедневно.')
    return [i for i, day in enumerate(DAYS) if day in selected]


def parse_start(value, now=None):
    try:
        start = datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=MOSCOW).astimezone(UTC)
    except ValueError as error:
        raise UserError('Дата: ГГГГ-ММ-ДД ЧЧ:ММ по Москве.') from error
    if start < (now or datetime.now(UTC)) + timedelta(minutes=15):
        raise UserError('До начала должно оставаться не менее 15 минут.')
    return timestamp(start)


def due_occurrences(schedule, now):
    if schedule['kind'] == 'once':
        starts = [datetime.fromisoformat(schedule['starts_at'])]
    else:
        local = now.astimezone(MOSCOW)
        hour, minute = map(int, schedule['clock'].split(':'))
        days = json.loads(schedule['days'])
        starts = []
        # Завтрашний день нужен для анонсов перед полуночью: например, МП в 00:05.
        for offset in (0, 1):
            day = local.date() + timedelta(days=offset)
            if day.weekday() in days:
                starts.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=MOSCOW).astimezone(UTC))
    return [start for start in starts if start - timedelta(minutes=15) <= now < start]


def text_channel(channel, guild_id, history=False):
    if not isinstance(channel, discord.TextChannel) or str(channel.guild.id) != str(guild_id):
        raise UserError('Нужен текстовый канал настроенного сервера.')
    permissions = channel.permissions_for(channel.guild.me)
    if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
        raise UserError('Боту нужны просмотр канала, отправка сообщений и вставка ссылок.')
    if history and not permissions.read_message_history:
        raise UserError('В хранилище картинок боту нужно читать историю сообщений.')
    return permissions


def check_role(channel, role):
    if role is None or role.is_default() or role.guild.id != channel.guild.id:
        raise UserError('Выберите конкретную роль этого сервера, кроме @everyone.')
    if not role.mentionable and not channel.permissions_for(channel.guild.me).mention_everyone:
        raise UserError('Сделайте роль упоминаемой или дайте боту право упоминать все роли в канале анонсов.')


class AnnouncementService:
    def __init__(self, client, store, guild_id):
        self.client, self.store, self.guild_id = client, store, guild_id
        self.lock = asyncio.Lock()

    def start(self):
        if self.client.is_ready() and not self.poll.is_running():
            self.poll.start()

    async def close(self):
        task = self.poll.get_task()
        self.poll.cancel()
        if task:
            with suppress(asyncio.CancelledError):
                await task

    @tasks.loop(seconds=30)
    async def poll(self):
        try:
            await self.tick()
        except Exception as error:
            log_error('Ошибка планировщика анонсов', error)

    async def upload_image(self, attachment):
        if attachment.content_type not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif') or attachment.size > 8*1024*1024:
            raise UserError('Картинка: PNG, JPEG, WEBP или GIF, до 8 МБ.')
        setting = await database_call(self.store, 'announcement_storage', str(self.guild_id))
        if not setting:
            raise UserError('Сначала задайте служебный канал: /анонс настройки хранилище.')
        channel = await self.client.fetch_channel(int(setting['channel_id']))
        permissions = text_channel(channel, self.guild_id, history=True)
        if not permissions.attach_files:
            raise UserError('В хранилище картинок боту нужно право прикреплять файлы.')
        file = await attachment.to_file()
        try:
            message = await channel.send('Картинка шаблона анонса. Не удаляйте это сообщение.', file=file,
                                         allowed_mentions=discord.AllowedMentions.none())
        finally:
            file.close()
        return json.dumps({'channel': str(channel.id), 'message': str(message.id), 'attachment': str(message.attachments[0].id)})

    async def image_url(self, reference):
        ref = json.loads(reference)
        channel = await self.client.fetch_channel(int(ref['channel']))
        if str(channel.guild.id) != str(self.guild_id):
            raise UserError('Хранилище картинки принадлежит другому серверу.')
        message = await channel.fetch_message(int(ref['message']))
        attachment = next((a for a in message.attachments if str(a.id) == ref['attachment']), None)
        if attachment is None:
            raise UserError('Картинка шаблона удалена. Загрузите её повторно через /анонс шаблон.')
        return attachment.url

    async def tick(self, now=None):
        supplied_now = now
        now = now or datetime.now(UTC)
        if not self.client.is_ready():
            return
        async with self.lock:
            rows = await database_call(self.store, 'announcement_schedules', str(self.guild_id), True)
            for row in rows:
                for start in due_occurrences(row, now):
                    try:
                        if await database_call(self.store, 'announcement_attempt', row['id'], timestamp(start)):
                            continue
                        channel = await self.client.fetch_channel(int(row['channel_id']))
                        text_channel(channel, self.guild_id)
                        role = channel.guild.get_role(int(row['role_id']))
                        check_role(channel, role)
                        embed = discord.Embed(title=f'Скоро МП: {row["name"]}', description=row['body'], colour=0xE5B84B)
                        unix = int(start.timestamp())
                        embed.add_field(name='Начало', value=f'<t:{unix}:F> · <t:{unix}:R>')
                        image_warning = ''
                        if row['image_ref']:
                            try:
                                embed.set_image(url=await self.image_url(row['image_ref']))
                            except Exception as error:
                                # Полезнее вовремя отправить текст, чем потерять весь анонс из-за картинки.
                                image_warning = ' Картинку загрузить не удалось.'
                                log_error('Недоступна картинка анонса; будет отправлен текст', error)
                        if image_warning:
                            embed.set_footer(text=image_warning.strip())
                        if (datetime.now(UTC) if supplied_now is None else supplied_now) >= start:
                            continue
                        claimed = await database_call(self.store, 'claim_announcement', row['id'], timestamp(start))
                        if not claimed:
                            continue
                        try:
                            message = await channel.send(content=f'<@&{role.id}>', embed=embed,
                                allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role], replied_user=False))
                            await database_call(self.store, 'finish_announcement', row['id'], timestamp(start), 'sent', str(message.id))
                        except Exception:
                            await database_call(self.store, 'finish_announcement', row['id'], timestamp(start), 'uncertain', None)
                            raise
                    except Exception as error:
                        log_error(f'Не удалось отправить анонс расписания #{row["id"]}; неопределённая отправка не повторяется', error)


class AnnouncementCommands(app_commands.Group):
    def __init__(self, service):
        super().__init__(name='анонс', description='Уведомления о МП за 15 минут', guild_only=True)
        self.service = service

    async def begin(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, self.service.store, 'admin')

    async def template_id(self, interaction, value):
        if not value.isdigit():
            raise UserError('Выберите шаблон из подсказок.')
        row = await database_call(self.service.store, 'announcement_template', str(interaction.guild_id), int(value))
        if row is None:
            raise UserError('Шаблон не найден.')
        return row

    async def choices(self, interaction, current):
        try:
            await asyncio.wait_for(require_access_async(interaction, self.service.store, 'admin'), timeout=1)
            rows = await asyncio.wait_for(database_call(self.service.store, 'announcement_templates', str(interaction.guild_id)), timeout=1)
        except (UserError, TimeoutError):
            return []
        return [app_commands.Choice(name=r['name'], value=str(r['id'])) for r in rows if current.casefold() in r['name'].casefold()][:25]

    @app_commands.command(name='настройки', description='Задать хранилище картинок анонсов', extras={'access': 'admin'})
    @app_commands.rename(channel='хранилище')
    async def settings(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.begin(interaction)
        permissions = text_channel(channel, self.service.guild_id, history=True)
        if not permissions.attach_files:
            raise UserError('Боту нужно право прикреплять файлы в хранилище.')
        await database_call(self.service.store, 'configure_announcement_storage', str(interaction.guild_id), str(channel.id))
        await interaction.edit_original_response(content='Хранилище картинок сохранено. Не удаляйте сообщения с картинками шаблонов.')

    @app_commands.command(name='шаблон', description='Создать или обновить шаблон анонса', extras={'access': 'admin'})
    @app_commands.rename(name='название', channel='канал', role='роль', body='текст', image='картинка', clear_image='убрать-картинку')
    async def template(self, interaction: discord.Interaction, name: app_commands.Range[str,1,80], channel: discord.TextChannel,
                       role: discord.Role, body: app_commands.Range[str,1,2000], image: discord.Attachment | None = None, clear_image: bool = False):
        await self.begin(interaction)
        text_channel(channel, self.service.guild_id); check_role(channel, role)
        if not name.strip() or not body.strip():
            raise UserError('Название и текст не должны быть пустыми.')
        if image and clear_image:
            raise UserError('Нельзя одновременно загрузить и удалить картинку.')
        reference = await self.service.upload_image(image) if image else None
        async with self.service.lock:
            number = await database_call(self.service.store, 'save_announcement_template', str(interaction.guild_id), name.strip(),
                                        str(channel.id), str(role.id), body.strip(), reference, clear_image)
        await interaction.edit_original_response(content=f'Шаблон #{number} сохранён. Совпадение названия обновляет его; без новой картинки прежняя сохраняется. Теперь задайте /анонс разово или /анонс расписание.')

    @app_commands.command(name='разово', description='Назначить разовое МП по Москве', extras={'access': 'admin'})
    @app_commands.rename(template='шаблон', start='начало')
    @app_commands.describe(start='ГГГГ-ММ-ДД ЧЧ:ММ по Москве; минимум через 15 минут')
    async def once(self, interaction: discord.Interaction, template: str, start: str):
        await self.begin(interaction)
        row = await self.template_id(interaction, template)
        begins = parse_start(start)
        async with self.service.lock:
            number = await database_call(self.service.store, 'create_announcement_schedule', str(interaction.guild_id), row['id'], 'once', begins, None, None)
        await interaction.edit_original_response(content=f'Расписание #{number}: {row["name"]}, начало {display_date(begins)} (Москва). Анонс за 15 минут.')

    @once.autocomplete('template')
    async def once_choices(self, interaction, current):
        return await self.choices(interaction, current)

    @app_commands.command(name='расписание', description='Назначить еженедельное МП по Москве', extras={'access': 'admin'})
    @app_commands.rename(template='шаблон', days='дни', clock='время')
    @app_commands.describe(days='пн,ср,пт или ежедневно', clock='Время начала МП ЧЧ:ММ по Москве')
    async def weekly(self, interaction: discord.Interaction, template: str, days: str, clock: str):
        await self.begin(interaction)
        row = await self.template_id(interaction, template)
        selected = parse_days(days); clock = parse_clock(clock)
        async with self.service.lock:
            number = await database_call(self.service.store, 'create_announcement_schedule', str(interaction.guild_id), row['id'], 'weekly', None, json.dumps(selected), clock)
        await interaction.edit_original_response(content=f'Расписание #{number}: {row["name"]}, {", ".join(DAYS[i] for i in selected)} в {clock} по Москве. Анонс за 15 минут.')

    @weekly.autocomplete('template')
    async def weekly_choices(self, interaction, current):
        return await self.choices(interaction, current)

    @app_commands.command(name='список', description='Показать шаблоны и расписания анонсов', extras={'access': 'admin'})
    async def listing(self, interaction: discord.Interaction):
        from io import BytesIO
        await self.begin(interaction)
        templates = await database_call(self.service.store, 'announcement_templates', str(interaction.guild_id))
        schedules = await database_call(self.service.store, 'announcement_schedules', str(interaction.guild_id), False)
        text = 'Шаблоны:\n' + '\n'.join(f'#{r["id"]} {r["name"]} → канал {r["channel_id"]}, роль {r["role_id"]}' for r in templates)
        text += '\n\nРасписания (Москва):\n' + '\n'.join(f'#{r["id"]} {r["name"]}: ' +
            (display_date(r['starts_at']) if r['kind']=='once' else f'{",".join(DAYS[i] for i in json.loads(r["days"]))} {r["clock"]}') +
            (' — включено' if r['enabled'] else ' — отключено') for r in schedules)
        if len(text) > 1900:
            await interaction.edit_original_response(content='Полный список — в файле.', attachments=[discord.File(BytesIO(text.encode('utf-8')), filename='anonsy.txt')])
        else:
            await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='отключить', description='Отключить расписание по номеру', extras={'access': 'admin'})
    @app_commands.rename(number='номер')
    async def disable(self, interaction: discord.Interaction, number: app_commands.Range[int,1]):
        await self.begin(interaction)
        async with self.service.lock:
            await database_call(self.service.store, 'disable_announcement_schedule', str(interaction.guild_id), number)
        await interaction.edit_original_response(content=f'Расписание #{number} отключено. Чтобы изменить время, создайте новое расписание, отключив прежнее.')
