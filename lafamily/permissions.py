"""Права проверяются в каждом действии, включая кнопки уже открытого отчёта."""

import discord

from lafamily.async_database import database_call
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.access_roles import role_ids


def require_access(interaction: discord.Interaction, store: Store, level: str):
    settings = store.settings(str(interaction.guild_id)) if level != "admin" else None
    check_access(interaction, settings, level)


async def require_access_async(interaction: discord.Interaction, store, level: str):
    # Чтение ролей также идёт через worker, чтобы не блокировать основной цикл.
    settings = await database_call(store, "settings", str(interaction.guild_id)) if level != "admin" else None
    check_access(interaction, settings, level)


def check_access(interaction: discord.Interaction, settings: dict | None, level: str):
    if interaction.guild is None:
        raise UserError("Команда работает только на сервере семьи.")
    if interaction.permissions.administrator:
        return
    if level == "admin":
        raise UserError("Это действие доступно администратору сервера.")
    roles = {str(role.id) for role in getattr(interaction.user, "roles", [])}
    if roles.intersection(role_ids(settings, "manager")):
        return
    if level == "organizer" and roles.intersection(role_ids(settings, "organizer")):
        return
    raise UserError("Для этого действия нужна назначенная роль организатора или руководства.")
