"""Версия из метаданных проекта и короткое описание текущего выпуска."""
from pathlib import Path
import tomllib

# Один источник номера версии; файл входит в проект и развёртывается вместе с ботом.
with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as metadata:
    VERSION = tomllib.load(metadata)["project"]["version"]

LATEST_UPDATE = (
    "Руководство может редактировать и подтверждать открытые отчёты организаторов; "
    "выбор победы и MVP доступен в форме подтверждения."
)
