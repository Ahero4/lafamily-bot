"""Выбор сегодняшней даты не фиксирует время раньше создания отчёта."""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.commands import EventCommands
from lafamily.modules.events.service import EventService
from lafamily.modules.events.time import parse_event_date


class DateChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.service = EventService(self.store)
        self.addCleanup(self.service.close)
        self.command = EventCommands(self.service)

    async def test_today_choice_uses_marker_and_manual_input_remains_available(self):
        choices = await self.command.date_autocomplete(None, "")
        self.assertEqual(choices[0].value, "сегодня")
        choices = await self.command.date_autocomplete(None, "2026-01-01 12:34")
        self.assertEqual([choice.value for choice in choices], ["сегодня", "2026-01-01 12:34"])
        choices = await self.command.date_autocomplete(None, "2026-01-")
        self.assertEqual([choice.value for choice in choices], ["сегодня"])

    async def test_date_option_is_optional_and_supports_autocomplete(self):
        data = self.command.create_report.to_dict(None)
        date = next(option for option in data["options"] if option["name"] == "дата")
        self.assertTrue(date["autocomplete"])
        self.assertFalse(date["required"])

    async def test_today_is_resolved_when_command_is_created_not_when_suggestion_is_opened(self):
        guild, actor, channel = "111111111111111111", "222222222222222222", "333333333333333333"
        self.store.configure(guild, actor, channel, "organizer", "manager")
        self.store.set_type(guild, actor, "МП", 10)
        kind = self.store.types(guild)[0]
        interaction = SimpleNamespace(
            guild_id=int(guild), channel_id=int(channel), guild=SimpleNamespace(), user=SimpleNamespace(id=int(actor)),
            permissions=discord.Permissions(administrator=True),
            channel=SimpleNamespace(get_partial_message=lambda message_id: SimpleNamespace(edit=AsyncMock())),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
        )
        # Подсказка могла быть открыта до московской полуночи, а команда отправлена после неё.
        with patch("lafamily.modules.events.commands.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 26, 20, 59, tzinfo=UTC)
            choices = await self.command.date_autocomplete(interaction, "")
        created = datetime(2026, 10, 26, 21, 1, 12, tzinfo=UTC)
        with patch("lafamily.modules.events.commands.datetime") as clock:
            clock.now.return_value = created
            await self.command.create_report.callback(self.command, interaction, str(kind["id"]), choices[0].value)
        view = self.service.views[(guild, actor)]
        self.assertEqual(view.draft.occurred_at, "2026-10-26T21:01:12.000Z")


class DateParsingTests(unittest.TestCase):
    def test_today_default_and_manual_moscow_date(self):
        now = datetime(2026, 10, 27, 10, 15, 34, tzinfo=UTC)
        for value in (None, "сегодня", " Сегодня "):
            self.assertEqual(parse_event_date(value, now), "2026-10-27T10:15:34.000Z")
        self.assertEqual(parse_event_date("2026-10-27 12:00", now), "2026-10-27T09:00:00.000Z")
        with self.assertRaises(UserError):
            parse_event_date("2026-10-28 12:00", now)
