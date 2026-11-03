"""Личная форма с отдельными наборами ролей и явным сохранением."""
import asyncio
import time
import discord
from lafamily.access_roles import role_ids
from lafamily.async_database import database_call
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from lafamily.responses import send_error

class SettingsView(discord.ui.View):
    def __init__(self, service, interaction, channel, settings):
        super().__init__(timeout=840)
        self.service, self.original, self.channel = service, interaction, channel
        self.owner_id, self.guild_id = interaction.user.id, interaction.guild_id
        self.organizers = [r for r in role_ids(settings, 'organizer') if interaction.guild.get_role(int(r))]
        self.managers = [r for r in role_ids(settings, 'manager') if interaction.guild.get_role(int(r))]
        self.finished, self.needs_refresh = False, False
        self.lock = asyncio.Lock()
        self.expires_at = time.monotonic() + 840
        self.expiry_handle = asyncio.get_running_loop().call_later(840, lambda: asyncio.create_task(self.on_timeout()))

    def payload(self, organizers=None, managers=None):
        organizers = self.organizers if organizers is None else organizers
        managers = self.managers if managers is None else managers
        for menu, roles in ((self.organizer_menu, organizers), (self.manager_menu, managers)):
            menu.default_values = [self.original.guild.get_role(int(r)) for r in roles if self.original.guild.get_role(int(r))]
        self.save.disabled = not organizers or not managers
        def describe(roles):
            return ', '.join(f'<@&{r}>' for r in roles) or 'не выбраны'
        return {'content': f'**Настройки LaFamily Bot**\nКанал отчётов: <#{self.channel.id}>\n\n'
                f'**Организаторы:** {describe(organizers)}\n**Руководство:** {describe(managers)}\n\n'
                'Выберите от 1 до 25 ролей в каждом меню. Достаточно любой одной роли. Руководство также может подавать отчёты.\n'
                '«Сохранить» применяет выбранные наборы вместо прежних. «Отмена» оставляет прежние настройки. Форма действует 14 минут.',
                'view': self, 'allowed_mentions': discord.AllowedMentions.none()}

    def finish(self):
        self.finished = True
        self.expiry_handle.cancel()
        self.stop()
        for item in self.children:
            item.disabled = True

    async def on_timeout(self):
        async with self.lock:
            if not self.finished:
                self.finish()
                try:
                    await self.original.edit_original_response(content='Срок настройки истёк. Изменения не сохранены. Выполните /бот настройки заново.', view=None)
                except Exception as error:
                    log_error('Не удалось закрыть форму настроек', error)

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)

    async def handle(self, interaction, action, selected=None):
        if self.lock.locked():
            raise UserError('Настройки обрабатываются. Дождитесь результата.')
        async with self.lock:
            await interaction.response.defer()
            if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
                raise UserError('Эту форму может менять только открывший её администратор.')
            if self.finished or time.monotonic() >= self.expires_at:
                raise UserError('Форма уже закрыта. Выполните /бот настройки заново.')
            await require_access_async(interaction, self.service.store, 'admin')
            self.original = interaction
            if action == 'cancel':
                self.finish()
                await interaction.edit_original_response(content='Настройка отменена. Изменения не сохранены.', view=None)
                return
            if action in ('organizer', 'manager'):
                if not 1 <= len(selected) <= 25 or any(r.is_default() for r in selected):
                    raise UserError('Выберите от 1 до 25 отдельных ролей. @everyone использовать нельзя.')
                organizers = [str(r.id) for r in selected] if action == 'organizer' else self.organizers
                managers = [str(r.id) for r in selected] if action == 'manager' else self.managers
                try:
                    await interaction.edit_original_response(**self.payload(organizers, managers))
                except BaseException:
                    self.needs_refresh = True
                    self.payload()
                    raise
                self.organizers, self.managers, self.needs_refresh = organizers, managers, False
                return
            if self.needs_refresh:
                await interaction.edit_original_response(**self.payload())
                self.needs_refresh = False
                return
            if not self.organizers or not self.managers:
                raise UserError('Выберите хотя бы одну роль в каждом списке.')
            # Роли могли удалить после открытия формы: перед записью проверяем актуальный список.
            existing = {str(r.id) for r in await interaction.guild.fetch_roles() if not r.is_default()}
            if not set(self.organizers + self.managers).issubset(existing):
                raise UserError('Одна из ролей удалена. Выберите актуальные роли или откройте /бот настройки заново.')
            await self.service.channel(interaction.guild, str(self.channel.id))
            await database_call(self.service.store, 'configure', str(self.guild_id), str(self.owner_id), str(self.channel.id), self.organizers, self.managers)
            self.finish()
            await interaction.edit_original_response(content='Настройки сохранены. Проверить назначенные роли можно через /инфо общее.', view=None)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='Организаторы: выберите все нужные роли', min_values=1, max_values=25, row=0)
    async def organizer_menu(self, interaction: discord.Interaction, menu: discord.ui.RoleSelect):
        await self.handle(interaction, 'organizer', menu.values)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='Руководство: выберите все нужные роли', min_values=1, max_values=25, row=1)
    async def manager_menu(self, interaction: discord.Interaction, menu: discord.ui.RoleSelect):
        await self.handle(interaction, 'manager', menu.values)

    @discord.ui.button(label='Сохранить', style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle(interaction, 'save')

    @discord.ui.button(label='Отмена', style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle(interaction, 'cancel')
