"""Места возникновения ошибок без текста исключений, HTTP-запросов и значений переменных."""

import logging
import traceback

logger = logging.getLogger("lafamily.errors")


def error_trace(error: BaseException) -> str:
    seen = set()
    parts = []
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        code = getattr(error, "code", None)
        parts.append(type(error).__name__ + (f" (код {code})" if isinstance(code, int) else ""))
        for frame in traceback.extract_tb(error.__traceback__):
            # Не выводим строку исходника: она тоже может содержать литерал с секретом.
            parts.append(f"  {frame.filename}:{frame.lineno} в {frame.name}")
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return "\n".join(parts)


def log_error(context: str, error: BaseException):
    logger.error("%s\n%s", context, error_trace(error))
