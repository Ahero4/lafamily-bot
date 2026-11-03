"""Хранение в SQLite: общая база семьи, история МП и журнал важных изменений."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.time import timestamp
from lafamily.price_delivery_store import PriceDeliveryStore
from lafamily.backup_store import BackupStore
from lafamily.channel_store import ChannelStore
from lafamily.onboarding_store import OnboardingStore


class Store(PriceDeliveryStore, BackupStore, ChannelStore, OnboardingStore):
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # Схема сохранена от JS-версии. ID Discord остаются строками без потери точности.
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS release_deliveries (
                guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, version TEXT NOT NULL,
                state TEXT NOT NULL, message_id TEXT,
                PRIMARY KEY(guild_id, channel_id, version)
            );
            CREATE TABLE IF NOT EXISTS release_settings (
                guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS release_publications (
                guild_id TEXT NOT NULL, version TEXT NOT NULL, state TEXT NOT NULL,
                message_id TEXT, PRIMARY KEY(guild_id, version)
            );
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;
            PRAGMA busy_timeout = 5000;
            CREATE TABLE IF NOT EXISTS announcement_settings(guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS announcement_templates(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, name TEXT NOT NULL,
                channel_id TEXT NOT NULL, role_id TEXT NOT NULL, body TEXT NOT NULL, image_ref TEXT,
                UNIQUE(guild_id,name));
            CREATE TABLE IF NOT EXISTS announcement_schedules(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
                template_id INTEGER NOT NULL REFERENCES announcement_templates(id), kind TEXT NOT NULL,
                starts_at TEXT, days TEXT, clock TEXT, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS announcement_attempts(
                schedule_id INTEGER NOT NULL REFERENCES announcement_schedules(id), starts_at TEXT NOT NULL,
                state TEXT NOT NULL, message_id TEXT, PRIMARY KEY(schedule_id,starts_at));
            CREATE TABLE IF NOT EXISTS settings (
                guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                organizer_role_id TEXT NOT NULL, manager_role_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_types (
                id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, name TEXT NOT NULL,
                points INTEGER NOT NULL CHECK(points BETWEEN 0 AND 1000000),
                active INTEGER NOT NULL DEFAULT 1, UNIQUE(guild_id, name)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                organizer_id TEXT NOT NULL, type_name TEXT NOT NULL, points INTEGER NOT NULL,
                occurred_at TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
                cancelled_at TEXT, cancel_reason TEXT, channel_id TEXT NOT NULL, message_id TEXT
            );
            CREATE TABLE IF NOT EXISTS report_members (
                report_id INTEGER NOT NULL REFERENCES reports(id), user_id TEXT NOT NULL,
                PRIMARY KEY(report_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS report_week ON reports(guild_id, occurred_at);
            CREATE TABLE IF NOT EXISTS onboarding_settings (guild_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, review_id TEXT NOT NULL, role_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS onboarding_requests (message_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, user_id TEXT NOT NULL, role_id TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS barn_settings (
                guild_id TEXT PRIMARY KEY, points INTEGER NOT NULL CHECK(points BETWEEN 0 AND 1000000)
            );
            CREATE TABLE IF NOT EXISTS barn_collections (
                report_id INTEGER PRIMARY KEY REFERENCES reports(id), amount INTEGER NOT NULL CHECK(amount > 0)
            );
            CREATE TABLE IF NOT EXISTS price_settings (
                guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS price_subscriptions (
                guild_id TEXT NOT NULL, user_id TEXT NOT NULL, item TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id, user_id, item)
            );
            CREATE TABLE IF NOT EXISTS price_alert_cursor (
                guild_id TEXT PRIMARY KEY, source_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS price_publications (
                source_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                result_id TEXT, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'published', 'uncertain'))
            );
            CREATE TABLE IF NOT EXISTS settings_roles (
                guild_id TEXT NOT NULL REFERENCES settings(guild_id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('organizer', 'manager')),
                role_id TEXT NOT NULL,
                PRIMARY KEY(guild_id, kind, role_id)
            );
            -- Переносим ранее назначенные одиночные роли, не удаляя отчёты и настройки.
            INSERT OR IGNORE INTO settings_roles SELECT guild_id, 'organizer', organizer_role_id FROM settings;
            INSERT OR IGNORE INTO settings_roles SELECT guild_id, 'manager', manager_role_id FROM settings;
        """)
        self.init_database_backups()
        self.init_price_delivery()
        self.init_onboarding()
        self.migrate_event_bonuses()
        # Старые подписки сохраняют прежний режим и защиту от повторных тегов.
        if 'threshold' not in {r['name'] for r in self.db.execute('PRAGMA table_info(price_subscriptions)')}:
            self.db.execute("ALTER TABLE price_subscriptions ADD COLUMN threshold TEXT NOT NULL DEFAULT 'max'")

    def migrate_event_bonuses(self):
        # Добавляем только отсутствующие колонки; старые начисления остаются прежними.
        columns = {
            "event_types": {"win_enabled": "INTEGER NOT NULL DEFAULT 0", "win_bonus": "INTEGER NOT NULL DEFAULT 0", "mvp_bonus": "INTEGER NOT NULL DEFAULT 0"},
            "reports": {"outcome": "TEXT NOT NULL DEFAULT ''", "win_bonus": "INTEGER NOT NULL DEFAULT 0", "mvp_id": "TEXT", "mvp_bonus": "INTEGER NOT NULL DEFAULT 0"},
        }
        with self.transaction():
            for table, additions in columns.items():
                existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def configure_event_bonuses(self, guild_id, actor_id, type_id, win_enabled, win_bonus, mvp_bonus):
        self.validate_points(win_bonus)
        self.validate_points(mvp_bonus)
        with self.transaction():
            if not self.event_type(guild_id, type_id):
                raise UserError("Активный вид МП не найден.")
            self.db.execute("UPDATE event_types SET win_enabled=?, win_bonus=?, mvp_bonus=? WHERE guild_id=? AND id=?",
                            (int(win_enabled), win_bonus, mvp_bonus, guild_id, type_id))
            self.audit(guild_id, actor_id, "type.bonuses.updated", {"id": type_id, "win_enabled": win_enabled, "win_bonus": win_bonus, "mvp_bonus": mvp_bonus})

    @contextmanager
    def transaction(self):
        """Отчёт, состав и журнал либо сохраняются вместе, либо откатываются целиком."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def audit(self, guild_id: str, actor_id: str, action: str, details: dict):
        # Параметры передаются отдельно от SQL: пользовательский ввод не становится запросом.
        self.db.execute(
            "INSERT INTO audit_log(guild_id, actor_id, action, details, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, actor_id, action, json.dumps(details, ensure_ascii=False), timestamp()),
        )

    def settings(self, guild_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
        if not row:
            return None
        settings = dict(row)
        for kind in ("organizer", "manager"):
            settings[f"{kind}_role_ids"] = [item["role_id"] for item in self.db.execute(
                "SELECT role_id FROM settings_roles WHERE guild_id=? AND kind=? ORDER BY role_id", (guild_id, kind))]
        return settings

    def configure(self, guild_id: str, actor_id: str, channel_id: str, organizer_role_id: str | list[str], manager_role_id: str | list[str]):
        # Строки поддерживаются для прежних настроек; новая форма передаёт списки.
        organizers = list(dict.fromkeys([organizer_role_id] if isinstance(organizer_role_id, str) else organizer_role_id))
        managers = list(dict.fromkeys([manager_role_id] if isinstance(manager_role_id, str) else manager_role_id))
        if not 1 <= len(organizers) <= 25 or not 1 <= len(managers) <= 25:
            raise UserError("Выберите от 1 до 25 ролей для организаторов и для руководства.")
        if guild_id in organizers or guild_id in managers:
            raise UserError("Нельзя назначить @everyone управляющей ролью.")
        with self.transaction():
            self.ensure_channel_separation(guild_id, channel_id)
            self.db.execute("""INSERT INTO settings VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,
                organizer_role_id=excluded.organizer_role_id, manager_role_id=excluded.manager_role_id""",
                (guild_id, channel_id, organizers[0], managers[0]))
            self.db.execute("DELETE FROM settings_roles WHERE guild_id=?", (guild_id,))
            self.db.executemany("INSERT INTO settings_roles VALUES (?, ?, ?)",
                                [(guild_id, kind, role) for kind, roles in (("organizer", organizers), ("manager", managers)) for role in roles])
            self.audit(guild_id, actor_id, "settings.updated", {
                "channel_id": channel_id, "organizer_role_ids": organizers, "manager_role_ids": managers,
            })

    def set_type(self, guild_id: str, actor_id: str, name: str, points: int | None = None,
                 win_bonus: int | None = None, mvp_bonus: int | None = None):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise UserError("Название МП должно содержать от 1 до 80 символов.")
        for value in (points, win_bonus, mvp_bonus):
            if value is not None:
                self.validate_points(value)
        with self.transaction():
            # Обновляем тот же вид; пропущенные ставки сохраняются, явный 0 отключает бонус.
            previous = self.db.execute("SELECT * FROM event_types WHERE guild_id=? AND name=?",
                                       (guild_id, name)).fetchone()
            if previous is None and points is None:
                raise UserError("Для нового вида МП укажите базовые очки.")
            base = points if points is not None else previous["points"]
            win = win_bonus if win_bonus is not None else (previous["win_bonus"] if previous else 0)
            mvp = mvp_bonus if mvp_bonus is not None else (previous["mvp_bonus"] if previous else 0)
            enabled = int(win > 0) if win_bonus is not None else (previous["win_enabled"] if previous else 0)
            self.db.execute("""INSERT INTO event_types(guild_id, name, points, win_enabled, win_bonus, mvp_bonus)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, name) DO UPDATE SET
                points=excluded.points, win_enabled=excluded.win_enabled,
                win_bonus=excluded.win_bonus, mvp_bonus=excluded.mvp_bonus, active=1""",
                (guild_id, name, base, enabled, win, mvp))
            self.audit(guild_id, actor_id, "type.updated",
                       {"name": name, "points": base, "win_bonus": win, "mvp_bonus": mvp, "win_enabled": enabled})
            return dict(self.db.execute("SELECT * FROM event_types WHERE guild_id=? AND name=?",
                                        (guild_id, name)).fetchone())

    @staticmethod
    def validate_points(points: int):
        if type(points) is not int or not 0 <= points <= 1_000_000:
            raise UserError("Очки: целое число от 0 до 1000000.")

    def types(self, guild_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM event_types WHERE guild_id=? AND active=1 ORDER BY name", (guild_id,))
        return [dict(row) for row in rows]

    def event_type(self, guild_id: str, type_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM event_types WHERE guild_id=? AND id=? AND active=1", (guild_id, type_id)).fetchone()
        return dict(row) if row else None

    def disable_type(self, guild_id: str, actor_id: str, type_id: int):
        with self.transaction():
            result = self.db.execute("UPDATE event_types SET active=0 WHERE guild_id=? AND id=? AND active=1", (guild_id, type_id))
            if not result.rowcount:
                raise UserError("Активный вид МП не найден.")
            self.audit(guild_id, actor_id, "type.disabled", {"id": type_id})

    def create_report(self, draft: Draft) -> dict:
        with self.transaction():
            # Ключ черновика защищает от повторного начисления при повторном подтверждении.
            existing = self.db.execute("SELECT id FROM reports WHERE request_id=? AND guild_id=?", (draft.request_id, draft.guild_id)).fetchone()
            if existing:
                return self.report(draft.guild_id, existing["id"])
            if not self.event_type(draft.guild_id, draft.type_id):
                raise UserError("Этот вид МП отключён. Создайте новый отчёт.")
            members = list(dict.fromkeys(draft.members))
            if not 1 <= len(members) <= 100 or any(not re.fullmatch(r"[0-9]{17,20}", member) for member in members):
                raise UserError("В отчёте должно быть от 1 до 100 участников с корректными Discord ID.")
            self.validate_points(draft.points)
            self.validate_points(draft.win_bonus)
            self.validate_points(draft.mvp_bonus)
            if draft.outcome not in ("", "win", "no_win"):
                raise UserError("Неизвестный итог события.")
            if draft.win_bonus and draft.outcome != "win":
                raise UserError("Бонус победы доступен только при победе.")
            if draft.mvp_id and (draft.mvp_id not in members or draft.mvp_bonus <= 0):
                raise UserError("MVP должен быть участником состава, а награда MVP — настроена.")
            # Ставка берётся из показанного организатору черновика и больше не меняется.
            result = self.db.execute("""INSERT INTO reports
                (guild_id, request_id, organizer_id, type_name, points, occurred_at, note, created_at, channel_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                draft.guild_id, draft.request_id, draft.organizer_id, draft.type_name, draft.points + draft.win_bonus,
                draft.occurred_at, draft.note, timestamp(), draft.channel_id,
            ))
            report_id = result.lastrowid
            self.db.execute("UPDATE reports SET outcome=?, win_bonus=?, mvp_id=?, mvp_bonus=? WHERE id=?",
                            (draft.outcome, draft.win_bonus, draft.mvp_id, draft.mvp_bonus if draft.mvp_id else 0, report_id))
            self.db.executemany("INSERT INTO report_members VALUES (?, ?)", [(report_id, member) for member in members])
            self.audit(draft.guild_id, draft.organizer_id, "report.created", {"id": report_id, "members": members})
            return self.report(draft.guild_id, report_id)

    def report(self, guild_id: str, report_id: int) -> dict:
        row = self.db.execute("SELECT * FROM reports WHERE guild_id=? AND id=?", (guild_id, report_id)).fetchone()
        if row is None:
            raise UserError("Отчёт не найден.")
        report = dict(row)
        report["members"] = [row["user_id"] for row in self.db.execute(
            "SELECT user_id FROM report_members WHERE report_id=? ORDER BY user_id", (report_id,))]
        barn = self.db.execute('SELECT amount FROM barn_collections WHERE report_id=?', (report_id,)).fetchone()
        if barn:
            report['barn_amount'] = barn['amount']
        return report

    def attach_message(self, guild_id: str, report_id: int, message_id: str):
        self.db.execute("UPDATE reports SET message_id=? WHERE guild_id=? AND id=?", (message_id, guild_id, report_id))

    def cancel_report(self, guild_id: str, actor_id: str, report_id: int, reason: str) -> dict:
        if not reason.strip() or len(reason) > 300:
            raise UserError("Укажите причину отмены от 1 до 300 символов.")
        with self.transaction():
            report = self.report(guild_id, report_id)
            if report["cancelled_at"]:
                # Повтор команды восстанавливает публикацию, не меняя причину, очки и журнал.
                return report
            self.db.execute("UPDATE reports SET cancelled_at=?, cancel_reason=? WHERE id=?", (timestamp(), reason, report_id))
            self.audit(guild_id, actor_id, "report.cancelled", {"id": report_id, "reason": reason})
            return self.report(guild_id, report_id)

    def statistics(self, guild_id: str, period: tuple[str, str], user_id: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
        # Нет отдельного изменяемого баланса: статистика всегда согласована с отчётами.
        rows = self.db.execute("""SELECT m.user_id, SUM(r.points + CASE WHEN m.user_id=r.mvp_id THEN r.mvp_bonus ELSE 0 END) AS points, COUNT(*) AS events
            FROM reports r JOIN report_members m ON m.report_id=r.id
            WHERE r.guild_id=? AND r.cancelled_at IS NULL AND r.occurred_at>=? AND r.occurred_at<?
            AND (? IS NULL OR m.user_id=?) GROUP BY m.user_id
            ORDER BY points DESC, events DESC, m.user_id LIMIT ? OFFSET ?""",
            (guild_id, *period, user_id, user_id, limit, offset))
        return [dict(row) for row in rows]

    def points_summary(self, guild_id: str, period: tuple[str, str], limit: int = 20, offset: int = 0) -> list[dict]:
        """Одна строка на участника: очки за всё время и за выбранную неделю."""
        # Период ограничивает только недельную колонку: участники прошлых недель тоже видны.
        # Отменённые отчёты исключаются из обеих колонок, данные других серверов не попадают в результат.
        rows = self.db.execute("""SELECT m.user_id, SUM(r.points + CASE WHEN m.user_id=r.mvp_id THEN r.mvp_bonus ELSE 0 END) AS total_points,
            SUM(CASE WHEN r.occurred_at>=? AND r.occurred_at<? THEN r.points + CASE WHEN m.user_id=r.mvp_id THEN r.mvp_bonus ELSE 0 END ELSE 0 END) AS week_points
            FROM reports r JOIN report_members m ON m.report_id=r.id
            WHERE r.guild_id=? AND r.cancelled_at IS NULL
            GROUP BY m.user_id ORDER BY total_points DESC, week_points DESC, m.user_id
            LIMIT ? OFFSET ?""", (*period, guild_id, limit, offset))
        return [dict(row) for row in rows]

    def configure_barn(self, guild_id: str, actor_id: str, points: int):
        self.validate_points(points)
        with self.transaction():
            self.db.execute("INSERT INTO barn_settings VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET points=excluded.points", (guild_id, points))
            self.audit(guild_id, actor_id, 'barn.settings.updated', {'points': points})

    def create_barn_collection(self, guild_id: str, actor_id: str, member_id: str, channel_id: str, request_id: str, amount: int):
        # Сумма, отчёт и начисление записываются атомарно; повтор события не удваивает баллы.
        if type(amount) is not int or not 1 <= amount <= 1_000_000_000:
            raise UserError('Сумма должна быть от 1 до 1 000 000 000.')
        if not re.fullmatch(r'[0-9]{17,20}', member_id):
            raise UserError('Некорректный участник.')
        with self.transaction():
            existing = self.db.execute('SELECT id FROM reports WHERE guild_id=? AND request_id=?', (guild_id, request_id)).fetchone()
            if existing:
                return self.report(guild_id, existing['id'])
            self.ensure_channel_separation(guild_id, channel_id)
            settings = self.db.execute('SELECT points FROM barn_settings WHERE guild_id=?', (guild_id,)).fetchone()
            if not settings:
                raise UserError('Администратору нужно сначала выполнить /коровник настройки и задать баллы за сбор.')
            now = timestamp()
            result = self.db.execute("""INSERT INTO reports
                (guild_id, request_id, organizer_id, type_name, points, occurred_at, note, created_at, channel_id)
                VALUES (?, ?, ?, 'Коровник — сбор', ?, ?, ?, ?, ?)""",
                (guild_id, request_id, actor_id, settings['points'], now, f'Собрано: ${amount:,}'.replace(',', ' '), now, channel_id))
            report_id = result.lastrowid
            self.db.execute('INSERT INTO report_members VALUES (?, ?)', (report_id, member_id))
            self.db.execute('INSERT INTO barn_collections VALUES (?, ?)', (report_id, amount))
            self.audit(guild_id, actor_id, 'barn.collected', {'id': report_id, 'member_id': member_id, 'amount': amount})
            return self.report(guild_id, report_id)

    def price_settings(self, guild_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM price_settings WHERE guild_id=?", (guild_id,)).fetchone()
        return dict(row) if row else None

    def configure_prices(self, guild_id: str, actor_id: str, channel_id: str):
        with self.transaction():
            self.ensure_channel_separation(guild_id, channel_id, 'prices')
            self.db.execute("""INSERT INTO price_settings VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id""", (guild_id, channel_id))
            self.audit(guild_id, actor_id, "prices.settings.updated", {"channel_id": channel_id})

    def latest_price_publication(self, guild_id: str, channel_id: str):
        rows = self.db.execute("SELECT * FROM price_publications WHERE guild_id=? AND channel_id=? AND state='published'", (guild_id, channel_id)).fetchall()
        return dict(max(rows, key=lambda row: int(row['source_id']))) if rows else None

    def price_publication(self, guild_id: str, source_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM price_publications WHERE guild_id=? AND source_id=?", (guild_id, source_id)).fetchone()
        return dict(row) if row else None

    def price_message_ids(self, guild_id, channel_id):
        return {value for row in self.db.execute('SELECT source_id,result_id FROM price_publications WHERE guild_id=? AND channel_id=?',
                (guild_id,channel_id)) for value in row if value is not None}

    def claim_price_publication(self, guild_id: str, channel_id: str, source_id: str, fingerprint: str) -> bool:
        # Запись до отправки защищает от повторной публикации после перезапуска или неоднозначного сбоя.
        result = self.db.execute("INSERT OR IGNORE INTO price_publications VALUES (?, ?, ?, NULL, ?, 'pending')",
                                 (source_id, guild_id, channel_id, fingerprint))
        return bool(result.rowcount)

    def save_price_publication(self, guild_id: str, source_id: str, result_id: str, fingerprint: str):
        self.db.execute("""UPDATE price_publications SET result_id=?, fingerprint=?, state='published'
            WHERE guild_id=? AND source_id=?""", (result_id, fingerprint, guild_id, source_id))

    def uncertain_price_publication(self, guild_id: str, source_id: str):
        self.db.execute("UPDATE price_publications SET state='uncertain' WHERE guild_id=? AND source_id=?", (guild_id, source_id))

    def release_failed_price_publication(self, guild_id, source_id):
        # Только подтверждённый отказ Discord позволяет безопасно повторить отправку.
        self.db.execute("DELETE FROM price_publications WHERE guild_id=? AND source_id=? AND state='pending'", (guild_id,source_id))

    def price_subscriptions(self, guild_id: str, user_id: str):
        return [row['item'] for row in self.db.execute(
            "SELECT item FROM price_subscriptions WHERE guild_id=? AND user_id=? ORDER BY item",
            (guild_id, user_id))]

    def price_subscription_modes(self, guild_id, user_id):
        return {r['item']: r['threshold'] for r in self.db.execute(
            'SELECT item, threshold FROM price_subscriptions WHERE guild_id=? AND user_id=?', (guild_id, user_id))}

    def replace_price_subscriptions(self, guild_id: str, user_id: str, items: list[str], modes=None):
        # Сохраняем весь выбор одной транзакцией. Для оставленных товаров не сбрасываем защиту от дублей.
        selected = set(items)
        if modes is not None and (set(modes) != selected or any(v not in ('max', 'near') for v in modes.values())):
            raise UserError('Неверный режим уведомлений.')
        with self.transaction():
            for old in self.price_subscriptions(guild_id, user_id):
                if old not in selected:
                    self.db.execute("DELETE FROM price_subscriptions WHERE guild_id=? AND user_id=? AND item=?",
                                    (guild_id, user_id, old))
            for item in selected:
                self.db.execute("INSERT OR IGNORE INTO price_subscriptions(guild_id, user_id, item, notified) VALUES (?, ?, ?, 0)",
                                (guild_id, user_id, item))
                if modes is not None:
                    # Смена порога начинает новый цикл только для изменённого товара.
                    self.db.execute("UPDATE price_subscriptions SET threshold=?, notified=0 WHERE guild_id=? AND user_id=? AND item=? AND threshold<>?",
                                    (modes[item], guild_id, user_id, item, modes[item]))
        return self.price_subscriptions(guild_id, user_id)

    def set_price_subscription(self, guild_id: str, user_id: str, item: str, enabled: bool):
        # Повторное включение сохраняет флаг уведомления, чтобы не получить лишний тег.
        if enabled:
            self.db.execute("INSERT OR IGNORE INTO price_subscriptions(guild_id, user_id, item, notified) VALUES (?, ?, ?, 0)",
                            (guild_id, user_id, item))
        else:
            self.db.execute("DELETE FROM price_subscriptions WHERE guild_id=? AND user_id=? AND item=?",
                            (guild_id, user_id, item))
        return [row['item'] for row in self.db.execute(
            "SELECT item FROM price_subscriptions WHERE guild_id=? AND user_id=? ORDER BY item",
            (guild_id, user_id))]

    def claim_price_alerts(self, guild_id: str, source_id: str, maximum_items: list[str], near_items=None):
        # Флаг сохраняется ДО отправки: неоднозначный сетевой сбой не вызывает повторных тегов.
        # Старые отчёты не сбрасывают флаги текущих цен при ручной обработке.
        with self.transaction():
            cursor = self.db.execute("SELECT source_id FROM price_alert_cursor WHERE guild_id=?", (guild_id,)).fetchone()
            if cursor and int(source_id) < int(cursor['source_id']):
                return []
            if not cursor:
                previous = self.db.execute("SELECT source_id FROM price_publications WHERE guild_id=?", (guild_id,)).fetchall()
                if any(int(row['source_id']) > int(source_id) for row in previous):
                    return []
            self.db.execute("INSERT INTO price_alert_cursor VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET source_id=excluded.source_id",
                            (guild_id, source_id))
            subscriptions = self.db.execute("SELECT * FROM price_subscriptions WHERE guild_id=?", (guild_id,)).fetchall()
            alerts = []
            for row in subscriptions:
                at_maximum = row['item'] in ((near_items or []) if row['threshold'] == 'near' else maximum_items)
                if at_maximum and not row['notified']:
                    alerts.append({'user_id': row['user_id'], 'item': row['item'], 'threshold': row['threshold']})
                self.db.execute("UPDATE price_subscriptions SET notified=? WHERE guild_id=? AND user_id=? AND item=?",
                                (int(at_maximum), guild_id, row['user_id'], row['item']))
            return self.track_price_alerts(guild_id, source_id, alerts)

    def release_channel(self, guild_id):
        row = self.db.execute('SELECT * FROM release_settings WHERE guild_id=?', (guild_id,)).fetchone()
        return dict(row) if row else None

    def configure_release_channel(self, guild_id, actor_id, channel_id):
        with self.transaction():
            self.ensure_channel_separation(guild_id, channel_id)
            self.db.execute('INSERT INTO release_settings VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id', (guild_id, channel_id))
            self.audit(guild_id, actor_id, 'release.channel.updated', {'channel_id': channel_id})

    def release_publication(self, guild_id, version):
        row = self.db.execute('SELECT * FROM release_publications WHERE guild_id=? AND version=?', (guild_id, version)).fetchone()
        return dict(row) if row else None

    def claim_release(self, guild_id, version):
        with self.transaction():
            result = self.db.execute("INSERT OR IGNORE INTO release_publications(guild_id,version,state) VALUES (?,?,'pending')", (guild_id, version))
            return bool(result.rowcount)

    def save_release(self, guild_id, version, message_id):
        self.db.execute("UPDATE release_publications SET state='published', message_id=? WHERE guild_id=? AND version=?", (message_id, guild_id, version))

    def uncertain_release(self, guild_id, version):
        self.db.execute("UPDATE release_publications SET state='uncertain' WHERE guild_id=? AND version=?", (guild_id, version))

    def claim_release_delivery(self, guild_id, channel_id, version):
        with self.transaction():
            # Ранее опубликованный, но теперь отсутствующий в истории пост можно восстановить.
            result = self.db.execute("""INSERT INTO release_deliveries(guild_id,channel_id,version,state)
                VALUES (?,?,?,'pending') ON CONFLICT(guild_id,channel_id,version)
                DO UPDATE SET state='pending', message_id=NULL WHERE release_deliveries.state='published'""",
                (guild_id, channel_id, version))
            return bool(result.rowcount)

    def save_release_delivery(self, guild_id, channel_id, version, message_id):
        self.db.execute("""INSERT INTO release_deliveries VALUES (?,?,?,'published',?)
            ON CONFLICT(guild_id,channel_id,version) DO UPDATE SET state='published', message_id=excluded.message_id""",
            (guild_id, channel_id, version, message_id))

    def uncertain_release_delivery(self, guild_id, channel_id, version):
        self.db.execute("UPDATE release_deliveries SET state='uncertain' WHERE guild_id=? AND channel_id=? AND version=?",
                        (guild_id, channel_id, version))

    def announcement_storage(self, guild_id):
        row = self.db.execute('SELECT * FROM announcement_settings WHERE guild_id=?', (guild_id,)).fetchone()
        return dict(row) if row else None

    def configure_announcement_storage(self, guild_id, channel_id):
        with self.transaction():
            self.ensure_channel_separation(guild_id, channel_id)
            self.db.execute('INSERT INTO announcement_settings VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id', (guild_id,channel_id))

    def announcement_template(self, guild_id, number):
        row = self.db.execute('SELECT * FROM announcement_templates WHERE guild_id=? AND id=?', (guild_id,number)).fetchone()
        return dict(row) if row else None

    def announcement_templates(self, guild_id):
        return [dict(r) for r in self.db.execute('SELECT * FROM announcement_templates WHERE guild_id=? ORDER BY name', (guild_id,))]

    def save_announcement_template(self, guild_id, name, channel_id, role_id, body, image_ref=None, clear_image=False):
        with self.transaction():
            self.ensure_channel_separation(guild_id, channel_id)
            self.db.execute("""INSERT INTO announcement_templates(guild_id,name,channel_id,role_id,body,image_ref)
                VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id,name) DO UPDATE SET
                channel_id=excluded.channel_id, role_id=excluded.role_id, body=excluded.body,
                image_ref=CASE WHEN ? THEN NULL WHEN excluded.image_ref IS NOT NULL THEN excluded.image_ref ELSE announcement_templates.image_ref END""",
                (guild_id,name,channel_id,role_id,body,image_ref,int(clear_image)))
            return self.db.execute('SELECT id FROM announcement_templates WHERE guild_id=? AND name=?',(guild_id,name)).fetchone()['id']

    def create_announcement_schedule(self, guild_id, template_id, kind, starts_at, days, clock):
        with self.transaction():
            if not self.announcement_template(guild_id,template_id):
                raise UserError('Шаблон не найден на этом сервере.')
            result = self.db.execute('INSERT INTO announcement_schedules(guild_id,template_id,kind,starts_at,days,clock) VALUES (?,?,?,?,?,?)',
                                     (guild_id,template_id,kind,starts_at,days,clock))
            return result.lastrowid

    def announcement_schedules(self, guild_id, enabled_only=False):
        return [dict(r) for r in self.db.execute("""SELECT s.*,t.name,t.channel_id,t.role_id,t.body,t.image_ref
            FROM announcement_schedules s JOIN announcement_templates t ON t.id=s.template_id
            WHERE s.guild_id=? AND (?=0 OR s.enabled=1) ORDER BY s.id""",(guild_id,int(enabled_only)))]

    def disable_announcement_schedule(self, guild_id, number):
        result = self.db.execute('UPDATE announcement_schedules SET enabled=0 WHERE guild_id=? AND id=?',(guild_id,number))
        if not result.rowcount:
            raise UserError('Расписание на этом сервере не найдено.')

    def announcement_attempt(self, schedule_id, starts_at):
        row = self.db.execute('SELECT * FROM announcement_attempts WHERE schedule_id=? AND starts_at=?',(schedule_id,starts_at)).fetchone()
        return dict(row) if row else None

    def claim_announcement(self, schedule_id, starts_at):
        with self.transaction():
            result = self.db.execute("""INSERT OR IGNORE INTO announcement_attempts(schedule_id,starts_at,state)
                SELECT id,?,'pending' FROM announcement_schedules WHERE id=? AND enabled=1""",(starts_at,schedule_id))
            return bool(result.rowcount)

    def finish_announcement(self, schedule_id, starts_at, state, message_id):
        self.db.execute('UPDATE announcement_attempts SET state=?,message_id=? WHERE schedule_id=? AND starts_at=?',
                        (state,message_id,schedule_id,starts_at))

    def close(self):
        self.db.close()
