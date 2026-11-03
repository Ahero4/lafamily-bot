"""SQLite работает в отдельном последовательном потоке и не задерживает Discord."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from lafamily.database import Store
from lafamily.errors import UserError


class DatabaseWorker:
    def __init__(self, path: str | Path):
        self.path = path
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lafamily-sqlite")
        self.store = None
        self.closing = False
        self.paused = False

    async def __aenter__(self):
        try:
            # Соединение создаётся и используется в одном потоке; check_same_thread сохраняется.
            self.store = await asyncio.get_running_loop().run_in_executor(self.executor, Store, self.path)
            return self
        except BaseException:
            self.executor.shutdown(wait=False)
            raise

    async def call(self, method: str, *args, **kwargs):
        if self.closing or self.store is None:
            raise RuntimeError("Работа с базой уже завершена.")
        if self.paused:
            raise UserError('База восстанавливается. Дождитесь переподключения бота и откройте команду заново.')
        callback = partial(getattr(self.store, method), *args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(self.executor, callback)

    async def freeze_and_export(self, target):
        if self.closing or self.paused or self.store is None:
            raise UserError('Работа с базой сейчас недоступна.')
        # Уже принятые операции выполнятся перед копированием в том же executor.
        # Последующие вызовы отклоняются до закрытия старого worker.
        self.paused = True
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.store.export_database, target)
        except BaseException:
            self.paused = False
            raise

    def resume(self):
        if not self.closing:
            self.paused = False

    async def __aexit__(self, exc_type, exc, tb):
        self.closing = True
        try:
            # Закрытие стоит после принятых заданий в очереди и не обрывает транзакции.
            await asyncio.get_running_loop().run_in_executor(self.executor, self.store.close)
        finally:
            self.executor.shutdown(wait=False)


async def database_call(store, method: str, *args, **kwargs):
    """В приложении используется worker; синхронный Store оставлен для изолированных тестов."""
    if isinstance(store, DatabaseWorker):
        return await store.call(method, *args, **kwargs)
    return getattr(store, method)(*args, **kwargs)
