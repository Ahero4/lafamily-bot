"""Личные уведомления при достижении выбранного порога обычной цены."""
import discord

from lafamily.async_database import database_call
from lafamily.diagnostics import log_error
from .parser import LIMITS
from .delivery_reports import send_admin_delivery_report


def bonus_price_text(report, item, price):
    """Берём бонус у тех же скупщиков, что предлагают лучшую обычную цену."""
    rows = ([(buyer, prices[item]) for buyer, prices in report.buyers.items()]
            if LIMITS[item]['bonus'] is not None else [(None, report.products[item])])
    best_rows = [(buyer, row) for buyer, row in rows if row.base == price]
    values = {row.bonus for _, row in best_rows}
    if len(values) == 1 and None not in values:
        return f" (${next(iter(values))})"
    # Отчёты продуктов и некоторые источники не содержат бонусной цены.
    # Расчёт явно отмечаем, чтобы не выдавать его за цену из игрового отчёта.
    def describe(row):
        return f"${row.bonus}" if row.bonus is not None else f"≈${row.base * 120 // 100} (расчёт)"
    if len(values) == 1:
        return f" ({describe(best_rows[0][1])})"
    prices = ', '.join(f"№{buyer}: {describe(row)}" for buyer, row in best_rows)
    return f" ({prices})"


async def notify_maximum_prices(store, message, report):
    maxima = {}
    near = {}
    for item, limits in LIMITS.items():
        rows = ([(buyer, prices[item]) for buyer, prices in report.buyers.items()]
                if limits['bonus'] is not None else [(None, report.products[item])])
        best = max(row.base for _, row in rows)
        if best * 100 >= limits['base'] * 95:
            places = ', '.join(f'№{buyer}' if buyer else 'продукты'
                               for buyer, row in rows if row.base == best)
            if best >= limits['base']:
                maxima[item] = (best, places)
            if best <= limits['base']:
                near[item] = (best, places)
    alerts = await database_call(store, 'claim_price_alerts', str(message.guild.id), str(message.id), list(maxima), list(near))
    grouped = {}
    for alert in alerts:
        grouped.setdefault((alert['item'], alert['threshold']), []).append(alert)
    for (item, mode), users in grouped.items():
        price, places = (near if mode == 'near' else maxima)[item]
        bonus = bonus_price_text(report, item, price)
        reason = 'цена 95–100% от максимума!' if mode == 'near' else 'достигнут справочный максимум!'
        # Флаги уже сохранены: неоднозначную доставку не повторяем в этом цикле.
        # Ошибка одного получателя не должна останавливать рассылку остальным.
        for alert in users:
            user = alert['user_id']
            sending = False
            state, error_type = 'sent', None
            try:
                member = message.guild.get_member(int(user))
                if member is None:
                    member = await message.guild.fetch_member(int(user))
                sending = True
                await member.send(
                    content=f'🏆 **{item}: ${price}{bonus}** — {reason} '
                            f'Скупщик: **{places}**.\n'
                            f'https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}',
                    allowed_mentions=discord.AllowedMentions.none())
            except Exception as error:
                # Включая закрытые DM, уход с сервера, HTTP- и сетевые ошибки.
                # Публичного запасного уведомления нет; подписка сохраняется.
                error_type = type(error).__name__
                if isinstance(error, discord.Forbidden):
                    state = 'forbidden'
                elif isinstance(error, discord.NotFound):
                    state = 'not_found'
                elif sending and (not isinstance(error, discord.HTTPException) or error.status >= 500):
                    state = 'uncertain'
                else:
                    state = 'failed'
                log_error('Не удалось доставить личное уведомление о цене; автоповтор отключён', error)
            # Успех DM и состояние журнала разделены: ошибка БД не означает ошибку доставки.
            await database_call(store, 'finish_price_delivery', alert['attempt_id'], state, error_type)
    if alerts:
        await send_admin_delivery_report(store, message, alerts[0]['run_id'])
