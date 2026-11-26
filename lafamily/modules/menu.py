"""Личное меню семьи и постоянная кнопка входа."""
import asyncio
from functools import partial

import discord
from aiohttp import ClientError
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.modules.events.time import display_date, week_range
from lafamily.modules.family_guide import FamilyGuide, guide_embed
from lafamily.modules.garden import show_garden
from lafamily.modules.insights import _history_entry, profile_card
from lafamily.modules.menu_cards import menu_card
from lafamily.modules.menu_forms import open_bank_form, open_barn_form, open_number_form, open_restore_form
from lafamily.modules.menu_help import (
    COMMANDS_PER_PAGE, PUBLIC_TOPICS, SERVICE_TOPICS, TOPICS, help_commands, help_topic_embed, topic_commands,
)
from lafamily.modules.prices.parser import LIMITS
from lafamily.permissions import check_access
from lafamily.responses import send_error

OPEN_ID = 'lafamily:menu:open:v1'
LAUNCHER_TEXT = '**Личное меню LaFamily**\nПрофиль, мероприятия, сад, скупщик, коровник и помощь в одном меню.'
HISTORY_PAGE_SIZE = 5

TITLES = {
    'home': 'LaFamily',
    'ranking': 'Рейтинг семьи',
    'prices': 'Скупщик',
    'barn': 'Коровник',
    'payouts': 'Премии и ведомости',
    'admin': 'Администрирование',
    'channels': 'Каналы и публикации',
    'diagnostics': 'Диагностика',
    'backups': 'Резервные копии',
    'reports_channel': 'Канал админских отчётов',
    'events_channel': 'Канал МП и управляющие роли',
    'release_channel': 'Канал обновлений',
    'launcher_channel': 'Закрепить карточку LaFamily',
    'guide_channel': 'Разместить памятку семьи',
    'backup_channel': 'Канал резервных копий',
}
CHANNEL_COMMANDS = {
    'reports_channel': 'бот отчёты',
    'events_channel': 'бот настройки',
    'release_channel': 'бот обновления',
    'backup_channel': 'бот бэкапы настройки',
}
PICKER_PARENTS = {
    'reports_channel': 'channels', 'events_channel': 'channels', 'release_channel': 'channels',
    'launcher_channel': 'channels', 'guide_channel': 'channels', 'backup_channel': 'backups',
}
ADMIN_SECTIONS = {'payouts', 'admin', 'channels', 'diagnostics', 'backups', *PICKER_PARENTS}


def check_server(interaction, client):
    if interaction.guild is None or interaction.guild_id != client.config.guild_id:
        raise UserError('Меню доступно только на сервере семьи.')
    if client.backups.restoring:
        raise UserError('Дождитесь завершения восстановления базы.')


