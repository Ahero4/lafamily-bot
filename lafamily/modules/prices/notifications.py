"""Персональные теги при достижении справочного максимума обычной цены."""
import discord

from lafamily.async_database import database_call
from .parser import LIMITS


async def notify_maximum_prices(store, message, report):
    maxima = {}
    for item, limits in LIMITS.items():
        rows = ([(buyer, prices[item]) for buyer, prices in report.buyers.items()]
                if limits['bonus'] is not None else [(None, report.products[item])])
        best = max(row.base for _, row in rows)
        if best >= limits['base']:
            places = ', '.join(f'№{buyer}' if buyer else 'продукты'
                               for buyer, row in rows if row.base == best)
            maxima[item] = (best, places)
    alerts = await database_call(store, 'claim_price_alerts', str(message.guild.id), str(message.id), list(maxima))
    grouped = {}
    for alert in alerts:
        grouped.setdefault(alert['item'], []).append(alert['user_id'])
    for item, users in grouped.items():
        price, places = maxima[item]
        # Не более 20 тегов за сообщение; роли и @everyone никогда не разрешены.
        for offset in range(0, len(users), 20):
            batch = users[offset:offset + 20]
            mentions = ' '.join(f'<@{user}>' for user in batch)
            await message.channel.send(
                content=f'{mentions}\n🏆 **{item}: ${price}** — достигнут справочный максимум! '
                        f'Скупщик: {places}.\n'
                        f'https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}',
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(id=int(user)) for user in batch], replied_user=False))
