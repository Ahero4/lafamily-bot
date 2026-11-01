"""Администраторская сводка: оба периода, отмена, страницы и ограничения доступа."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.commands import EventCommands
from lafamily.modules.events.models import Draft
from lafamily.modules.events.service import EventService
from lafamily.modules.events.time import week_range

GUILD = "111111111111111111"
ACTOR = "222222222222222222"
MEMBER = "333333333333333333"
OTHER = "444444444444444444"
PERIOD = week_range(now=datetime(2026, 10, 25, 10, tzinfo=UTC))


def setup_store():
    store = Store(":memory:")
    store.configure(GUILD, ACTOR, "555555555555555555", "organizer", "manager")
    store.set_type(GUILD, ACTOR, "МП", 10)
    kind = store.types(GUILD)[0]
    draft = Draft(GUILD, ACTOR, "555555555555555555", kind["id"], kind["name"], 10,
                  "2026-10-18T10:00:00.000Z", members=[MEMBER, OTHER])
    return store, draft


class SummaryStorageTests(unittest.TestCase):
    def setUp(self):
        self.store, self.draft = setup_store()
        self.addCleanup(self.store.close)

    def test_old_participants_are_included_with_zero_week_points(self):
        self.store.create_report(self.draft)
        self.store.create_report(replace(self.draft, request_id="this-week", occurred_at=PERIOD[0], points=7, members=[MEMBER]))
        rows = self.store.points_summary(GUILD, PERIOD)
        self.assertEqual(rows, [
            {"user_id": MEMBER, "total_points": 17, "week_points": 7},
            {"user_id": OTHER, "total_points": 10, "week_points": 0},
        ])

    def test_cancellation_and_guild_isolation_apply_to_both_totals(self):
        self.store.create_report(self.draft)
        current = self.store.create_report(replace(self.draft, request_id="current", occurred_at=PERIOD[0], points=7, members=[MEMBER]))
        self.store.cancel_report(GUILD, ACTOR, current["id"], "Ошибка")
        rows = self.store.points_summary(GUILD, PERIOD)
        self.assertTrue(all(row["total_points"] == 10 and row["week_points"] == 0 for row in rows))
        self.assertEqual(self.store.points_summary("999999999999999999", PERIOD), [])

    def test_week_start_included_and_end_excluded_from_week_column(self):
        for index, occurred in enumerate(("2026-10-18T20:59:59.999Z", PERIOD[0], "2026-10-25T20:59:59.999Z", PERIOD[1])):
            self.store.create_report(replace(self.draft, request_id=f"boundary-{index}", occurred_at=occurred, members=[MEMBER]))
        self.assertEqual(self.store.points_summary(GUILD, PERIOD), [
            {"user_id": MEMBER, "total_points": 40, "week_points": 20},
        ])

    def test_pages_are_stable_with_tied_points(self):
        members = [str(600000000000000000 + index) for index in range(25)]
        self.store.create_report(replace(self.draft, members=members))
        first = self.store.points_summary(GUILD, PERIOD, limit=20)
        second = self.store.points_summary(GUILD, PERIOD, limit=20, offset=20)
        self.assertEqual([row["user_id"] for row in first + second], members)
        self.assertEqual(len(first), 20)
        self.assertEqual(len(second), 5)


class SummaryCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.draft = setup_store()
        self.addCleanup(self.store.close)
        self.service = EventService(self.store)
        self.addCleanup(self.service.close)
        self.group = EventCommands(self.service)

    def interaction(self, admin=False, role=None):
        return SimpleNamespace(
            guild_id=int(GUILD), guild=SimpleNamespace(), permissions=discord.Permissions(administrator=admin),
            user=SimpleNamespace(roles=[SimpleNamespace(id=role)] if role else []),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()), edit_original_response=AsyncMock(),
        )

    async def execute(self, interaction, page=1):
        with patch("lafamily.modules.events.commands.week_range", return_value=PERIOD):
            await self.group.points_statistics.callback(self.group, interaction, page)

    async def test_non_admin_roles_are_denied_before_sending_statistics(self):
        for role in (None, "organizer", "manager"):
            interaction = self.interaction(role=role)
            with self.subTest(role=role), self.assertRaises(UserError):
                await self.execute(interaction)
            interaction.response.send_message.assert_not_awaited()

    async def test_admin_gets_both_columns_privately_without_pings(self):
        self.store.create_report(self.draft)
        self.store.create_report(replace(self.draft, request_id="this-week", occurred_at=PERIOD[0], points=7, members=[MEMBER]))
        interaction = self.interaction(admin=True)
        await self.execute(interaction)
        payload = interaction.edit_original_response.call_args.kwargs
        self.assertTrue(interaction.response.defer.call_args.kwargs["ephemeral"])
        self.assertEqual(payload["allowed_mentions"].to_dict(), {"parse": []})
        self.assertIn(f"<@{MEMBER}> — всего **17**, неделя **7**", payload["embed"].description)
        self.assertIn(f"<@{OTHER}> — всего **10**, неделя **0**", payload["embed"].description)

    async def test_empty_result_and_next_page_hint(self):
        interaction = self.interaction(admin=True)
        await self.execute(interaction)
        self.assertIn("пока нет", interaction.edit_original_response.call_args.kwargs["embed"].description)
        self.store.create_report(replace(self.draft, members=[str(600000000000000000 + index) for index in range(21)]))
        await self.execute(interaction)
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        self.assertIn("страница:2", embed.footer.text)
        self.assertEqual(embed.description.count("<@"), 20)
        await self.execute(interaction, page=2)
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        self.assertEqual(embed.description.count("<@"), 1)
        self.assertNotIn("Дальше", embed.footer.text)
        await self.execute(interaction, page=3)
        self.assertIn("меньший номер", interaction.edit_original_response.call_args.kwargs["embed"].description)
