"""Версия из метаданных проекта и короткое описание текущего выпуска."""
from pathlib import Path
import tomllib

# Один источник номера версии; файл входит в проект и развёртывается вместе с ботом.
with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as metadata:
    VERSION = tomllib.load(metadata)["project"]["version"]

LATEST_UPDATE = (
    "Автоматическая регистрация команд при запуске main.py; недельные премии "
    "и единая настройка наград за участие, победу и MVP."
)
