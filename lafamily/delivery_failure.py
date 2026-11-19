"""Безопасные пояснения к результатам доставки, без текста запросов и исключений."""
import discord


KNOWN_REASONS = {
    'ChannelAccessDenied': 'У получателя нет доступа к каналу цен.',
    'ExpiredPrice': 'Цена устарела до отправки.',
    'SubscriptionChanged': 'Подписка изменилась до отправки.',
    'ProcessInterrupted': 'Бот перезапустился во время отправки. Доставка не подтверждена.',
    'Forbidden': 'Discord запретил операцию.',
    'NotFound': 'Discord не нашёл запрошенный объект.',
}


def stored_reason(error_type, state):
    if state in ('sent', 'pending'):
        return None
    if error_type in KNOWN_REASONS:
        return KNOWN_REASONS[error_type]
    if state == 'uncertain':
        return 'Доставка не подтверждена.'
    return 'Причина не уточнена.'


def delivery_failure(error, stage):
    if isinstance(error, discord.HTTPException):
        descriptions = {
            50007: 'Discord не разрешил написать получателю.',
            10007: 'Участник не найден на сервере.',
            10003: 'Канал не найден.',
            50001: 'У бота нет доступа.',
            50013: 'У бота недостаточно прав.',
        }
        if error.status >= 500:
            explanation = 'Сервер Discord вернул ошибку. Результат запроса не подтверждён.'
        else:
            explanation = descriptions.get(error.code, 'Discord отклонил запрос.')
        return f'{stage}: {explanation} HTTP {error.status}, код Discord {error.code}.'
    if isinstance(error, TimeoutError):
        explanation = 'Истекло время ожидания ответа.'
    elif isinstance(error, OSError):
        explanation = 'Ошибка сети или соединения.'
    else:
        explanation = 'Операция завершилась ошибкой.'
    return f'{stage}: {explanation}'
