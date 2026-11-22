"""Поля меню запускают те же операции, что и slash-команды."""
import re

import discord

from lafamily.errors import UserError
from lafamily.responses import send_error


class MenuModal(discord.ui.Modal):
    def __init__(self, menu, interaction, title, fields, submit, *, admin=False):
        super().__init__(title=title, timeout=300)
        self.menu = menu
        self.owner = interaction.user.id
        self.submit = submit
        self.admin = admin
        for label, field in fields:
            self.add_item(discord.ui.Label(text=label, component=field))

    async def on_submit(self, interaction):
        self.menu.check(interaction, owner=self.owner, admin=self.admin)
        await self.submit(interaction)

    async def on_error(self, interaction, error):
        await send_error(interaction, error)


def positive_number(value: str, label: str, maximum: int) -> int:
    value = value.strip()
    if not value.isascii() or not value.isdigit() or len(value) > len(str(maximum)) or not 1 <= int(value) <= maximum:
        raise UserError(f'{label}: укажите целое число от 1 до {maximum}.')
    return int(value)


def selected_value(select, allowed):
    values = select.values
    if len(values) != 1 or values[0] not in allowed:
        raise UserError('Выберите один из предложенных вариантов.')
    return values[0]


async def open_barn_form(menu, interaction):
    menu.check(interaction)
    amount = discord.ui.TextInput(max_length=40, placeholder='157000 или +157к')
    recipient = discord.ui.UserSelect(min_values=0, max_values=1, required=False)

    async def submit(current):
        members = recipient.values
        if len(members) > 1:
            raise UserError('Выберите одного получателя.')
        # Команда перепроверит участника на сервере и опубликует ответ на это новое действие.
        await menu.invoke(current, 'коровник сбор', amount=amount.value, member=members[0] if members else None)

    await interaction.response.send_modal(MenuModal(menu, interaction, 'Сбор коровника',
        (('Сумма сбора', amount), ('Участник (по умолчанию вы)', recipient)), submit))


async def open_bank_form(menu, interaction, *, sheet):
    menu.check(interaction, admin=True)
    bank = discord.ui.TextInput(max_length=10, placeholder='Например 1000000')
    week = discord.ui.Select(options=[
        discord.SelectOption(label='Прошлая неделя', value='-1', default=True),
        discord.SelectOption(label='Текущая неделя', value='0'),
    ])
    command = 'баллы ведомости создать' if sheet else 'баллы премии'
    title = 'Создать ведомость' if sheet else 'Рассчитать премии'

    async def submit(current):
        amount = positive_number(bank.value, 'Банк премии', 1_000_000_000)
        period = int(selected_value(week, {'-1', '0'}))
        await menu.invoke(current, command, bank=amount, week=period)

    await interaction.response.send_modal(MenuModal(menu, interaction, title,
        (('Банк премии', bank), ('Неделя', week)), submit, admin=True))


async def open_number_form(menu, title, path, interaction, *, select_status=False, kind=None):
    menu.check(interaction, admin=True)
    number = discord.ui.TextInput(max_length=16)
    fields = [('Номер записи', number)]
    status = None
    if select_status:
        status = discord.ui.Select(options=[
            discord.SelectOption(label='Утверждена', value='approved'),
            discord.SelectOption(label='Выплачена', value='paid'),
        ])
        fields.append(('Новый статус', status))

    async def submit(current):
        kwargs = {'number': positive_number(number.value, 'Номер записи', 2**53 - 1)}
        if status is not None:
            kwargs['state'] = selected_value(status, {'approved', 'paid'})
        if kind is not None:
            kwargs['kind'] = kind
        await menu.invoke(current, path, **kwargs)

    await interaction.response.send_modal(MenuModal(menu, interaction, title, fields, submit, admin=True))


async def open_restore_form(menu, interaction):
    menu.check(interaction, admin=True)
    message_id = discord.ui.TextInput(max_length=20, placeholder='17–20 цифр')

    async def submit(current):
        target = message_id.value.strip()
        if not re.fullmatch(r'[0-9]{17,20}', target):
            raise UserError('Укажите ID сообщения с бэкапом: 17–20 цифр.')
        await menu.invoke(current, 'бот бэкапы восстановить', message_id=target)

    await interaction.response.send_modal(MenuModal(menu, interaction, 'Восстановить из бэкапа',
        (('ID сообщения с архивом', message_id),), submit, admin=True))
