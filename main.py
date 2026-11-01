"""Запуск: python main.py. Команды публикуются отдельно через deploy.py."""

import asyncio
import logging

from lafamily.bot import FamilyBot
from lafamily.config import load_config
from lafamily.async_database import DatabaseWorker
from lafamily.errors import UserError
from lafamily.diagnostics import log_error


async def run():
    config = load_config()
    async with DatabaseWorker(config.database) as store:
        async with FamilyBot(config, store) as bot:
            await bot.start(config.token)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except UserError as error:
        logging.error("%s", error)
        raise SystemExit(1)
    except Exception as error:
        log_error("Не удалось запустить бота. Проверьте .env и соединение.", error)
        raise SystemExit(1)
