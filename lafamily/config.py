"""Загрузка секретов из .env; настройки семьи хранятся отдельно в SQLite."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from lafamily.errors import UserError

ROOT = Path(__file__).resolve().parent.parent


def database_path() -> Path:
    """Один и тот же путь к базе для запуска, резервного копирования и тестов."""
    path = Path(os.getenv("DATABASE_PATH", "./data/lafamily.sqlite"))
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Config:
    # repr=False не даёт случайно вывести токен при печати объекта настроек.
    token: str = field(repr=False)
    client_id: int
    guild_id: int
    database: Path
    proxy: str | None = field(default=None, repr=False)
    members_intent: bool = False
    message_content_intent: bool = False


def load_config() -> Config:
    load_dotenv(ROOT / ".env", override=False)
    for name in ("DISCORD_TOKEN", "DISCORD_CLIENT_ID", "DISCORD_GUILD_ID"):
        if not os.getenv(name, "").strip():
            raise UserError(f"Заполните {name} в файле .env.")
    for name in ("DISCORD_CLIENT_ID", "DISCORD_GUILD_ID"):
        if not re.fullmatch(r"[0-9]{17,20}", os.environ[name]):
            raise UserError(f"Некорректный {name}.")
    members = os.getenv("DISCORD_MEMBERS_INTENT", "false").strip().lower()
    if members not in ("true", "false"):
        raise UserError("DISCORD_MEMBERS_INTENT должен быть true или false.")
    message_content = os.getenv("DISCORD_MESSAGE_CONTENT_INTENT", "false").strip().lower()
    if message_content not in ("true", "false"):
        raise UserError("DISCORD_MESSAGE_CONTENT_INTENT должен быть true или false.")
    return Config(
        token=os.environ["DISCORD_TOKEN"].strip(),
        client_id=int(os.environ["DISCORD_CLIENT_ID"]),
        guild_id=int(os.environ["DISCORD_GUILD_ID"]),
        database=database_path(),
        proxy=os.getenv("DISCORD_PROXY") or None,
        members_intent=members == "true",
        message_content_intent=message_content == "true",
    )
