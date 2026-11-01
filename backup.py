"""Согласованная копия SQLite, включая ещё не перенесённые из WAL записи."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from lafamily.config import ROOT, database_path
from lafamily.errors import UserError


def create_backup(source: Path, directory: Path) -> Path:
    if not source.is_file():
        raise UserError("База ещё не создана. Сначала запустите бота.")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"lafamily-{datetime.now(UTC):%Y-%m-%dT%H-%M-%S-%fZ}.sqlite"
    # Режим ro не создаёт пустую базу при ошибке в исходном пути.
    original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        original.backup(copy)
    finally:
        copy.close()
        original.close()
    return target


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    try:
        print(f"Резервная копия: {create_backup(database_path(), ROOT / 'backups')}")
    except UserError as error:
        print(error)
        raise SystemExit(1)
