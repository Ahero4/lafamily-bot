"""Все даты вводятся по Москве, в базе хранятся в UTC."""

import re
from datetime import UTC, datetime, timedelta, timezone

from lafamily.errors import UserError

# Для текущего учёта достаточно UTC+3: системная база часовых поясов не требуется.
MOSCOW = timezone(timedelta(hours=3), name="Europe/Moscow")


def timestamp(value: datetime | None = None) -> str:
    # Формат совпадает с прежней JS-версией, поэтому существующая SQLite совместима.
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_start(value: str, now: datetime | None = None) -> str:
    value = value.strip()
    if re.fullmatch(r'[0-9]{2}:[0-9]{2}', value):
        today = (now or datetime.now(UTC)).astimezone(MOSCOW).date()
        value = f'{today.isoformat()} {value}'
    try:
        start = datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=MOSCOW)
    except ValueError as error:
        raise UserError('Укажите ЧЧ:ММ для сегодняшнего дня или ГГГГ-ММ-ДД ЧЧ:ММ по Москве.') from error
    return timestamp(start)


def parse_event_date(value: str | None, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    # В подсказке хранится маркер, а не время её открытия: оно берётся при создании отчёта.
    if not value or value.strip().casefold() == "сегодня":
        return timestamp(now)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}", value):
        raise UserError("Дата нужна в формате ГГГГ-ММ-ДД ЧЧ:ММ по Москве.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=MOSCOW)
    except ValueError as error:
        raise UserError("Указана несуществующая дата или время.") from error
    if parsed > now:
        raise UserError("Нельзя подать отчёт о будущем мероприятии.")
    return timestamp(parsed)


def week_range(offset: int = 0, now: datetime | None = None) -> tuple[str, str]:
    if offset not in (0, -1):
        raise UserError("Доступны текущая и прошлая недели.")
    local = (now or datetime.now(UTC)).astimezone(MOSCOW)
    monday = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    monday += timedelta(weeks=offset)
    return timestamp(monday), timestamp(monday + timedelta(days=7))


def display_date(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")
