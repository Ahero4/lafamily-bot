import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.service import EventService
from lafamily.modules.events.time import parse_event_date, week_range
from lafamily.modules.events.ui import report_embed
from lafamily.permissions import require_access

GUILD = "111111111111111111"
ACTOR = "222222222222222222"
MEMBER = "333333333333333333"
CHANNEL = "444444444444444444"


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.store.configure(GUILD, ACTOR, CHANNEL, "organizer", "manager")
        self.store.set_type(GUILD, ACTOR, "Семейное МП", 12)
        self.service = EventService(self.store)
        self.addCleanup(self.store.close)
        self.addCleanup(self.service.close)

    def interaction(self, roles=("organizer",), user_id=ACTOR, admin=False):
        return SimpleNamespace(
            guild_id=int(GUILD), guild=SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(bot=False))),
            user=SimpleNamespace(id=int(user_id), roles=[SimpleNamespace(id=role) for role in roles]),
            permissions=discord.Permissions(administrator=admin),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
        )

    def new_view(self):
        kind = self.store.types(GUILD)[0]
        return self.service.new_view(Draft(GUILD, ACTOR, CHANNEL, kind["id"], kind["name"], 12, parse_event_date(None)))

    async def add_member(self, view):
        member = MagicMock(spec=discord.Member)
        member.id, member.bot = int(MEMBER), False
        await view.dispatch("add", self.interaction(), [member])

    async def test_workflow_only_credits_after_confirmation(self):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=555555555555555555)))
        self.service.channel = AsyncMock(return_value=channel)
        view = self.new_view()
        self.assertEqual(self.store.statistics(GUILD, week_range()), [])
        await self.add_member(view)
        self.assertEqual(self.store.statistics(GUILD, week_range()), [])
        confirm = self.interaction()
        await view.dispatch("confirm", confirm)
        self.assertEqual(self.store.statistics(GUILD, week_range(), MEMBER)[0]["points"], 12)
        channel.send.assert_awaited_once()
        self.assertTrue(view.finished)
        with self.assertRaises(UserError):
            await view.dispatch("confirm", self.interaction())

    async def test_publication_failure_preserves_points_and_warns(self):
        self.service.channel = AsyncMock(return_value=SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("offline"))))
        view = self.new_view()
        await self.add_member(view)
        confirm = self.interaction()
        await view.dispatch("confirm", confirm)
        self.assertEqual(self.store.statistics(GUILD, week_range(), MEMBER)[0]["points"], 12)
        content = confirm.edit_original_response.call_args.kwargs["content"]
        self.assertIn("Повторно подавать отчёт не нужно", content)

    async def test_role_loss_blocks_old_draft(self):
        view = self.new_view()
        await self.add_member(view)
        with self.assertRaises(UserError):
            await view.dispatch("confirm", self.interaction(roles=()))
        self.assertEqual(self.store.statistics(GUILD, week_range()), [])

    async def test_other_user_and_expired_draft_are_blocked(self):
        view = self.new_view()
        with self.assertRaises(UserError):
            await view.dispatch("clear", self.interaction(user_id=MEMBER))
        view.expires_at = time.monotonic() - 1
        with self.assertRaises(UserError):
            await view.dispatch("clear", self.interaction())

    async def test_new_draft_replaces_old_and_clear_resets_members(self):
        previous = self.new_view()
        view = self.new_view()
        self.assertTrue(previous.finished)
        await self.add_member(view)
        await self.add_member(view)
        self.assertEqual(view.draft.members, [MEMBER])
        await view.dispatch("clear", self.interaction())
        self.assertEqual(view.draft.members, [])
        self.assertTrue(view.confirm.disabled)

    async def test_parallel_confirmation_does_not_duplicate_credit(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_channel(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=555555555555555555)))

        self.service.channel = slow_channel
        view = self.new_view()
        await self.add_member(view)
        first = asyncio.create_task(view.dispatch("confirm", self.interaction()))
        await entered.wait()
        try:
            with self.assertRaises(UserError):
                await view.dispatch("confirm", self.interaction())
        finally:
            release.set()
            await first
        self.assertEqual(self.store.statistics(GUILD, week_range(), MEMBER)[0]["events"], 1)

    async def test_startup_registers_only_configured_guild_not_on_ready(self):
        bot = FamilyBot(Config("token", 123, int(GUILD), Path(":memory:")), self.store)
        try:
            bot.tree.sync = AsyncMock(return_value=[])
            with patch.object(bot, "add_view") as add_view:
                await bot.setup_hook()
                self.assertEqual(add_view.call_count, 2)
            bot.tree.sync.assert_awaited_once_with(guild=discord.Object(id=int(GUILD)))
            await bot.on_ready()
            await bot.on_ready()
            self.assertEqual(bot.tree.sync.await_count, 1)
        finally:
            await bot.close()

    async def test_startup_registration_failure_is_not_ignored(self):
        bot = FamilyBot(Config("token", 123, int(GUILD), Path(":memory:")), self.store)
        try:
            bot.tree.sync = AsyncMock(side_effect=RuntimeError("network"))
            with patch.object(bot, "add_view"), self.assertRaisesRegex(UserError, "зарегистрировать команды"):
                await bot.setup_hook()
            bot.tree.sync.assert_awaited_once()
        finally:
            await bot.close()

    async def test_commands_build_with_russian_names_and_options(self):
        bot = FamilyBot(Config("fake", 666666666666666666, int(GUILD), Path(":memory:")), self.store)
        try:
            commands = bot.tree.get_commands(guild=discord.Object(id=int(GUILD)))
            data = {command.name: command.to_dict(bot.tree) for command in commands}
            self.assertEqual(set(data), {"мп", "баллы", "бот", "помощь", "команды", "инфо", "ники", "скупщик", "коровник", "старт", "заявки", "анонс"})
            self.assertEqual({item["name"] for item in data["инфо"]["options"]}, {"общее", "фарм", "админ"})
            self.assertEqual({item["name"] for item in data["старт"]["options"]}, {"участник", "админ"})
            event = data["мп"]["options"]
            self.assertEqual({item["name"] for item in event}, {"создать", "виды", "посмотреть", "отменить"})
            kinds = next(item for item in event if item["name"] == "виды")["options"]
            self.assertEqual({item["name"] for item in kinds}, {"список", "добавить", "отключить"})
            add = next(item for item in kinds if item["name"] == "добавить")
            self.assertEqual([item["name"] for item in add["options"]], ["название", "очки", "очки-победы", "очки-mvp"])
            self.assertTrue(add["options"][0]["autocomplete"])
            self.assertTrue(add["options"][0]["required"])
            self.assertTrue(all(not item.get("required", False) for item in add["options"][1:]))
            payout = next(item for item in data["баллы"]["options"] if item["name"] == "премии")
            self.assertEqual([p["name"] for p in payout["options"]], ["банк-денег", "неделя"])
            self.assertTrue(payout["options"][0]["required"])
            self.assertFalse(payout["options"][1].get("required", False))
            self.assertEqual({c["value"] for c in payout["options"][1]["choices"]}, {0, -1})
            report = next(item for item in event if item["name"] == "создать")
            self.assertEqual([item["name"] for item in report["options"]], ["вид", "дата", "примечание", "голосовой-канал"])
            self.assertTrue(report["options"][0]["autocomplete"])
        finally:
            await bot.close()

    async def test_roles_distinguish_manager_organizer_admin(self):
        for roles, level, allowed in [
            ((), "organizer", False), (("organizer",), "organizer", True),
            (("organizer",), "manager", False), (("manager",), "manager", True),
            (("manager",), "organizer", True), (("manager",), "admin", False),
        ]:
            with self.subTest(roles=roles, level=level):
                if allowed:
                    require_access(self.interaction(roles=roles), self.store, level)
                else:
                    with self.assertRaises(UserError):
                        require_access(self.interaction(roles=roles), self.store, level)
        require_access(self.interaction(roles=(), admin=True), self.store, "admin")

    async def test_full_report_fits_discord_embed_limits(self):
        embed = report_embed({"id": 1, "type_name": "*" * 80, "points": 1000000,
            "occurred_at": "2026-10-24T17:00:00.000Z", "organizer_id": "1" * 20,
            "note": "*" * 300, "cancelled_at": "2026-10-24T18:00:00.000Z", "cancel_reason": "*" * 300,
            "members": [str(10000000000000000000 + index) for index in range(100)]})
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertEqual(sum(field.name.startswith("Участники") for field in embed.fields), 4)
