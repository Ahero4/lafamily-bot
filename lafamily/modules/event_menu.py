"""Личная навигация по мероприятиям и формы для существующих команд."""

import re
from functools import partial

import discord

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.modules.events.ui import report_embed
from lafamily.permissions import check_access, require_access_async
from lafamily.responses import send_error


def text_input(label, *, required=True, limit=80, paragraph=False, placeholder=None):
    style = discord.TextStyle.paragraph if paragraph else discord.TextStyle.short
    return discord.ui.Label(text=label, component=discord.ui.TextInput(
        required=required, max_length=limit, style=style, placeholder=placeholder,
    ))


def choice_input(label, choices):
    return discord.ui.Label(text=label, component=discord.ui.Select(
        min_values=0, max_values=1, required=False,
        options=[discord.SelectOption(label=name, value=value) for name, value in choices],
    ))


def user_input(label):
    return discord.ui.Label(text=label, component=discord.ui.UserSelect(min_values=0, max_values=1, required=False))


def read_number(value, minimum=1, maximum=2**63 - 1):
    if not value:
        return None
    if re.fullmatch(r"[0-9]{1,19}", value):
        number = int(value)
        if minimum <= number <= maximum:
            return number
    raise UserError(f"Введите целое число от {minimum} до {maximum}.")


async def event_call(menu, interaction, path, **kwargs):
    await menu.invoke(interaction, path, **kwargs)


class EventForm(discord.ui.Modal):
    def __init__(self, menu, interaction, title, fields, submit, *, access=None):
        super().__init__(title=title, timeout=300)
        self.menu = menu
        self.owner = interaction.user.id
        self.fields = fields
        self.submit = submit
        self.admin = access == "admin"
        for field in fields.values():
            self.add_item(field)

    async def on_submit(self, interaction):
        self.menu.check(interaction, owner=self.owner, admin=self.admin)
        # Проверки ролей и запросы к Discord выполняет выбранная команда после ACK.
        values = {}
        for key, label in self.fields.items():
            component = label.component
            if isinstance(component, discord.ui.TextInput):
                values[key] = component.value.strip() or None
            else:
                values[key] = component.values[0] if component.values else None
        await self.submit(interaction, **values)

    async def on_error(self, interaction, error):
        await send_error(interaction, error)


