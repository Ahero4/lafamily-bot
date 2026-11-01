"""Личное меню подписок: выбор товаров сохраняется только по кнопке подтверждения."""
import asyncio
import time

import discord

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.responses import send_error
from .parser import LIMITS

MODE_LABELS = {"near": "95 - 100", "max": "Макс. цена"}


class PriceSubscriptionView(discord.ui.View):
    def __init__(self, service, original, items, modes=None):
        super().__init__(timeout=600)
        self.service, self.original = service, original
        self.owner_id, self.guild_id = original.user.id, original.guild_id
        self.selected = set(items).intersection(LIMITS)
        self.modes = {item: (modes or {}).get(item, 'max') for item in self.selected}
        self.default_mode = 'max'
        self.scope_item = '*'
        self.finished = False
        self.lock = asyncio.Lock()
        self.expires_at = time.monotonic() + 600
        # Один список Discord вмещает до 25 вариантов; сейчас товаров 20.
        self.selector.options = [discord.SelectOption(label=name, value=name, default=name in self.selected)
                                 for name in LIMITS]
        self.selector.max_values = len(LIMITS)
        self.scope_selector.options = [discord.SelectOption(label='Все выбранные товары', value='*')] + [
            discord.SelectOption(label=item, value=item) for item in LIMITS]

    def prompt(self):
        scope = 'все выбранные товары' if self.scope_item == '*' else self.scope_item
        settings = '\n'.join(f'{item}: {MODE_LABELS[self.modes.get(item, self.default_mode)]}'
                             for item in LIMITS if item in self.selected) or 'Товары пока не выбраны.'
        return (
            '**Уведомления о ценах — как настроить**\n'
            '**1 · Товары.** В первом поле отметьте, по каким товарам получать теги.\n'
            '**2 · Для кого менять режим.** Во втором поле выберите «Все выбранные товары» '
            'или один товар из вашего списка. **Это поле само режим не меняет** — оно выбирает, к чему применить шаг 3.\n'
            '**3 · Режим цены.** В третьем поле выберите «95 - 100» или «Макс. цена». '
            'Режим применится к выбранной на шаге 2 области.\n'
            '**4 · Сохранить.** Проверьте список ниже и нажмите «Подтвердить».\n\n'
            '**Пример:** всем товарам «95 - 100», но меди — максимум: '
            'во втором поле выберите «Медь», в третьем — «Макс. цена».\n'
            '**95 - 100** — обычная цена от 95% до 100% максимума включительно; '
            '**Макс. цена** — обычная цена достигла максимума или выше.\n\n'
            f'**Сейчас меняете режим для:** {scope}.\n'
            f'**Ваш выбор перед сохранением:**\n{settings}\n\n'
            'Чтобы отписаться, снимите товар в первом поле. Пустой список отключает все уведомления. '
            'До «Подтвердить» изменения не сохраняются; «Отмена» их отменяет. Меню действует 10 минут.'
        )

    def check_owner(self, interaction):
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            raise UserError('Это меню принадлежит другому участнику. Откройте /скупщик-уведомление для себя.')
        if self.finished or time.monotonic() >= self.expires_at:
            raise UserError('Меню закрыто или устарело. Откройте /скупщик-уведомление заново.')

    async def interaction_check(self, interaction):
        try:
            self.check_owner(interaction)
        except UserError as error:
            await send_error(interaction, error)
            return False
        return True

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)

    def finish(self):
        self.finished = True
        self.stop()

    async def choose(self, interaction, values):
        async with self.lock:
            self.check_owner(interaction)
            if not set(values).issubset(LIMITS):
                raise UserError('Выберите товары из списка.')
            self.selected = set(values)
            self.modes = {item: self.modes.get(item, self.default_mode) for item in self.selected}
            for option in self.selector.options:
                option.default = option.value in self.selected
            await interaction.response.edit_message(content=self.prompt(), view=self,
                                                    allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.select(placeholder='1 · Выберите товары для уведомлений', min_values=0, max_values=1, row=0)
    async def selector(self, interaction, select):
        await self.choose(interaction, select.values)

    @discord.ui.button(label='Подтвердить', style=discord.ButtonStyle.success, row=3)
    async def confirm(self, interaction, button):
        await self.save(interaction)

    async def save(self, interaction):
        async with self.lock:
            self.check_owner(interaction)
            await interaction.response.defer()
            settings = None
            if self.selected:
                # Канал и права могут измениться после открытия меню: перепроверяем перед записью.
                self.service.require_enabled()
                settings = await database_call(self.service.store, 'price_settings', str(self.guild_id))
                if not settings:
                    raise UserError('Руководству нужно сначала выбрать канал через /скупщик-настройка.')
                channel = await interaction.guild.fetch_channel(int(settings['channel_id']))
                self.service.require_channel(channel, interaction.guild)
                if not channel.permissions_for(interaction.user).view_channel:
                    raise UserError('У вас нет доступа к каналу цен. Обратитесь к руководству.')
            items = await database_call(self.service.store, 'replace_price_subscriptions',
                                        str(self.guild_id), str(self.owner_id), list(self.selected), self.modes)
            # После записи повторное нажатие не должно ещё раз менять подписки.
            self.finish()
            text = ('Подписки сохранены: ' + ', '.join(items) +
                    f'.\nТег придёт в <#{settings["channel_id"]}> по выбранным порогам обычной цены. '
                    'Повторный тег — после выхода из выбранного условия и нового входа в него.\n' + '\n'.join(f'{item}: {MODE_LABELS[self.modes[item]]}' for item in items)
                    if items else 'Все уведомления о ценах отключены.')
            await interaction.edit_original_response(content=text, view=None,
                                                     allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='Отмена', style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, interaction, button):
        async with self.lock:
            self.check_owner(interaction)
            self.finish()
            await interaction.response.edit_message(content='Изменения отменены. Подписки сохранены без изменений.', view=None)

    async def on_timeout(self):
        async with self.lock:
            if self.finished:
                return
            self.finish()
            try:
                await self.original.edit_original_response(
                    content='Время выбора истекло. Подписки не изменились. Откройте /скупщик-уведомление заново.', view=None)
            except discord.HTTPException:
                pass  # Личное сообщение могло быть удалено; подписки всё равно не меняются.

    @discord.ui.select(placeholder='2 · Для кого менять режим: все товары или один', row=1)
    async def scope_selector(self, interaction, select):
        async with self.lock:
            self.check_owner(interaction)
            self.scope_item = select.values[0]
            for option in self.scope_selector.options:
                option.default = option.value == self.scope_item
            self.mode_selector.placeholder = '3 · Режим для: ' + ('все выбранные товары' if self.scope_item == '*' else self.scope_item)
            await interaction.response.edit_message(content=self.prompt(), view=self)

    @discord.ui.select(placeholder='3 · Выберите режим цены для указанной области', row=2,
                       options=[discord.SelectOption(label='95 - 100', value='near'),
                                discord.SelectOption(label='Макс. цена', value='max')])
    async def mode_selector(self, interaction, select):
        await self.choose_mode(interaction, select.values[0])

    async def choose_mode(self, interaction, mode):
        async with self.lock:
            self.check_owner(interaction)
            if mode not in MODE_LABELS:
                raise UserError('Выберите режим из списка.')
            if self.scope_item == '*':
                self.default_mode = mode
                self.modes = {item: mode for item in self.selected}
            elif self.scope_item in self.selected:
                self.modes[self.scope_item] = mode
            else:
                raise UserError('Сначала добавьте этот товар в список отслеживания.')
            await interaction.response.edit_message(content=self.prompt(), view=self,
                                                     allowed_mentions=discord.AllowedMentions.none())
