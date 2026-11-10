"""Регрессии по результатам аудита: сетевые сбои, очередь SQLite и старые кнопки."""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from lafamily.async_database import DatabaseWorker
from lafamily.database import Store
from lafamily.diagnostics import error_trace, log_error
from lafamily.errors import UserError
from lafamily.modules.events.commands import EventCommands
from lafamily.modules.events.models import Draft
from lafamily.modules.events.service import EventService
from lafamily.modules.events.time import parse_event_date

GUILD = "111111111111111111"
ACTOR = "222222222222222222"
MEMBER = "333333333333333333"
OTHER = "444444444444444444"
CHANNEL = "555555555555555555"


def request(role="organizer"):
    return SimpleNamespace(
        guild_id=int(GUILD),
        guild=SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(bot=False))),
        user=SimpleNamespace(id=int(ACTOR), roles=[SimpleNamespace(id=role)]),
        permissions=discord.Permissions(),
        response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
    )


def participant(member_id):
    member = MagicMock(spec=discord.Member)
    member.id, member.bot = int(member_id), False
    return member


class DraftFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.configure(GUILD, ACTOR, CHANNEL, "organizer", "manager")
        self.store.set_type(GUILD, ACTOR, "МП", 10)
        kind = self.store.types(GUILD)[0]
        self.draft = Draft(GUILD, ACTOR, CHANNEL, kind["id"], kind["name"], 10, parse_event_date(None), members=[MEMBER])
        self.service = EventService(self.store)
        self.addCleanup(self.service.close)
        self.view = self.service.new_view(self.draft)

    async def test_failed_add_keeps_shown_composition_and_requires_refresh(self):
        failed = request()
        failed.edit_original_response.side_effect = RuntimeError("simulated network failure")
        with self.assertRaises(RuntimeError):
            await self.view.dispatch("add", failed, [participant(OTHER)])
        self.assertEqual(self.draft.members, [MEMBER])
        self.assertTrue(self.view.needs_refresh)
        self.service.channel = AsyncMock(return_value=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=666666666666666666))))
        # Первый клик только восстанавливает показанный состав. Начисления ещё нет.
        await self.view.dispatch("confirm", request())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 0)
        self.assertFalse(self.view.needs_refresh)
        await self.view.dispatch("confirm", request())
        self.assertEqual(self.store.report(GUILD, 1)["members"], [MEMBER])

    async def test_failed_clear_does_not_erase_composition(self):
        failed = request()
        failed.edit_original_response.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            await self.view.dispatch("clear", failed)
        self.assertEqual(self.draft.members, [MEMBER])
        self.assertTrue(self.view.needs_refresh)
        # Неудачное повторное отображение также не разрешает подтверждение.
        with self.assertRaises(RuntimeError):
            await self.view.dispatch("confirm", failed)
        self.assertTrue(self.view.needs_refresh)

    async def test_expiry_removes_controls_and_explains_reason(self):
        original = request()
        self.view.bind(original)
        await self.view.on_timeout()
        self.assertTrue(self.view.finished)
        self.assertTrue(all(item.disabled for item in self.view.children))
        payload = original.edit_original_response.call_args.kwargs
        self.assertIsNone(payload["view"])
        self.assertIn("истёк", payload["content"])
        self.assertEqual(self.service.views, {})

    async def test_replacement_retires_previous_message(self):
        original = request()
        self.view.bind(original)
        replacement = await self.service.new_view_async(replace(self.draft, request_id="replacement"))
        self.assertTrue(self.view.finished)
        self.assertFalse(replacement.finished)
        self.assertIn("заменён", original.edit_original_response.call_args.kwargs["content"])

    async def test_slow_retirement_cannot_replace_a_newer_draft(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def edit_old(**kwargs):
            started.set()
            await release.wait()
        self.view.original = SimpleNamespace(edit_original_response=edit_old)
        first = asyncio.create_task(self.service.new_view_async(replace(self.draft,request_id='first')))
        await started.wait()
        try:
            latest = await self.service.new_view_async(replace(self.draft,request_id='latest'))
        finally:
            release.set()
        with self.assertRaises(UserError):
            await first
        self.assertIs(self.service.views[(GUILD, ACTOR)], latest)
        self.assertFalse(latest.finished)

    async def test_timeout_waits_for_current_confirmation(self):
        await self.view.lock.acquire()
        expiry = asyncio.create_task(self.view.on_timeout())
        await asyncio.sleep(0)
        self.assertFalse(self.view.finished)
        self.view.lock.release()
        await expiry
        self.assertTrue(self.view.finished)

    async def test_cancel_retries_publication_without_second_audit_or_reason_change(self):
        report = self.store.create_report(self.draft)
        self.service.update_cancelled = AsyncMock(side_effect=[False, True])
        command = EventCommands(self.service)
        first, retry = request("manager"), request("manager")
        await command.cancel_report.callback(command, first, report["id"], "Исходная причина")
        confirmation=first.edit_original_response.call_args.kwargs['view']
        self.assertIsNone(self.store.report(GUILD,report['id'])['cancelled_at'])
        await confirmation.confirm.callback(first)
        self.assertIn("Повторите", first.edit_original_response.call_args.kwargs["content"])
        await command.cancel_report.callback(command, retry, report["id"], "Новая причина")
        self.assertEqual(self.service.update_cancelled.await_count, 2)
        self.assertEqual(self.store.report(GUILD, report["id"])["cancel_reason"], "Исходная причина")
        count = self.store.db.execute("SELECT COUNT(*) FROM audit_log WHERE action='report.cancelled'").fetchone()[0]
        self.assertEqual(count, 1)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_lock_does_not_block_ack_or_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite"
            async with DatabaseWorker(path) as store:
                await store.call("configure", GUILD, ACTOR, CHANNEL, "organizer", "manager")
                writer = sqlite3.connect(path, isolation_level=None)
                writer.execute("BEGIN IMMEDIATE")
                service = EventService(store)
                command = EventCommands(service)
                interaction = request("manager")
                operation = asyncio.create_task(command.add_type.callback(command, interaction, "МП", 10))
                started = time.monotonic()
                try:
                    await asyncio.sleep(0.08)
                    self.assertLess(time.monotonic() - started, 0.5)
                    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                    self.assertFalse(operation.done())
                finally:
                    writer.execute("ROLLBACK")
                    writer.close()
                    await operation
                    service.close()
                self.assertEqual((await store.call("types", GUILD))[0]["points"], 10)

    async def test_worker_serializes_parallel_transactions_and_remains_usable_after_error(self):
        async with DatabaseWorker(":memory:") as store:
            await asyncio.gather(*(store.call("set_type", GUILD, ACTOR, f"МП {i}", i) for i in range(10)))
            with self.assertRaises(Exception):
                await store.call("set_type", GUILD, ACTOR, "Ошибка", -1)
            self.assertEqual(len(await store.call("types", GUILD)), 10)

    async def test_real_worker_supports_report_confirmation(self):
        async with DatabaseWorker(":memory:") as store:
            await store.call("configure", GUILD, ACTOR, CHANNEL, "organizer", "manager")
            await store.call("set_type", GUILD, ACTOR, "МП", 10)
            kind = (await store.call("types", GUILD))[0]
            service = EventService(store)
            try:
                draft = Draft(GUILD, ACTOR, CHANNEL, kind["id"], "МП", 10, parse_event_date(None), members=[MEMBER])
                view = service.new_view(draft)
                service.channel = AsyncMock(return_value=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=666666666666666666))))
                await view.dispatch("confirm", request())
                self.assertEqual((await store.call("report", GUILD, 1))["members"], [MEMBER])
            finally:
                service.close()


class DiagnosticsTests(unittest.TestCase):
    def test_trace_contains_frames_and_chain_but_no_secret_values_or_exception_messages(self):
        secret = "secret-token-do-not-log"
        try:
            try:
                raise ValueError(secret)
            except ValueError as cause:
                raise RuntimeError("private request " + secret) from cause
        except RuntimeError as error:
            trace = error_trace(error)
            self.assertIn("RuntimeError", trace)
            self.assertIn("ValueError", trace)
            self.assertIn("test_trace_contains_frames", trace)
            self.assertNotIn(secret, trace)
            self.assertNotIn("private request", trace)
            with self.assertLogs("lafamily.errors", level="ERROR") as captured:
                log_error("Ошибка проверки", error)
            self.assertIn("test_resilience.py:", captured.output[0])
            self.assertNotIn(secret, captured.output[0])
