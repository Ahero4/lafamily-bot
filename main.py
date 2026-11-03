"""Запуск: python main.py. Команды регистрируются автоматически перед подключением Discord."""

import asyncio
import logging

from lafamily.bot import FamilyBot
from lafamily.config import load_config
from lafamily.async_database import DatabaseWorker
from lafamily.errors import UserError
from lafamily.database_archives import apply_restore, FatalRestoreError
from lafamily.diagnostics import log_error


async def run():
    config = load_config()
    notice = None
    while True:
        async with DatabaseWorker(config.database) as store:
            async with FamilyBot(config, store) as bot:
                bot.startup_notice = notice
                await bot.start(config.token)
            request = bot.restore_request
        if request is None:
            return
        # Старые соединения и сервисы закрыты. Новый worker не использует старые объекты меню.
        try:
            await asyncio.to_thread(apply_restore, request, config.database)
        except FatalRestoreError:
            # Страховочный архив уже сохранён локально и в Discord; запуск не продолжаем.
            raise
        except Exception as error:
            log_error('Восстановление не выполнено; возвращена страховочная копия', error)
            notice = (request.channel_id, 'Восстановление не выполнено. Возвращена страховочная копия исходной БД; '
                      'бот переподключён. Подробности — в журнале ошибок.')
        else:
            notice = (request.channel_id, f'✅ БД восстановлена из сообщения `{request.message_id}`. '
                      f'Бот переподключён. Страховочная копия: сообщение `{request.safety_message_id}`. '
                      'Откройте команды и меню заново.')
        request.cleanup()


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
