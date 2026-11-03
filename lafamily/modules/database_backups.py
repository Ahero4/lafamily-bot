"""Шестичасовые архивы в Discord и подтверждаемое восстановление с переподключением."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import discord
from discord import app_commands
from discord.ext import tasks

from lafamily.async_database import DatabaseWorker, database_call
from lafamily.command_groups import command_group
from lafamily.database_archives import MAX_ARCHIVE_BYTES, inspect_archive, pack_database
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from lafamily.release import VERSION
from lafamily.modules.events.time import display_date
from lafamily.responses import send_error


def backup_caption(manifest, archive, title='Резервная копия БД'):
    row = manifest['summary']
    kinds = {'scheduled':'по расписанию','manual':'ручная','before-restore':'перед восстановлением'}
    return (f'**LaFamily • {title}**\n'
        f"Дата: **{display_date(manifest['created_at'])} (Москва)** · UTC: {manifest['created_at']}\n"
        f"Тип: {kinds.get(manifest['reason'],manifest['reason'])} · версия бота **{manifest['version']}**.\n"
        f"Размер БД: **{row['database_bytes']:,} байт** · ZIP: **{Path(archive).stat().st_size:,} байт**.\n"
        f"Таблиц: **{len(row['tables'])}** · записей во всех таблицах: **{row['records']}**.\n"
        f"Отчётов: **{row['reports']}**, активных **{row['active']}**, отменённых **{row['cancelled']}**.\n"
        f"Участников в истории: **{row['participants']}**, с активными начислениями **{row['active_participants']}**.\n"
        f"Учитываемых баллов: **{row['points']}** · подписчиков скупщика: **{row['subscribers']}**.\n"
        f"SHA-256: `{manifest['sha256']}`\n"
        'Восстановление: `/бот бэкапы восстановить сообщение:ID`. '
        'Отменённые отчёты в баллы не входят; это полная копия данных и настроек.')


@dataclass
class RestoreRequest:
    directory: TemporaryDirectory
    database: Path
    guild_id: str
    actor_id: str
    channel_id: str
    message_id: str
    manifest: dict
    summary: dict
    safety_database: Path | None = None
    safety_archive: Path | None = None
    safety_message_id: str | None = None

    def cleanup(self):
        self.directory.cleanup()


class DatabaseBackups:
    def __init__(self, client, store, guild_id):
        self.client, self.store, self.guild_id = client, store, guild_id
        self.lock = asyncio.Lock()
        self.restoring = False

    def start(self):
        if self.client.is_ready() and not self.poll.is_running():
            self.poll.start()

    async def close(self):
        task = self.poll.get_task()
        self.poll.cancel()
        if task:
            with suppress(asyncio.CancelledError):
                await task

    @tasks.loop(seconds=60)
    async def poll(self):
        try:
            await self.create()
        except Exception as error:
            log_error('Не удалось отправить автоматический бэкап БД', error)

    def check_channel(self, channel, guild):
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild_id:
            raise UserError('Для бэкапов выберите текстовый канал этого сервера.')
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.attach_files
                and permissions.read_message_history):
            raise UserError('Боту нужны просмотр, отправка, прикрепление файлов и чтение истории в канале бэкапов.')

    async def archive(self, database, directory, kind):
        tag = datetime.now(UTC).strftime('%Y-%m-%dT%H-%M-%S-%fZ')
        target = Path(directory)/f'lafamily-backup-{tag}-{kind}.zip'
        manifest = await asyncio.to_thread(pack_database, database, target, self.guild_id,
                                           self.client.user.id, VERSION, kind)
        return target, manifest

    async def upload(self, channel, archive, content):
        limit = min(MAX_ARCHIVE_BYTES, channel.guild.filesize_limit)
        if archive.stat().st_size > limit:
            raise UserError('Архив превышает лимит вложения Discord. Восстановление не начато; проверьте журнал.')
        file = discord.File(archive)
        try:
            return await channel.send(content=content,file=file,allowed_mentions=discord.AllowedMentions.none())
        finally:
            file.close()

    async def create(self, *, force=False):
        async with self.lock:
            if self.restoring:
                raise UserError('Выполняется восстановление базы.')
            setting = await database_call(self.store,'claim_database_backup',str(self.guild_id),force=force)
            if not setting:
                if force:
                    raise UserError('Сначала выберите канал через /бот бэкапы настройки.')
                return None
            sending = False
            try:
                channel = await self.client.fetch_channel(int(setting['channel_id']))
                self.check_channel(channel,channel.guild)
                prices = await database_call(self.store,'price_settings',str(self.guild_id))
                if prices and setting['channel_id'] == prices['channel_id']:
                    raise UserError('Канал бэкапов должен отличаться от очищаемого канала цен.')
                with TemporaryDirectory(prefix='lafamily-backup-') as directory:
                    database = Path(directory)/'snapshot.sqlite'
                    await database_call(self.store,'export_database',database)
                    archive, manifest = await self.archive(database,directory,'manual' if force else 'scheduled')
                    if archive.stat().st_size > min(MAX_ARCHIVE_BYTES,channel.guild.filesize_limit):
                        raise UserError('Архив превышает лимит вложения Discord; требуется внешний способ хранения.')
                    sending = True
                    message = await self.upload(channel,archive,backup_caption(manifest,archive))
                await database_call(self.store,'finish_database_backup',str(self.guild_id),setting['last_attempt'],
                                    'sent',str(message.id))
                return message
            except Exception as error:
                await database_call(self.store,'finish_database_backup',str(self.guild_id),setting['last_attempt'],
                                    'uncertain' if sending else 'failed')
                log_error('Не подтверждена отправка архива БД; следующая автоматическая попытка через 6 часов',error)
                if not isinstance(error,UserError):
                    raise UserError('Отправка бэкапа не подтверждена. Проверьте канал и журнал; следующая автоматическая попытка через 6 часов.') from error
                raise

    async def configure(self, interaction, channel):
        async with self.lock:
            if self.restoring:
                raise UserError('Выполняется восстановление базы.')
            if channel is not None:
                self.check_channel(channel,interaction.guild)
                prices = await database_call(self.store,'price_settings',str(self.guild_id))
                if prices and str(channel.id) == prices['channel_id']:
                    raise UserError('Выберите отдельный канал бэкапов, отличный от канала цен.')
            await database_call(self.store,'configure_database_backups',str(self.guild_id),str(interaction.user.id),
                                str(channel.id) if channel else None)
        if channel is None:
            await interaction.edit_original_response(content='Автоматические бэкапы отключены. Ранее отправленные архивы сохранены.')
            return
        message = await self.create(force=True)
        await interaction.edit_original_response(content=f'Канал бэкапов: <#{channel.id}>. Первая копия: {message.jump_url}. '
            'Следующая автоматическая попытка через 6 часов. Архив содержит данные участников; ограничьте доступ к каналу.',
            allowed_mentions=discord.AllowedMentions.none())

    async def prepare(self, interaction, message_id):
        if not isinstance(self.store,DatabaseWorker) or str(self.store.path) == ':memory:':
            raise UserError('Восстановление доступно только для рабочей файловой базы.')
        if not message_id.isascii() or not message_id.isdigit() or not 17 <= len(message_id) <= 20:
            raise UserError('Укажите ID сообщения с бэкапом: 17–20 цифр.')
        setting = await database_call(self.store,'database_backup_settings',str(self.guild_id))
        if not setting or not setting['channel_id']:
            raise UserError('Сначала укажите канал через /бот бэкапы настройки.')
        channel = await self.client.fetch_channel(int(setting['channel_id']))
        self.check_channel(channel,interaction.guild)
        message = await channel.fetch_message(int(message_id))
        if message.author.id != self.client.user.id:
            raise UserError('Для восстановления выберите архив, отправленный этим ботом.')
        files = [a for a in message.attachments if a.filename.startswith('lafamily-backup-') and a.filename.endswith('.zip')]
        if len(files) != 1 or not 0 < files[0].size <= min(MAX_ARCHIVE_BYTES,channel.guild.filesize_limit):
            raise UserError('В сообщении должен быть один архив бота допустимого размера.')
        directory = TemporaryDirectory(prefix='lafamily-restore-')
        try:
            archive = Path(directory.name)/'download.zip'
            data = await files[0].read()
            if len(data) != files[0].size or len(data) > MAX_ARCHIVE_BYTES:
                raise UserError('Размер скачанного архива не совпал.')
            await asyncio.to_thread(archive.write_bytes,data)
            database = Path(directory.name)/'restored.sqlite'
            manifest,summary = await asyncio.to_thread(inspect_archive,archive,database,self.guild_id,
                                                       self.client.user.id,VERSION)
            return RestoreRequest(directory,database,str(self.guild_id),str(interaction.user.id),
                                  setting['channel_id'],message_id,manifest,summary)
        except BaseException:
            directory.cleanup()
            raise

    async def restore(self, interaction, request):
        async with self.lock:
            await require_access_async(interaction,self.store,'admin')
            setting = await database_call(self.store,'database_backup_settings',str(self.guild_id))
            if self.restoring or not setting or setting['channel_id'] != request.channel_id:
                raise UserError('Канал бэкапов изменился или уже идёт восстановление. Откройте команду заново.')
            channel = await self.client.fetch_channel(int(request.channel_id))
            self.check_channel(channel,interaction.guild)
            self.restoring = True
            frozen = False
            try:
                request.safety_database = Path(request.directory.name)/'before-restore.sqlite'
                await self.store.freeze_and_export(request.safety_database)
                frozen = True
                safety_dir = Path(self.store.path).resolve().parent/'backups'
                await asyncio.to_thread(safety_dir.mkdir,parents=True,exist_ok=True)
                request.safety_archive,safety_manifest = await self.archive(request.safety_database,safety_dir,'before-restore')
                saved = await self.upload(channel,request.safety_archive,
                    backup_caption(safety_manifest,request.safety_archive,'копия перед восстановлением'))
                request.safety_message_id = str(saved.id)
                await interaction.edit_original_response(content='Проверки завершены. Текущая БД сохранена: '
                    f'{saved.jump_url}. Восстанавливаю выбранную копию и переподключаю бота.',view=None,
                    allowed_mentions=discord.AllowedMentions.none())
                self.client.restore_request = request
            except BaseException as error:
                if frozen:
                    self.store.resume()
                self.restoring = False
                if isinstance(error,Exception) and not isinstance(error,UserError):
                    log_error('Восстановление не начато; исходная БД продолжает работать',error)
                    raise UserError('Восстановление не начато. Исходная база продолжает работать; проверьте канал бэкапов и журнал.') from error
                raise
            # Новый клиент создаётся main.py только после закрытия этого клиента и worker.
            await self.client.close()


class RestoreView(discord.ui.View):
    def __init__(self, service, original, request):
        super().__init__(timeout=180)
        self.service,self.original,self.request = service,original,request
        self.owner_id,self.guild_id = original.user.id,original.guild_id
        self.finished = False
        self.lock = asyncio.Lock()

    def check(self, interaction):
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            raise UserError('Подтверждение принадлежит другому администратору.')
        if self.finished or self.is_finished():
            raise UserError('Подтверждение закрыто. Откройте восстановление заново.')

    async def on_error(self, interaction, error, item):
        await send_error(interaction,error)

    @discord.ui.button(label='Восстановить и переподключить',style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        async with self.lock:
            self.check(interaction)
            await interaction.response.defer()
            await require_access_async(interaction,self.service.store,'admin')
            self.finished = True
            self.stop()
            try:
                await self.service.restore(interaction,self.request)
            except BaseException:
                if self.service.client.restore_request is not self.request:
                    self.request.cleanup()
                raise

    @discord.ui.button(label='Отмена',style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        async with self.lock:
            self.check(interaction)
            self.finished = True
            self.stop()
            self.request.cleanup()
            await interaction.response.edit_message(content='Восстановление отменено. База не изменена.',view=None)

    async def on_timeout(self):
        async with self.lock:
            if self.finished:
                return
            self.finished = True
            self.stop()
            self.request.cleanup()
            with suppress(discord.HTTPException):
                await self.original.edit_original_response(content='Время подтверждения истекло. База не изменена.',view=None)


def register_database_backups(tree, service, guild_id):
    root = command_group(tree,guild_id,'бот','Настройки, состояние и служебные отчёты бота')
    group = app_commands.Group(name='бэкапы',description='Автоматические архивы БД и восстановление')
    root.add_command(group)

    @group.command(name='настройки',description='Выбрать канал шестичасовых бэкапов или отключить их',extras={'access':'admin'})
    @app_commands.rename(channel='канал')
    @app_commands.describe(channel='Закрытый канал архивов; без канала — отключить автоматическую отправку')
    async def settings(interaction:discord.Interaction,channel:discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True,thinking=True)
        await require_access_async(interaction,service.store,'admin')
        await service.configure(interaction,channel)

    @group.command(name='создать',description='Сразу создать архив БД и отправить в выбранный канал',extras={'access':'admin'})
    async def create(interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True,thinking=True)
        await require_access_async(interaction,service.store,'admin')
        message = await service.create(force=True)
        await interaction.edit_original_response(content=f'Бэкап отправлен: {message.jump_url}.')

    @group.command(name='восстановить',description='Проверить архив из сообщения и подтвердить восстановление БД',extras={'access':'admin'})
    @app_commands.rename(message_id='сообщение')
    @app_commands.describe(message_id='ID сообщения этого бота с архивом в выбранном канале бэкапов')
    async def restore(interaction:discord.Interaction,message_id:str):
        await interaction.response.defer(ephemeral=True,thinking=True)
        await require_access_async(interaction,service.store,'admin')
        request = await service.prepare(interaction,message_id)
        view = RestoreView(service,interaction,request)
        summary = request.summary
        try:
            await interaction.edit_original_response(content=(
                '**Восстановление всей базы — подтвердите действие**\n'
                f"Копия UTC: {request.manifest['created_at']} · версия {request.manifest['version']}.\n"
                f"Отчётов: {summary['reports']}, активных: {summary['active']}, отменённых: {summary['cancelled']}, "
                f"участников в истории: {summary['participants']}.\n"
                'Все данные и настройки откатятся к этой копии; изменения после неё будут заменены. '
                'Сначала бот отправит страховочный архив текущей БД, затем восстановит данные и сам переподключится. '
                'Отменённые в копии отчёты останутся отменёнными. Старые черновики и меню закроются.\n'
                'Подтверждение действует 3 минуты.'),view=view,allowed_mentions=discord.AllowedMentions.none())
        except BaseException:
            view.stop()
            request.cleanup()
            raise
