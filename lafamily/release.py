"""Версия из метаданных проекта и короткое описание текущего выпуска."""
from pathlib import Path
import tomllib

# Один источник номера версии; файл входит в проект и развёртывается вместе с ботом.
with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as metadata:
    VERSION = tomllib.load(metadata)["project"]["version"]

LATEST_UPDATE = (
    "Расчёт недельных премий по баллам; настройка базовых баллов, победы и MVP "
    "через /мп вид-добавить; версия и описание обновления в /статус."
)
