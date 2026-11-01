"""SQLite работает в отдельном последовательном потоке и не задерживает Discord."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from lafamily.database import Store


class DatabaseWorker:
    def __init__(self, path: str | Path):
        self.path = path
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lafamily-sqlite")
        self.store = None
        self.closing = False

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
        callback = partial(getattr(self.store, method), *args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(self.executor, callback)

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
