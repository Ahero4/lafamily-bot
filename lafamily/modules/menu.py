"""Личное меню семьи и постоянная кнопка входа."""
import asyncio

import discord
from aiohttp import ClientError
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.modules.prices.parser import LIMITS
from lafamily.permissions import check_access
from lafamily.responses import send_error

OPEN_ID = 'lafamily:menu:open:v1'
LAUNCHER_TEXT = '**Личное меню LaFamily**\nЗдесь можно посмотреть свои баллы, открыть сад и настроить уведомления о ценах.'
TITLES = {'home': 'LaFamily', 'member': 'Участник', 'prices': 'Скупщик', 'admin': 'Администрирование',
          'publish': 'Закрепить карточку LaFamily', 'reports': 'Выбрать канал админских отчётов'}


SECTION_TEXT = {
    'home': 'В разделе «Участник» собраны профиль и история баллов. В саду можно проверить посадки, у скупщика — цены и подписки.',
    'member': 'В профиле видны баллы за неделю и за всё время. В истории можно проверить начисления и отмены по каждому отчёту.',
    'prices': 'Подписки определяют, при какой цене придёт ЛС. Чтобы посмотреть, как менялась цена, выберите товар в списке.',
    'admin': 'Проверьте состояние бота, разберите незавершённые операции или посмотрите результаты рассылок.',
    'publish': 'В выбранном канале бот закрепит кнопку «Открыть LaFamily». Каждый участник увидит своё личное меню.',
    'reports': 'Выберите служебный канал для отчётов о доставке ЛС и предупреждений о состоянии бота. Сообщения увидят все, у кого есть доступ к каналу.',
}


def section_text(section):
    return f'**{TITLES[section]}**\n{SECTION_TEXT[section]}'


def check_server(interaction, client):
    if interaction.guild is None or interaction.guild_id != client.config.guild_id:
        raise UserError('Меню доступно только на сервере семьи.')
    if client.backups.restoring:
        raise UserError('Дождитесь завершения восстановления базы.')


class FamilyMenu:
    def __init__(self, client, actions):
        self.client = client
        self.actions = actions
        self.publish_lock = asyncio.Lock()

    async def open(self, interaction):
        check_server(interaction, self.client)
        await interaction.response.send_message(content=section_text('home'),
            view=MenuView(self, interaction, 'home'), ephemeral=True)

    async def publish(self, interaction, channel_id):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.publish_lock:
            check_server(interaction, self.client)
            check_access(interaction, None, 'admin')
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
    def __init__(self, menu, interaction, section):
        super().__init__(timeout=600)
        self.menu, self.owner, self.section = menu, interaction.user.id, section
        if section == 'home':
            self.section_button('member')
            self.action_button('Сад', 'garden')
            self.section_button('prices')
            if interaction.permissions.administrator:
                self.section_button('admin')
        elif section == 'member':
            self.action_button('Мой профиль', 'profile')
            self.action_button('История моих баллов', 'points')
        elif section == 'prices':
            self.action_button('Подписки', 'subscriptions')
            self.action_button('Проверить ЛС', 'test_dm')
            select = discord.ui.Select(placeholder='История цен — выберите товар',
                                       options=[discord.SelectOption(label=name) for name in LIMITS])
            async def history(current):
                await menu.actions['history'](current, select.values[0])
            select.callback = history
            self.add_item(select)
        elif section == 'admin':
            self.action_button('Диагностика', 'diagnostics')
            self.action_button('Незавершённые операции', 'operations')
            self.action_button('Статистика ЛС', 'statistics')
            self.section_button('reports')
            self.section_button('publish')
        elif section in ('reports', 'publish'):
            select = discord.ui.ChannelSelect(placeholder='Выберите текстовый канал',
                                              channel_types=[discord.ChannelType.text])
            async def choose(current):
                selected = select.values[0]
                if section == 'publish':
                    await menu.publish(current, selected.id)
                else:
                    channel = current.guild.get_channel(selected.id)
                    if not isinstance(channel, discord.TextChannel):
                        raise UserError('Канал недоступен. Откройте меню снова.')
                    await menu.actions['reports'](current, channel)
            select.callback = choose
            self.add_item(select)
        if section != 'home':
            self.section_button('admin' if section in ('publish', 'reports') else 'home', 'Назад')

    def section_button(self, section, label=None):
        button = discord.ui.Button(label=label or TITLES[section])
        async def switch(current):
            if section == 'admin':
                check_access(current, None, 'admin')
            await current.response.edit_message(content=section_text(section),
                                                view=MenuView(self.menu, current, section))
            self.stop()
        button.callback = switch
        self.add_item(button)

    def action_button(self, label, action):
        button = discord.ui.Button(label=label)
        button.callback = self.menu.actions[action]
        self.add_item(button)

    async def interaction_check(self, interaction):
        check_server(interaction, self.menu.client)
        if interaction.user.id != self.owner:
            raise UserError('Откройте собственное меню.')
        if self.section in ('admin', 'publish', 'reports'):
            check_access(interaction, None, 'admin')
        return True

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)


def register_menu(client, actions):
    menu = FamilyMenu(client, actions)

    @client.tree.command(name='меню', description='Открыть личное меню LaFamily',
                         guild=discord.Object(id=client.config.guild_id), extras={'access': 'all'})
    @app_commands.guild_only()
    async def open_menu(interaction: discord.Interaction):
        await menu.open(interaction)

    return menu