class FamilyMenu:
    def __init__(self, client):
        self.client = client
        self.publish_lock = asyncio.Lock()
        self.guide = FamilyGuide(client)

    def check(self, interaction, *, owner=None, admin=False):
        check_server(interaction, self.client)
        if owner is not None and interaction.user.id != owner:
            raise UserError('Откройте собственное меню.')
        if admin:
            check_access(interaction, None, 'admin')

    async def invoke(self, interaction, path: str, **kwargs):
        self.check(interaction)
        guild = discord.Object(id=interaction.guild_id)
        first, *parts = path.split()
        command = self.client.tree.get_command(first, guild=guild)
        for part in parts:
            command = command.get_command(part) if isinstance(command, app_commands.Group) else None
        if not isinstance(command, app_commands.Command) or command.extras.get('hidden'):
            raise UserError('Это действие сейчас недоступно.')
        if command.extras.get('access') == 'admin':
            self.check(interaction, admin=True)
        # Обработчики организатора и руководства сами подтверждают Interaction перед проверкой ролей в SQLite.
        if command.binding is not None:
            return await command.callback(command.binding, interaction, **kwargs)
        return await command.callback(interaction, **kwargs)

    async def db(self, interaction, method, *args):
        return await database_call(self.client.store, method, str(interaction.guild_id), *args)

    def home_view(self, interaction):
        view = MenuView(self, interaction)
        view.button('Профиль', self.show_profile)
        view.button('Мероприятия', self.show_events)
        view.button('Сад', self.show_garden)
        view.section('prices', row=1)
        view.section('barn', row=1)
        view.button('Помощь', self.show_help, row=1)
        if interaction.permissions.administrator:
            view.section('admin', row=2)
        return view

    async def open(self, interaction):
        self.check(interaction)
        with menu_card('home') as (embed, files):
            await interaction.response.send_message(
                content='**LaFamily**', embed=embed, files=files, view=self.home_view(interaction),
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )

    async def show_home(self, interaction):
        self.check(interaction)
        with menu_card('home') as (embed, attachments):
            await interaction.response.edit_message(
                content='**LaFamily**', embed=embed, attachments=attachments, view=self.home_view(interaction),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def show_garden(self, interaction):
        self.check(interaction)
        await show_garden(self.client.garden, interaction, back=self.show_home)

    async def show_events(self, interaction):
        from lafamily.modules.event_menu import open_event_menu

        self.check(interaction)
        await open_event_menu(self, interaction)

    async def show_profile(self, interaction):
        self.check(interaction)
        await interaction.response.defer()
        user_id = str(interaction.user.id)
        data = await self.db(interaction, 'participant_profile', user_id, week_range())
        subscriptions = await self.db(interaction, 'price_subscriptions', user_id)
        view = MenuView(self, interaction)
        view.button('История баллов', self.show_history)
        view.section('ranking')
        if interaction.permissions.administrator:
            view.section('payouts')
        view.button('Назад', self.show_home, row=1)
        await interaction.edit_original_response(
            content=None, embed=profile_card(user_id, data, subscriptions), attachments=[], view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def show_history(self, interaction, page: int = 1):
        self.check(interaction)
        if not 1 <= page <= 10_000:
            raise UserError('История доступна на страницах с 1 по 10000.')
        await interaction.response.defer()
        rows = await self.db(interaction, 'points_history', str(interaction.user.id),
                             (page - 1) * HISTORY_PAGE_SIZE, HISTORY_PAGE_SIZE + 1)
        entries = rows[:HISTORY_PAGE_SIZE]
        view = MenuView(self, interaction)
        if entries:
            options = [
                discord.SelectOption(label=f"#{row['id']} · {' '.join(row['type_name'].split())[:35]}",
                                     description=display_date(row['occurred_at']), value=str(row['id']))
                for row in entries
            ]

            async def open_report(current, value):
                from lafamily.modules.event_menu import open_saved_report

                if value not in {option.value for option in options}:
                    raise UserError('Выберите отчёт на текущей странице.')
                await open_saved_report(self, current, int(value), back=partial(self.show_history, page=page))

            view.select(discord.ui.Select(placeholder='Открыть отчёт', options=options), open_report)
        view.button('←', partial(self.show_history, page=page - 1), row=1, disabled=page == 1)
        view.button('→', partial(self.show_history, page=page + 1), row=1, disabled=len(rows) <= HISTORY_PAGE_SIZE or page == 10_000)
        view.button('Назад', self.show_profile, row=1)
        content = '\n\n'.join(_history_entry(row) for row in entries) or 'Записей нет.'
        await interaction.edit_original_response(content=f'**История баллов** · страница {page}\n\n{content}',
                                                 embed=None, view=view, allowed_mentions=discord.AllowedMentions.none())

    async def available_help_commands(self, interaction):
        self.check(interaction)
        await interaction.response.defer()
        settings = None if interaction.permissions.administrator else await self.db(interaction, 'settings')
        self.check(interaction)
        return help_commands(interaction, self.client.tree, settings)

    async def show_help(self, interaction):
        commands = await self.available_help_commands(interaction)
        await self.render_help(interaction, commands)

    async def render_help(self, interaction, commands):
        view = MenuView(self, interaction)
        for index, key in enumerate(PUBLIC_TOPICS):
            view.button(TOPICS[key].title, partial(self.show_help_topic, key=key), row=index // 3)
        if any(topic_commands(TOPICS[key], commands) for key in SERVICE_TOPICS):
            view.button('Служебная помощь', self.show_service_help, row=2)
        view.command('Каталог команд', 'команды', row=2)
        view.button('Назад', self.show_home, row=2)
        with menu_card('help', guide_embed()) as (embed, attachments):
            await interaction.edit_original_response(
                content=None, embed=embed, attachments=attachments, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def show_service_help(self, interaction, *, returning=False):
        commands = await self.available_help_commands(interaction)
        topics = [key for key in SERVICE_TOPICS if topic_commands(TOPICS[key], commands)]
        if not topics:
            if returning:
                await self.render_help(interaction, commands)
                return
            raise UserError('Служебная помощь доступна руководству и администраторам.')
        view = MenuView(self, interaction)
        for index, key in enumerate(topics):
            view.button(TOPICS[key].title, partial(self.show_help_topic, key=key), row=index // 3)
        view.button('Назад', self.show_help, row=2)
        await interaction.edit_original_response(
            content='**Служебная помощь**', embed=None, attachments=[], view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def show_help_topic(self, interaction, *, key, page=0):
        topic = TOPICS[key]
        commands = topic_commands(topic, await self.available_help_commands(interaction))
        if key in SERVICE_TOPICS and not commands:
            raise UserError('Этот раздел справки недоступен для вашей роли.')
        last_page = max(0, (len(commands) - 1) // COMMANDS_PER_PAGE)
        page = max(0, min(page, last_page))
        view = MenuView(self, interaction)
        if last_page:
            turn_page = partial(self.show_help_topic, key=key)
            view.button('Предыдущая страница', partial(turn_page, page=page - 1), disabled=page == 0)
            view.button('Следующая страница', partial(turn_page, page=page + 1), disabled=page == last_page)
        back = partial(self.show_service_help, returning=True) if key in SERVICE_TOPICS else self.show_help
        view.button('Назад', back)
        await interaction.edit_original_response(
            content=None, embed=help_topic_embed(topic, commands, page), attachments=[], view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def show_section(self, interaction, *, name):
        admin = name in ADMIN_SECTIONS
        self.check(interaction, admin=admin)
        view = MenuView(self, interaction, admin=admin)
        back = self.show_home
        if name == 'ranking':
            view.command('Текущая неделя', 'баллы рейтинг', week=0)
            view.command('Прошлая неделя', 'баллы рейтинг', week=-1)
            back = self.show_profile
        elif name == 'payouts':
            view.button('Рассчитать премии', partial(open_bank_form, self, sheet=False))
            view.button('Создать ведомость', partial(open_bank_form, self, sheet=True))
            view.command('Список ведомостей', 'баллы ведомости список')
            view.button('Посмотреть ведомость',
                        partial(open_number_form, self, 'Посмотреть ведомость', 'баллы ведомости посмотреть'), row=1)
            view.button('Статус ведомости',
                        partial(open_number_form, self, 'Статус ведомости', 'баллы ведомости статус',
                                select_status=True), row=1)
            back = self.show_profile
        elif name == 'prices':
            view.command('Подписки', 'скупщик подписки')
            view.command('Проверить ЛС', 'скупщик проверить-лс')
            select = discord.ui.Select(placeholder='История цен — выберите товар', row=1,
                                       options=[discord.SelectOption(label=item) for item in LIMITS])

            async def history(current, item):
                await self.invoke(current, 'скупщик история', item=item)

            view.select(select, history)
        elif name == 'barn':
            view.button('Записать сбор', partial(open_barn_form, self))
        elif name == 'admin':
            view.section('channels')
            view.section('diagnostics')
            view.section('backups')
        elif name == 'channels':
            view.section('events_channel')
            view.section('reports_channel')
            view.section('release_channel')
            view.section('launcher_channel', row=1)
            view.section('guide_channel', row=1)
            view.command('Отключить админские отчёты', 'бот отчёты', row=1)
            back = partial(self.show_section, name='admin')
        elif name == 'diagnostics':
            view.command('Состояние бота', 'бот диагностика')
            view.command('Незавершённые операции', 'бот операции')
            view.command('Статистика ЛС', 'скупщик статистика')
            view.button('Восстановить карточку отчёта',
                        partial(open_number_form, self, 'Восстановить карточку отчёта', 'бот восстановить-операцию',
                                kind='report'), row=1)
            back = partial(self.show_section, name='admin')
        elif name == 'backups':
            view.section('backup_channel')
            view.command('Отключить', 'бот бэкапы настройки')
            view.command('Создать бэкап', 'бот бэкапы создать', row=1)
            view.button('Восстановить из бэкапа', partial(open_restore_form, self), row=1)
            back = partial(self.show_section, name='admin')
        elif name in PICKER_PARENTS:
            select = discord.ui.ChannelSelect(placeholder='Выберите текстовый канал',
                                              channel_types=[discord.ChannelType.text])

            async def choose(current, selected):
                if name == 'launcher_channel':
                    await self.publish(current, selected.id)
                elif name == 'guide_channel':
                    await self.publish_guide(current, selected.id)
                else:
                    channel = current.guild.get_channel(selected.id)
                    if not isinstance(channel, discord.TextChannel) or channel.guild.id != current.guild_id:
                        raise UserError('Канал недоступен. Выберите текстовый канал сервера семьи.')
                    await self.invoke(current, CHANNEL_COMMANDS[name], channel=channel)

            view.select(select, choose)
            back = partial(self.show_section, name=PICKER_PARENTS[name])
        view.button('Назад', back, row=2)
        with menu_card(name if name in {'prices', 'barn'} else None) as (embed, attachments):
            await interaction.response.edit_message(
                content=f'**{TITLES[name]}**', embed=embed, attachments=attachments, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def publish_guide(self, interaction, channel_id):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.guide.lock:
            self.check(interaction, admin=True)
            try:
                url, moved = await self.guide.select(channel_id)
            except discord.Forbidden as error:
                raise UserError('Discord запретил обновить памятку. Проверьте права бота в выбранном канале.') from error
            except (discord.HTTPException, discord.RateLimited, ClientError, OSError) as error:
                raise UserError('Обновление памятки не подтверждено. Проверьте канал и повторите выбор.') from error
        text = f'Памятка семьи: {url}'
        if not moved:
            text += '\nПрежнюю карточку пока не удалось пометить как перенесённую.'
        await interaction.edit_original_response(content=text)

    async def publish(self, interaction, channel_id):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.publish_lock:
            self.check(interaction, admin=True)
            channel = await interaction.guild.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != interaction.guild_id:
                raise UserError('Выберите текстовый канал сервера семьи.')
            await database_call(self.client.store, 'ensure_channel_separation', str(channel.guild.id), str(channel.id))
            permissions = channel.permissions_for(channel.guild.me)
            if not (permissions.view_channel and permissions.read_message_history and permissions.send_messages
                    and permissions.pin_messages):
                raise UserError('Боту нужны просмотр канала, чтение истории, отправка и закрепление сообщений.')
            async for message in channel.pins(limit=None):
                if message.author.id == self.client.user.id and any(
                        getattr(button, 'custom_id', None) == OPEN_ID for row in message.components
                        for button in getattr(row, 'children', ())):
                    if message.content != LAUNCHER_TEXT:
                        await message.edit(content=LAUNCHER_TEXT, allowed_mentions=discord.AllowedMentions.none())
                    await interaction.edit_original_response(content=f'Карточка уже закреплена: {message.jump_url}')
                    return
            posted = await channel.send(LAUNCHER_TEXT, view=MenuLauncher(self),
                                        allowed_mentions=discord.AllowedMentions.none())
            try:
                await posted.pin(reason='Карточка меню LaFamily')
            except (discord.HTTPException, discord.RateLimited, ClientError, OSError) as error:
                raise UserError(f'Закрепление не подтверждено. Карточка опубликована: {posted.jump_url}. '
                                'Проверьте её закрепление в канале.') from error
            await interaction.edit_original_response(content=f'Карточка закреплена: {posted.jump_url}')


class MenuLauncher(discord.ui.View):
    def __init__(self, menu):
        super().__init__(timeout=None)
        self.menu = menu

    @discord.ui.button(label='Открыть LaFamily', custom_id=OPEN_ID, style=discord.ButtonStyle.primary)
    async def open(self, interaction, button):
        await self.menu.open(interaction)

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)


class MenuView(discord.ui.View):
    def __init__(self, menu, interaction, *, admin=False):
        super().__init__(timeout=600)
        self.menu = menu
        self.owner = interaction.user.id
        self.admin = admin

    async def interaction_check(self, interaction):
        self.menu.check(interaction, owner=self.owner, admin=self.admin)
        return True

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)

    def button(self, label, action, *, row=0, disabled=False):
        button = discord.ui.Button(label=label, row=row, disabled=disabled)

        async def click(current):
            await self.interaction_check(current)
            await action(current)

        button.callback = click
        self.add_item(button)

    def section(self, name, *, row=0):
        self.button(TITLES[name], partial(self.menu.show_section, name=name), row=row)

    def command(self, label, path, *, row=0, **kwargs):
        self.button(label, partial(self.menu.invoke, path=path, **kwargs), row=row)

    def select(self, select, action):
        async def choose(current):
            await self.interaction_check(current)
            if len(select.values) != 1:
                raise UserError('Выберите один вариант.')
            await action(current, select.values[0])

        select.callback = choose
        self.add_item(select)


def register_menu(client):
    menu = FamilyMenu(client)

    @client.tree.command(name='меню', description='Открыть личное меню LaFamily',
                         guild=discord.Object(id=client.config.guild_id), extras={'access': 'all'})
    @app_commands.guild_only()
    async def open_menu(interaction: discord.Interaction):
        await menu.open(interaction)

    return menu
