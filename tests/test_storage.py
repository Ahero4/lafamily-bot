import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from backup import create_backup
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.time import parse_event_date, week_range

GUILD = "111111111111111111"
ACTOR = "222222222222222222"
MEMBER = "333333333333333333"
OTHER = "444444444444444444"
CHANNEL = "555555555555555555"
NOW = datetime(2026, 10, 25, 10, tzinfo=UTC)
PERIOD = week_range(now=NOW)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.set_type(GUILD, ACTOR, "Тестовое МП", 10)
        kind = self.store.types(GUILD)[0]
        self.draft = Draft(GUILD, ACTOR, CHANNEL, kind["id"], kind["name"], 10,
                           "2026-10-24T17:00:00.000Z", members=[MEMBER, OTHER])

    def test_duplicate_confirmation_and_members_do_not_duplicate_points(self):
        self.draft.members.append(MEMBER)
        report = self.store.create_report(self.draft)
        self.assertEqual(report["id"], self.store.create_report(self.draft)["id"])
        self.assertEqual(len(report["members"]), 2)
        rows = self.store.statistics(GUILD, PERIOD)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["points"] == 10 and row["events"] == 1 for row in rows))

    def test_changed_rate_does_not_change_old_report(self):
        self.store.create_report(self.draft)
        self.store.set_type(GUILD, ACTOR, self.draft.type_name, 35)
        self.store.create_report(replace(self.draft, request_id="new-draft", points=35))
        self.assertEqual(self.store.statistics(GUILD, PERIOD, MEMBER)[0]["points"], 45)

    def test_cancel_removes_points_but_keeps_history(self):
        report = self.store.create_report(self.draft)
        self.store.cancel_report(GUILD, ACTOR, report["id"], "Неверный состав")
        self.assertEqual(self.store.statistics(GUILD, PERIOD), [])
        saved = self.store.report(GUILD, report["id"])
        self.assertEqual(len(saved["members"]), 2)
        self.assertEqual(saved["cancel_reason"], "Неверный состав")
        repeated = self.store.cancel_report(GUILD, ACTOR, report["id"], "Повтор")
        self.assertEqual(repeated["cancel_reason"], "Неверный состав")
        count = self.store.db.execute("SELECT COUNT(*) FROM audit_log WHERE action='report.cancelled'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_disabled_type_preserves_history_and_blocks_drafts(self):
        self.store.create_report(self.draft)
        self.store.disable_type(GUILD, ACTOR, self.draft.type_id)
        with self.assertRaises(UserError):
            self.store.create_report(replace(self.draft, request_id="new-draft"))
        self.assertEqual(self.store.types(GUILD), [])
        self.assertEqual(self.store.statistics(GUILD, PERIOD, MEMBER)[0]["points"], 10)

    def test_empty_and_invalid_members_are_rejected(self):
        for members in ([], ["invalid"], [str(100000000000000000 + i) for i in range(101)]):
            with self.subTest(members=len(members)), self.assertRaises(UserError):
                self.store.create_report(replace(self.draft, members=members))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 0)

    def test_failure_after_inserts_rolls_back_whole_transaction(self):
        # Сбой журнала возникает уже после вставки отчёта и состава.
        with patch.object(self.store, "audit", side_effect=sqlite3.OperationalError("test")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.create_report(self.draft)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM report_members").fetchone()[0], 0)
        self.assertIsNotNone(self.store.create_report(self.draft))

    def test_guilds_are_isolated(self):
        report = self.store.create_report(self.draft)
        foreign = "999999999999999999"
        with self.assertRaises(UserError):
            self.store.report(foreign, report["id"])
        with self.assertRaises(UserError):
            self.store.cancel_report(foreign, ACTOR, report["id"], "Нет доступа")
        self.assertEqual(self.store.statistics(foreign, PERIOD), [])
        self.assertIsNone(self.store.event_type(foreign, self.draft.type_id))

    def test_moscow_week_boundary(self):
        self.store.create_report(replace(self.draft, occurred_at="2026-10-25T20:59:59.000Z"))
        self.store.create_report(replace(self.draft, request_id="next", occurred_at="2026-10-25T21:00:00.000Z"))
        self.assertEqual(PERIOD[1], "2026-10-25T21:00:00.000Z")
        next_monday = datetime(2026, 10, 25, 21, tzinfo=UTC)
        self.assertEqual(self.store.statistics(GUILD, PERIOD, MEMBER)[0]["events"], 1)
        self.assertEqual(self.store.statistics(GUILD, week_range(now=next_monday), MEMBER)[0]["events"], 1)
        self.assertEqual(week_range(-1, next_monday), PERIOD)

    def test_points_and_blank_names_are_validated(self):
        for points in (-1, 1000001, 1.5, True):
            with self.subTest(points=points), self.assertRaises(UserError):
                self.store.set_type(GUILD, ACTOR, "МП", points)
        with self.assertRaises(UserError):
            self.store.set_type(GUILD, ACTOR, "   ", 1)


class TimeTests(unittest.TestCase):
    def test_input_is_moscow_and_default_is_now(self):
        self.assertEqual(parse_event_date("2026-10-25 12:00", NOW), "2026-10-25T09:00:00.000Z")
        self.assertEqual(parse_event_date(None, NOW), "2026-10-25T10:00:00.000Z")

    def test_invalid_calendar_format_and_future(self):
        for value in ("2026-02-30 12:00", "2026-10-26 12:00", "25.10.2026", "2026-10-25 24:00"):
            with self.subTest(value=value), self.assertRaises(UserError):
                parse_event_date(value, NOW)


class FileTests(unittest.TestCase):
    def test_restart_and_backup_of_live_wal_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "lafamily.sqlite"
            store = Store(path)
            try:
                store.configure(GUILD, ACTOR, CHANNEL, ACTOR, OTHER)
                store.set_type(GUILD, ACTOR, "МП", 7)
                kind = store.types(GUILD)[0]
                draft = Draft(GUILD, ACTOR, CHANNEL, kind["id"], kind["name"], 7, PERIOD[0], members=[MEMBER])
                store.create_report(draft)
                target = create_backup(path, directory / "backups")
            finally:
                store.close()
            for saved in (path, target):
                reopened = Store(saved)
                try:
                    self.assertEqual(reopened.statistics(GUILD, PERIOD, MEMBER)[0]["points"], 7)
                    self.assertEqual(reopened.settings(GUILD)["manager_role_id"], OTHER)
                finally:
                    reopened.close()

    def test_backup_does_not_create_missing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.sqlite"
            with self.assertRaises(UserError):
                create_backup(path, Path(temporary) / "backups")
            self.assertFalse(path.exists())

    def test_legacy_js_timestamps_and_discord_ids_remain_readable(self):
        # Запись в формате прежней версии (ISO UTC с миллисекундами, ID строками).
        store = Store(":memory:")
        try:
            store.db.execute("""INSERT INTO reports
                (guild_id, request_id, organizer_id, type_name, points, occurred_at, note, created_at, channel_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (GUILD, "legacy", ACTOR, "Старое МП", 17, "2026-10-24T17:00:00.000Z", "", "2026-10-24T18:00:00.000Z", CHANNEL))
            store.db.execute("INSERT INTO report_members VALUES (1, ?)", (MEMBER,))
            self.assertEqual(store.statistics(GUILD, PERIOD, MEMBER)[0]["points"], 17)
            self.assertEqual(store.report(GUILD, 1)["members"], [MEMBER])
            store.cancel_report(GUILD, ACTOR, 1, "Проверка совместимости")
            self.assertEqual(store.statistics(GUILD, PERIOD), [])
        finally:
            store.close()
