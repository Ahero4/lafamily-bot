import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.event_menu import open_event_menu, open_saved_report
from lafamily.modules.events.models import Draft


class EventMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.configure("100", "30", "201", "1", "2")
        self.kind = self.store.set_type("100", "30", "Капт", 10)
        self.report = self.store.create_report(Draft(
            "100", "30", "201", self.kind["id"], self.kind["name"], 10, "2026-11-20T12:00:00Z",
            members=["111111111111111111"],
        ))
        self.menu = SimpleNamespace(client=SimpleNamespace(store=self.store), invoke=AsyncMock(),
                                    show_home=AsyncMock(), check=self.check)

    @staticmethod
    def check(interaction, *, owner=None, admin=False):
        if interaction.guild_id != 100 or (owner is not None and interaction.user.id != owner):
            raise UserError("Нет доступа.")
        if admin and not interaction.permissions.administrator:
            raise UserError("Нужен администратор.")

    @staticmethod
    def interaction(*, role=None, admin=False, user=30, guild=100,
                    interaction_type=discord.InteractionType.component):
        response = SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock(),
                                   is_done=lambda: True)
        member = SimpleNamespace(id=user, roles=[SimpleNamespace(id=role)] if role else [])
        return SimpleNamespace(response=response, edit_original_response=AsyncMock(), type=interaction_type,
                               user=member, guild_id=guild, guild=SimpleNamespace(id=guild),
                               permissions=discord.Permissions(administrator=admin))

    @staticmethod
    def view(interaction):
        return interaction.edit_original_response.call_args.kwargs["view"]

    @staticmethod
    def labels(view):
        return [item.label for item in view.children if isinstance(item, discord.ui.Button)]

    @staticmethod
    def button(view, name):
        return next(item for item in view.children if isinstance(item, discord.ui.Button) and item.label == name)

    async def test_event_menu_shows_controls_for_live_roles(self):
        expected = ["Посмотреть отчёт", "Состав записи", "Виды МП и ставки"]
        for role, admin, extra in [
            (None, False, []),
            (1, False, ["Создать отчёт", "Открыть запись"]),
            (2, False, ["Создать отчёт", "Открыть запись", "Закрыть запись"]),
            (None, True, ["Создать отчёт", "Открыть запись", "Закрыть запись"]),
        ]:
            with self.subTest(role=role, admin=admin):
                interaction = self.interaction(role=role, admin=admin)
                await open_event_menu(self.menu, interaction)
                view = self.view(interaction)
                self.assertEqual(self.labels(view), expected + extra + ["Назад"])
                with self.assertRaises(UserError):
                    await view.interaction_check(self.interaction(user=31))

    async def test_report_from_modal_is_private_and_back_returns_to_caller(self):
        interaction = self.interaction(interaction_type=discord.InteractionType.modal_submit)
        back = AsyncMock()
        await open_saved_report(self.menu, interaction, self.report["id"], back=back)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertIn(str(self.report["id"]), interaction.edit_original_response.call_args.kwargs["embed"].title)
        view = self.view(interaction)
        self.assertEqual(self.labels(view), ["Назад"])
        clicked = self.interaction()
        await self.button(view, "Назад").callback(clicked)
        back.assert_awaited_once_with(clicked)

    async def test_report_actions_capture_id_and_check_restored_interaction(self):
        opening = self.interaction(admin=True)
        await open_saved_report(self.menu, opening, self.report["id"])
        view = self.view(opening)
        self.assertEqual(self.labels(view), ["Отменить отчёт", "Исправить отчёт", "Назад"])
        await self.button(view, "Отменить отчёт").callback(opening)
        modal = opening.response.send_modal.call_args.args[0]
        modal.fields["reason"].component._value = "Опечатка"
        for invalid in [self.interaction(admin=True, user=31), self.interaction(admin=True, guild=200)]:
            with self.assertRaises(UserError):
                await modal.on_submit(invalid)
        self.menu.invoke.assert_not_awaited()
        submitted = self.interaction(role=2, interaction_type=discord.InteractionType.modal_submit)
        await modal.on_submit(submitted)
        self.menu.invoke.assert_awaited_once_with(submitted, "мп отменить", number=self.report["id"], reason="Опечатка")

    async def test_editor_opens_real_modal_and_keeps_zero_base_points(self):
        opening = self.interaction(admin=True)
        await open_saved_report(self.menu, opening, self.report["id"])
        await self.button(self.view(opening), "Исправить отчёт").callback(opening)
        editor = self.view(opening)
        await self.button(editor, "Итог, MVP и бонусы").callback(opening)
        modal = opening.response.send_modal.call_args.args[0]
        self.assertEqual(len(modal.to_dict()["components"]), 5)
        self.assertFalse(modal.fields["outcome"].component.required)

        await self.button(editor, "Базовые очки").callback(opening)
        modal = opening.response.send_modal.call_args.args[0]
        modal.fields["points"].component._value = "0"
        with self.assertRaises(UserError):
            await modal.on_submit(self.interaction(interaction_type=discord.InteractionType.modal_submit))
        submitted = self.interaction(admin=True, interaction_type=discord.InteractionType.modal_submit)
        await modal.on_submit(submitted)
        self.menu.invoke.assert_awaited_once_with(submitted, "мп редактировать", number=self.report["id"], points=0)

    async def test_removal_and_result_forms_do_not_require_a_user_selection(self):
        opening = self.interaction(admin=True)
        await open_saved_report(self.menu, opening, self.report["id"])
        await self.button(self.view(opening), "Исправить отчёт").callback(opening)
        editor = self.view(opening)
        for button, field in [("Итог, MVP и бонусы", "mvp"), ("Состав", "add")]:
            await self.button(editor, button).callback(opening)
            modal = opening.response.send_modal.call_args.args[0]
            user_select = modal.fields[field].to_component_dict()["component"]
            self.assertEqual(user_select["min_values"], 0)
            self.assertFalse(user_select["required"])
        modal.fields["remove"].component._value = "111111111111111111"
        submitted = self.interaction(admin=True, interaction_type=discord.InteractionType.modal_submit)
        await modal.on_submit(submitted)
        self.menu.invoke.assert_awaited_once_with(
            submitted, "мп редактировать", number=self.report["id"], add=None,
            remove="111111111111111111",
        )

    async def test_editor_passes_resolved_user_without_fetch_before_ack(self):
        opening = self.interaction(admin=True)
        await open_saved_report(self.menu, opening, self.report["id"])
        await self.button(self.view(opening), "Исправить отчёт").callback(opening)
        await self.button(self.view(opening), "Состав").callback(opening)
        modal = opening.response.send_modal.call_args.args[0]
        selected = SimpleNamespace(id=222222222222222222, bot=False)
        modal.fields["add"].component._values = [selected]
        submitted = self.interaction(admin=True, interaction_type=discord.InteractionType.modal_submit)
        submitted.guild.fetch_member = AsyncMock()
        await modal.on_submit(submitted)
        submitted.guild.fetch_member.assert_not_awaited()
        self.menu.invoke.assert_awaited_once_with(submitted, "мп редактировать", number=self.report["id"],
                                                add=selected, remove=None)

    async def test_report_type_pages_and_creation_use_fresh_modal_submit(self):
        for index in range(27):
            self.store.set_type("100", "30", f"Тест {index:02d}", 10)
        opening = self.interaction(role=1)
        await open_event_menu(self.menu, opening)
        await self.button(self.view(opening), "Создать отчёт").callback(opening)
        next_page = self.button(self.view(opening), "Следующая страница")
        await next_page.callback(opening)
        select = self.view(opening).children[0]
        self.assertEqual(len(select.options), 3)
        selected = select.options[-1].value
        select._values = [selected]
        await select.callback(opening)
        modal = opening.response.send_modal.call_args.args[0]
        self.menu.invoke.assert_not_awaited()
        modal.fields["note"].component._value = "Примечание"
        submitted = self.interaction(role=1, interaction_type=discord.InteractionType.modal_submit)
        await modal.on_submit(submitted)
        self.menu.invoke.assert_awaited_once_with(submitted, "мп создать", kind=selected, date=None,
                                                note="Примечание", voice=None)
