"""Регистрация slash-команд только на сервере из .env, без запуска Gateway."""

import asyncio
import logging

import discord

from lafamily.bot import FamilyBot
from lafamily.config import load_config
from lafamily.async_database import DatabaseWorker
from lafamily.errors import UserError
from lafamily.diagnostics import log_error


async def deploy():
    config = load_config()
    # Для публикации описаний не нужна настоящая база семьи.
    async with DatabaseWorker(":memory:") as store:
        async with FamilyBot(config, store) as bot:
            await bot.login(config.token)
            # login уже выполнил регистрацию в setup_hook: второй запрос не нужен.
            commands = bot.tree.get_commands(guild=discord.Object(id=config.guild_id))
            print(f"Команды LaFamily зарегистрированы: {len(commands)}.")


if __name__ == "__main__":
    try:
        asyncio.run(deploy())
    except UserError as error:
        logging.error("%s", error)
        raise SystemExit(1)
    except Exception as error:
        log_error("Не удалось зарегистрировать команды. Проверьте .env и права приложения.", error)
        raise SystemExit(1)
