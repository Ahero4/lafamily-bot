"""Правка сохранённого МП без повторного отчёта и начисления."""

import asyncio
import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.commands import EventCommands
from lafamily.modules.events.models import Draft
from lafamily.modules.events.service import EventService, publish_report_message
from lafamily.modules.events.time import parse_event_date
from lafamily.modules.events.ui import report_embed
from lafamily.modules.health import HealthService


GUILD = "111111111111111111"
ADMIN = "222222222222222222"
ORGANIZER = "333333333333333333"
CHANNEL = "444444444444444444"
FIRST = "555555555555555555"
SECOND = "666666666666666666"
ADDED = "777777777777777777"
FOURTH = "888888888888888888"
DATE = "2026-01-03 12:00"
PERIOD = ("2025", "2027")


class SavedReportEditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.configure(GUILD, ADMIN, CHANNEL, ORGANIZER, ADMIN)
        self.kind = self.store.set_type(GUILD, ORGANIZER, "Капт", 10, 4, 5)
        self.original = self.store.create_report(Draft(
            GUILD, ORGANIZER, CHANNEL, self.kind["id"], "Капт", 10, parse_event_date(DATE), note="Заметка",
            members=[FIRST, SECOND], outcome="win", win_bonus=4, mvp_id=FIRST, mvp_bonus=5,
        ))
        self.number = self.original["id"]
        self.store.attach_message(GUILD, self.number, "810")
        self.original = self.store.report(GUILD, self.number)
        self.message = SimpleNamespace(edit=AsyncMock())
        self.channel = SimpleNamespace(get_partial_message=lambda message_id: self.message)
        self.service = EventService(self.store)
        self.service.channel = AsyncMock(return_value=self.channel)
        self.commands = EventCommands(self.service)

    def interaction(self, *, actor=ADMIN, administrator=True, guild_id=GUILD):
        return SimpleNamespace(
            guild_id=int(guild_id), user=SimpleNamespace(id=int(actor), roles=[SimpleNamespace(id=int(ADMIN))]),
            guild=SimpleNamespace(id=int(guild_id), fetch_member=AsyncMock(return_value=SimpleNamespace(bot=False))),
            permissions=discord.Permissions(administrator=administrator),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
        )

    async def preview(self, request, **changes):
        await self.commands.edit_report.callback(self.commands, request, self.number, **changes)
        payload = request.edit_original_response.call_args.kwargs
        self.addCleanup(payload["view"].stop)
        return payload

    def saved(self):
        return self.store.report(GUILD, self.number)

    def audit_edits(self):
        return self.store.db.execute("SELECT * FROM audit_log WHERE action='report.edited' ORDER BY id").fetchall()

    @staticmethod
    def edit_summary(embed):
        return next((field.value for field in embed.fields if field.name == "Последнее исправление"), None)

    async def confirm(self, payload, interaction=None):
        await payload["view"].confirm.callback(interaction or self.interaction())

    async def test_updates_same_report_roster_points_history_and_publication(self):
        new_kind = self.store.set_type(GUILD, ADMIN, "Гонка", 100, 20, 30)
        request = self.interaction()
        payload = await self.preview(
            request, kind=str(new_kind["id"]), date="2026-01-04 13:00", clear_note=True,
            remove=f"<@!{FIRST}>", add=SimpleNamespace(id=int(ADDED), bot=False), outcome="no_win",
            mvp=SimpleNamespace(id=int(ADDED), bot=False), points=12, mvp_bonus=3,
        )
        request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertIn("33 → 27", payload["content"])
        self.assertEqual(payload["embed"].description, "Гонка")
        self.assertIsNone(self.edit_summary(payload["embed"]))
        self.assertEqual(self.saved(), self.original)
        self.assertEqual(self.audit_edits(), [])

        click = self.interaction()
        await self.confirm(payload, click)
        click.guild.fetch_member.assert_awaited_once_with(int(ADDED))
        saved = self.saved()
        for key in ("id", "request_id", "guild_id", "organizer_id", "channel_id", "message_id", "created_at"):
            self.assertEqual(saved[key], self.original[key])
        self.assertEqual(saved["members"], [SECOND, ADDED])
        self.assertEqual((saved["note"], saved["type_name"], saved["occurred_at"]),
                         ("", "Гонка", parse_event_date("2026-01-04 13:00")))
        self.assertEqual((saved["points"], saved["win_bonus"], saved["mvp_bonus"]), (12, 0, 3))
        self.assertEqual([(row["user_id"], row["points"]) for row in self.store.statistics(GUILD, PERIOD)],
                         [(ADDED, 15), (SECOND, 12)])
        self.assertEqual(len(self.audit_edits()), 1)
        entry = self.audit_edits()[0]
        self.assertEqual(entry["actor_id"], ADMIN)
        details = json.loads(entry["details"])
        self.assertEqual(details["before"]["members"], [FIRST, SECOND])
        self.assertEqual(details["after"]["members"], [SECOND, ADDED])
        self.message.edit.assert_awaited_once()
        card_payload = self.message.edit.await_args.kwargs
        self.assertEqual(card_payload["embed"], report_embed(saved))
        self.assertEqual(card_payload["allowed_mentions"].to_dict(), {"parse": []})
        self.assertEqual(self.edit_summary(card_payload["embed"]).splitlines(), [
            f"Добавлен участник <@{ADDED}>",
            f"Удалён участник <@{FIRST}>",
            "Вид: Капт → Гонка",
            "Дата (Москва): 03.01.2026 12:00 → 04.01.2026 13:00",
            "Итог: Победа → Без победы",
            "Базовые очки: 10 → 12",
            "Бонус победы: 4 → 0",
            f"MVP: <@{FIRST}> → <@{ADDED}>",
            "Бонус MVP: 5 → 3",
            "Примечание удалено",
        ])
        with self.assertRaisesRegex(UserError, "уже завершено"):
            await self.confirm(payload)
        self.assertEqual(len(self.audit_edits()), 1)

    async def test_preview_hides_previous_edit_and_public_card_shows_only_latest(self):
        self.assertIsNone(self.edit_summary(report_embed(self.original)))
        await self.confirm(await self.preview(self.interaction(), note="Первая правка"))
        self.assertEqual(self.edit_summary(report_embed(self.saved())), "Примечание изменено")
        preview = await self.preview(self.interaction(), points=11)
        self.assertIsNone(self.edit_summary(preview["embed"]))
        await self.confirm(preview)
        self.assertEqual(self.edit_summary(report_embed(self.saved())), "Базовые очки: 10 → 11")

    async def test_summary_caps_only_long_rosters_and_escapes_type_names(self):
        old_members = [str(10000000000000000000 + member) for member in range(100)]
        new_members = [str(20000000000000000000 + member) for member in range(100)]
        kind = self.store.set_type(GUILD, ADMIN, "**" * 40, 2)
        original = self.store.create_report(Draft(
            GUILD, ORGANIZER, CHANNEL, kind["id"], kind["name"], 2, parse_event_date(DATE),
            members=old_members,
        ))
        updated = dict(original, members=new_members, type_name="[x](https://x) [y](https://y)\n" + f"<@{ADMIN}>" + "*" * 22,
                       occurred_at=parse_event_date("2026-01-04 13:00"), note="*" * 300, outcome="win",
                       points=2_000_000, win_bonus=1_000_000, mvp_id=new_members[0], mvp_bonus=1_000_000)
        saved = self.store.edit_report(GUILD, ADMIN, original["id"], original, updated)
        cancelled = self.store.cancel_report(GUILD, ADMIN, saved["id"], "*" * 300)
        embed = report_embed(cancelled)
        summary = self.edit_summary(embed)
        lines = summary.splitlines()
        self.assertTrue(lines[0].startswith("Добавлены участники (100): "))
        self.assertTrue(lines[1].startswith("Удалены участники (100): "))
        for line, roster in zip(lines[:2], (new_members, old_members)):
            self.assertEqual([f"<@{member}>" for member in roster[:8]], [
                part.removesuffix(" и ещё 92") for part in line.split(": ", 1)[1].split(", ")
            ])
        self.assertIn(r"\[x\]\(https://x\) \[y\]\(https://y\)", lines[2])
        self.assertNotIn(f"<@{ADMIN}>", lines[2])
        self.assertTrue(all(line.startswith(label) for line, label in zip(lines[2:], (
            "Вид", "Дата", "Итог", "Базовые очки", "Бонус победы", "MVP", "Бонус MVP", "Примечание добавлено",
        ), strict=True)))
        self.assertLessEqual(len(summary.encode("utf-16-le")) // 2, 1024)
        self.assertLessEqual(len(embed), 6000)
        self.assertIn("…", lines[2])

    async def test_short_roster_change_shows_all_members_and_barn_has_no_marker(self):
        fourth = "888888888888888888"
        updated = dict(self.original, members=[ADDED, fourth], mvp_id=None, mvp_bonus=0)
        saved = self.store.edit_report(GUILD, ADMIN, self.number, self.original, updated)
        self.assertEqual(self.edit_summary(report_embed(saved)).splitlines()[:2], [
            f"Добавлены участники (2): <@{ADDED}>, <@{fourth}>",
            f"Удалены участники (2): <@{FIRST}>, <@{SECOND}>",
        ])
        self.store.configure_barn(GUILD, ADMIN, 3)
        barn = self.store.create_barn_collection(GUILD, ADMIN, FIRST, CHANNEL, "barn-2", 200)
        self.assertIsNone(self.edit_summary(report_embed(barn)))

    async def test_restart_recovery_and_cancellation_use_most_recent_edit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reports.sqlite"
            with closing(Store(path)) as store:
                kind = store.set_type(GUILD, ADMIN, "Капт", 3)
                original = store.create_report(Draft(
                    GUILD, ORGANIZER, CHANNEL, kind["id"], kind["name"], 3,
                    parse_event_date(DATE), members=[FIRST],
                ))
                number = original["id"]
                first = store.edit_report(GUILD, ADMIN, number, original, dict(original, note="Первая правка"))
                store.edit_report(GUILD, ADMIN, number, first, dict(first, members=[FIRST, ADDED]))
                store.attach_message(GUILD, number, "810")
                store.audit(CHANNEL, ADMIN, "report.edited", {"id": number, "before": first, "after": original})
                # Старый файл может уже содержать правки и некорректные строки журнала.
                store.db.execute("DROP INDEX report_edit_audit_latest")
                store.db.execute("INSERT INTO audit_log(guild_id,actor_id,action,details,created_at) VALUES(?,?,?,?,?)",
                                 (GUILD, ADMIN, "report.edited", "{", first["created_at"]))

            with closing(Store(path)) as store:
                current = store.report(GUILD, number)
                summary = f"Добавлен участник <@{ADDED}>"
                self.assertEqual(self.edit_summary(report_embed(current)), summary)
                service = EventService(store)
                service.channel = AsyncMock(return_value=self.channel)
                self.channel.fetch_message = AsyncMock(return_value=self.message)
                health = HealthService(SimpleNamespace(events=service), store, int(GUILD))
                guild = self.interaction().guild
                await health.recover_report(guild, number)
                self.assertEqual(self.edit_summary(self.message.edit.await_args.kwargs["embed"]), summary)
                cancelled = store.cancel_report(GUILD, ADMIN, number, "Причина")
                self.assertTrue(await service.update_cancelled(guild, cancelled))
                card = self.message.edit.await_args.kwargs
                self.assertIn("отменено", card["embed"].title)
                self.assertEqual(self.edit_summary(card["embed"]), summary)
                self.assertEqual(card["allowed_mentions"].to_dict(), {"parse": []})

    async def test_authority_and_optimistic_concurrency(self):
        with self.assertRaisesRegex(UserError, "администратору"):
            await self.preview(self.interaction(administrator=False), note="Менеджер")
        with self.assertRaisesRegex(UserError, "не найден"):
            await self.preview(self.interaction(guild_id=CHANNEL), note="Другой сервер")
        first = await self.preview(self.interaction(), note="Первая правка")
        second = await self.preview(self.interaction(), points=15)
        for click in (self.interaction(actor=SECOND), self.interaction(guild_id=CHANNEL),
                      self.interaction(administrator=False)):
            with self.assertRaises(UserError):
                await self.confirm(first, click)
        self.assertEqual(self.saved(), self.original)
        await self.confirm(first)
        with self.assertRaisesRegex(UserError, "изменился после предпросмотра"):
            await self.confirm(second)
        self.assertEqual(self.saved()["note"], "Первая правка")
        self.assertEqual(self.saved()["points"], self.original["points"])
        self.assertEqual(len(self.audit_edits()), 1)

    async def test_cancellation_and_barn_reject_edits(self):
        payload = await self.preview(self.interaction(), note="Правка")
        self.store.cancel_report(GUILD, ADMIN, self.number, "Причина")
        with self.assertRaisesRegex(UserError, "Отменённый"):
            await self.confirm(payload)
        self.assertEqual(self.saved()["note"], "Заметка")
        self.store.configure_barn(GUILD, ADMIN, 3)
        barn = self.store.create_barn_collection(GUILD, ADMIN, FIRST, CHANNEL, "barn-1", 100)
        with self.assertRaisesRegex(UserError, "сбор"):
            await self.commands.edit_report.callback(self.commands, self.interaction(), barn["id"], points=2)
        self.assertEqual(self.audit_edits(), [])

    async def test_mvp_roster_and_bonus_constraints(self):
        for changes in (
            {"remove": f"{FIRST},{SECOND}"},
            {"remove": ADDED},
            {"mvp": SimpleNamespace(id=int(ADDED), bot=False)},
            {"add": SimpleNamespace(id=int(ADDED), bot=True)},
            {"outcome": "no_win", "win_bonus": 1},
            {"clear_mvp": True, "mvp_bonus": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(UserError):
                await self.preview(self.interaction(), **changes)
        payload = await self.preview(self.interaction(), remove=FIRST)
        await self.confirm(payload)
        report = self.saved()
        self.assertIsNone(report["mvp_id"])
        self.assertEqual((report["mvp_bonus"], report["points"], report["win_bonus"]), (0, 14, 4))
        with self.assertRaisesRegex(UserError, "бонус"):
            await self.preview(self.interaction(), mvp=SimpleNamespace(id=int(SECOND), bot=False))

    async def test_report_and_audit_roll_back_and_health_keeps_previous_gap(self):
        self.assertFalse(self.store.health_snapshot(GUILD)["warnings"])
        updated = dict(self.original, members=[SECOND], mvp_id=None, mvp_bonus=0)
        with patch.object(self.store, "audit", side_effect=RuntimeError("audit failed")), self.assertRaises(RuntimeError):
            self.store.edit_report(GUILD, ADMIN, self.number, self.original, updated)
        self.assertEqual(self.saved(), self.original)
        self.assertEqual(self.store.db.execute("SELECT members FROM health_baseline").fetchone()[0], 2)
        self.store.edit_report(GUILD, ADMIN, self.number, self.original, updated)
        self.assertFalse(self.store.health_snapshot(GUILD)["warnings"])

        current = self.saved()
        self.store.edit_report(GUILD, ADMIN, self.number, current, dict(current, members=[SECOND, ADDED]))
        self.store.health_snapshot(GUILD)
        self.store.db.execute("UPDATE health_baseline SET members=3")
        current = self.saved()
        self.store.edit_report(GUILD, ADMIN, self.number, current, dict(current, members=[SECOND]))
        self.assertEqual(self.store.db.execute("SELECT members FROM health_baseline").fetchone()[0], 2)
        self.assertTrue(self.store.health_snapshot(GUILD)["warnings"])

    async def test_roster_reduction_does_not_lower_previously_observed_baseline(self):
        self.store.health_snapshot(GUILD)
        self.store.edit_report(GUILD, ADMIN, self.number, self.original,
                               dict(self.original, members=[FIRST, SECOND, ADDED, FOURTH]))
        grown = self.saved()
        self.store.edit_report(GUILD, ADMIN, self.number, grown, dict(grown, members=[FIRST, SECOND, ADDED]))
        self.assertEqual(self.store.db.execute("SELECT members FROM health_baseline").fetchone()[0], 2)
        self.store.db.execute("DELETE FROM report_members WHERE report_id=? AND user_id<>?", (self.number, FIRST))
        self.assertTrue(self.store.health_snapshot(GUILD)["warnings"])

    async def test_edit_requires_new_cancellation_preview_when_awards_change(self):
        cancel_request = self.interaction()
        await self.commands.cancel_report.callback(self.commands, cancel_request, self.number, "Причина")
        cancellation = cancel_request.edit_original_response.call_args.kwargs["view"]
        self.addCleanup(cancellation.stop)
        await self.confirm(await self.preview(self.interaction(), points=30))
        with self.assertRaisesRegex(UserError, "начисления отчёта изменились"):
            await cancellation.confirm.callback(self.interaction())
        self.assertIsNone(self.saved()["cancelled_at"])
        self.assertEqual(self.saved()["points"], 34)

    async def test_discord_timeout_after_save_keeps_edit_and_provides_recovery(self):
        payload = await self.preview(self.interaction(), points=20)
        self.message.edit.side_effect = TimeoutError("lost response")
        click = self.interaction()
        with self.assertLogs("lafamily.errors", level="ERROR"):
            await self.confirm(payload, click)
        self.assertEqual(self.saved()["points"], 24)
        self.assertIn("/бот восстановить-операцию", click.edit_original_response.call_args.kwargs["content"])

    async def test_recovery_during_first_send_cannot_create_second_card(self):
        started = asyncio.Event()
        finish_send = asyncio.Event()
        sent = SimpleNamespace(id=999, edit=AsyncMock())

        async def send(**kwargs):
            started.set()
            await finish_send.wait()
            return sent

        async def history(**kwargs):
            for message in ():
                yield message

        self.channel.send = AsyncMock(side_effect=send)
        self.channel.history = history
        self.channel.permissions_for = lambda member: SimpleNamespace(read_message_history=True)
        request = self.interaction()
        request.guild.me = SimpleNamespace(id=123)
        health = HealthService(SimpleNamespace(events=self.service, user=request.guild.me), self.store, int(GUILD))
        kind = self.store.set_type(GUILD, ADMIN, "Заезд", 9)
        draft = Draft(GUILD, ORGANIZER, CHANNEL, kind["id"], "Заезд", 9, parse_event_date(DATE), members=[FIRST])
        view = self.service.new_view(draft)
        confirm_task = asyncio.create_task(self.service.confirm(request, view))
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.number = self.store.db.execute("SELECT id FROM reports WHERE type_name='Заезд'").fetchone()[0]
            await self.confirm(await self.preview(self.interaction(), points=20))
            with self.assertRaisesRegex(UserError, "публикуется"):
                await health.recover_report(request.guild, self.number)
            self.channel.send.assert_awaited_once()
        finally:
            finish_send.set()
            await confirm_task

        self.assertEqual(self.saved()["message_id"], "999")
        self.assertEqual(sent.edit.await_args.kwargs["embed"], report_embed(self.saved()))

    async def test_definite_discord_refusal_leaves_publication_retryable(self):
        self.assertTrue(self.store.forget_report_message(GUILD, self.number, "810"))
        denied = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no access")
        self.channel.send = AsyncMock(side_effect=[denied, SimpleNamespace(id=999)])
        with self.assertRaises(discord.Forbidden):
            await publish_report_message(self.store, self.channel, self.saved())
        self.assertIsNone(self.saved()["message_id"])
        self.assertTrue(await publish_report_message(self.store, self.channel, self.saved()))
        self.assertEqual(self.saved()["message_id"], "999")
        self.assertFalse(await publish_report_message(self.store, self.channel, self.saved()))
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_recovery_resynchronizes_edits_made_during_network_update(self):
        self.channel.fetch_message = AsyncMock(return_value=self.message)
        guild = self.interaction().guild
        async def edit(**kwargs):
            if self.message.edit.await_count == 1:
                current = self.saved()
                self.store.edit_report(GUILD, ADMIN, self.number, current, dict(current, note="Во время восстановления"))

        self.message.edit.side_effect = edit
        health = HealthService(SimpleNamespace(events=self.service), self.store, int(GUILD))
        await health.recover_report(guild, self.number)
        self.assertEqual(self.message.edit.await_count, 2)
        self.assertEqual(self.message.edit.await_args.kwargs["embed"], report_embed(self.saved()))