class EventView(discord.ui.View):
    def __init__(self, menu, interaction):
        super().__init__(timeout=600)
        self.menu = menu
        self.owner = interaction.user.id

    async def interaction_check(self, interaction):
        self.menu.check(interaction, owner=self.owner)
        return True

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)

    def button(self, label, callback, *, row=None, disabled=False):
        button = discord.ui.Button(label=label, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def form(self, label, title, fields_factory, submit, *, access=None, row=None):
        async def open_form(interaction):
            await interaction.response.send_modal(EventForm(
                self.menu, interaction, title, fields_factory(), submit, access=access,
            ))
        self.button(label, open_form, row=row)

    def command(self, label, path, *, row=None):
        self.button(label, partial(event_call, self.menu, path=path), row=row)


def can(interaction, settings, access):
    try:
        check_access(interaction, settings, access)
    except UserError:
        return False
    return True


async def show_screen(interaction, *, view, content=None, embed=None):
    if interaction.response.is_done():
        await interaction.edit_original_response(
            content=content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await interaction.response.edit_message(
            content=content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none(),
        )


def number_fields():
    return {"number": text_input("Номер", limit=19)}


async def number_action(menu, interaction, *, number, path=None):
    parsed = read_number(number)
    if parsed is None:
        raise UserError("Укажите номер.")
    if path is None:
        await open_saved_report(menu, interaction, parsed)
    else:
        await menu.invoke(interaction, path, number=parsed)


async def create_signup(menu, interaction, *, name, start, capacity, reminder):
    if not name or not start:
        raise UserError("Укажите название и начало мероприятия.")
    await menu.invoke(
        interaction, "мп запись создать", name=name, start=start,
        capacity=read_number(capacity, 1, 100) or 100, reminder=reminder != "no",
    )


async def open_event_menu(menu, interaction):
    menu.check(interaction)
    await interaction.response.defer()
    settings = await database_call(menu.client.store, "settings", str(interaction.guild_id))
    view = EventView(menu, interaction)
    view.form("Посмотреть отчёт", "Отчёт МП", number_fields, partial(number_action, menu), row=0)
    view.form("Состав записи", "Состав записи", number_fields,
              partial(number_action, menu, path="мп запись состав"), row=0)
    view.command("Виды МП и ставки", "мп виды список", row=0)
    if can(interaction, settings, "organizer"):
        view.button("Создать отчёт", partial(open_kind_menu, menu), row=1)
        view.form("Открыть запись", "Новая запись на МП", lambda: {
            "name": text_input("Название", limit=80),
            "start": text_input("Начало по Москве", limit=16, placeholder="ЧЧ:ММ или ГГГГ-ММ-ДД ЧЧ:ММ"),
            "capacity": text_input("Количество мест (по умолчанию 100)", required=False, limit=3),
            "reminder": choice_input("Напомнить за 15 минут?", [("Да", "yes"), ("Нет", "no")]),
        }, partial(create_signup, menu), access="organizer", row=1)
    if can(interaction, settings, "manager"):
        view.form("Закрыть запись", "Закрыть запись", number_fields,
                  partial(number_action, menu, path="мп запись закрыть"), access="manager", row=1)
    view.button("Назад", menu.show_home, row=2)
    await show_screen(interaction, content="**Мероприятия**", view=view)


async def open_saved_report(menu, interaction, number, *, back=None):
    menu.check(interaction)
    if interaction.type == discord.InteractionType.modal_submit:
        await interaction.response.defer(ephemeral=True, thinking=True)
    else:
        await interaction.response.defer()
    guild_id = str(interaction.guild_id)
    store = menu.client.store
    report = await database_call(store, "report", guild_id, number)
    settings = await database_call(store, "settings", guild_id)
    view = EventView(menu, interaction)
    if not report["cancelled_at"]:
        if can(interaction, settings, "manager"):
            async def cancel(current, reason):
                if not reason:
                    raise UserError("Укажите причину отмены.")
                await menu.invoke(current, "мп отменить", number=number, reason=reason)

            view.form("Отменить отчёт", "Причина отмены", lambda: {
                "reason": text_input("Причина", limit=300, paragraph=True),
            }, cancel, access="manager")
        if interaction.permissions.administrator and "barn_amount" not in report:
            view.button("Исправить отчёт", partial(open_editor, menu, number=number, back=back))
    view.button("Назад", back or partial(open_event_menu, menu))
    await show_screen(interaction, embed=report_embed(report), view=view)


async def edit_details(menu, number, interaction, *, date, note, note_action):
    await menu.invoke(interaction, "мп редактировать", number=number, date=date, note=note,
                      clear_note=note_action == "clear")


async def edit_roster(menu, number, interaction, *, add, remove):
    await menu.invoke(interaction, "мп редактировать", number=number,
                      add=add, remove=remove)


async def edit_result(menu, number, interaction, *, outcome, mvp, mvp_action, win_bonus, mvp_bonus):
    await menu.invoke(interaction, "мп редактировать", number=number, outcome=outcome,
                      mvp=mvp, clear_mvp=mvp_action == "clear",
                      win_bonus=read_number(win_bonus, 0, 1_000_000),
                      mvp_bonus=read_number(mvp_bonus, 0, 1_000_000))


async def edit_base(menu, number, interaction, *, points):
    await menu.invoke(interaction, "мп редактировать", number=number,
                      points=read_number(points, 0, 1_000_000))


async def open_editor(menu, interaction, *, number, back):
    menu.check(interaction, admin=True)
    view = EventView(menu, interaction)
    view.button("Вид МП", partial(open_kind_menu, menu, number=number, back=back), row=0)
    view.form("Дата и примечание", "Дата и примечание", lambda: {
        "date": text_input("Дата (по Москве)", required=False, limit=16, placeholder="ГГГГ-ММ-ДД ЧЧ:ММ"),
        "note": text_input("Новое примечание", required=False, limit=300, paragraph=True),
        "note_action": choice_input("Действие с примечанием", [("Удалить примечание", "clear")]),
    }, partial(edit_details, menu, number), access="admin", row=0)
    view.form("Состав", "Исправить состав", lambda: {
        "add": user_input("Добавить участника"),
        "remove": text_input("Убрать участников по ID или упоминаниям", required=False, limit=2400,
                             paragraph=True, placeholder="Разделяйте пробелами или запятыми"),
    }, partial(edit_roster, menu, number), access="admin", row=0)
    view.form("Итог, MVP и бонусы", "Итог, MVP и бонусы", lambda: {
        "outcome": choice_input("Новый итог", [("Победа", "win"), ("Без победы", "no_win"), ("Без итога", "none")]),
        "mvp": user_input("Выбрать MVP"),
        "mvp_action": choice_input("Действие с MVP", [("Снять MVP", "clear")]),
        "win_bonus": text_input("Бонус победы", required=False, limit=7),
        "mvp_bonus": text_input("Бонус MVP", required=False, limit=7),
    }, partial(edit_result, menu, number), access="admin", row=1)
    view.form("Базовые очки", "Базовые очки", lambda: {"points": text_input("Очки", limit=7)},
              partial(edit_base, menu, number), access="admin", row=1)
    view.button("Назад", partial(open_saved_report, menu, number=number, back=back), row=2)
    await show_screen(interaction, content=f"**Исправить отчёт #{number}**", view=view)


async def create_report(menu, kind, interaction, *, date, note, voice):
    await menu.invoke(interaction, "мп создать", kind=kind, date=date, note=note, voice=voice)


async def open_kind_menu(menu, interaction, *, number=None, back=None, page=0):
    menu.check(interaction)
    editing = number is not None
    access = "admin" if editing else "organizer"
    await interaction.response.defer()
    await require_access_async(interaction, menu.client.store, access)
    types = await database_call(menu.client.store, "types", str(interaction.guild_id))
    last_page = max(0, (len(types) - 1) // 25)
    page = max(0, min(page, last_page))
    view = EventView(menu, interaction)
    if types:
        options = [discord.SelectOption(label=kind["name"], value=str(kind["id"]))
                   for kind in types[page * 25:(page + 1) * 25]]
        select = discord.ui.Select(placeholder="Выберите вид МП", options=options, row=0)

        async def select_kind(current):
            kind = select.values[0]
            if editing:
                await menu.invoke(current, "мп редактировать", number=number, kind=kind)
                return
            await current.response.send_modal(EventForm(menu, current, "Новый отчёт МП", {
                "date": text_input("Дата (по Москве)", required=False, limit=16, placeholder="ГГГГ-ММ-ДД ЧЧ:ММ"),
                "note": text_input("Примечание", required=False, limit=300, paragraph=True),
                "voice": discord.ui.Label(text="Голосовой канал", component=discord.ui.ChannelSelect(
                    channel_types=[discord.ChannelType.voice], min_values=0, max_values=1, required=False,
                )),
            }, partial(create_report, menu, kind), access="organizer"))

        select.callback = select_kind
        view.add_item(select)
    if last_page:
        view.button("Предыдущая страница", partial(open_kind_menu, menu, number=number, back=back, page=page-1),
                    row=1, disabled=page == 0)
        view.button("Следующая страница", partial(open_kind_menu, menu, number=number, back=back, page=page+1),
                    row=1, disabled=page == last_page)
    parent = partial(open_editor, menu, number=number, back=back) if editing else partial(open_event_menu, menu)
    view.button("Назад", parent, row=2)
    title = "Вид МП" if types else "Активных видов МП пока нет."
    if last_page:
        title += f" · страница {page + 1}/{last_page + 1}"
    await show_screen(interaction, content=title, view=view)
