"""Персональные теги при достижении справочного максимума обычной цены."""
import discord

from lafamily.async_database import database_call
from .parser import LIMITS


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
        grouped.setdefault((alert['item'], alert['threshold']), []).append(alert['user_id'])
    for (item, mode), users in grouped.items():
        price, places = (near if mode == 'near' else maxima)[item]
        reason = 'цена 95–100% от максимума!' if mode == 'near' else 'достигнут справочный максимум!'
        # Не более 20 тегов за сообщение; роли и @everyone никогда не разрешены.
        for offset in range(0, len(users), 20):
            batch = users[offset:offset + 20]
            mentions = ' '.join(f'<@{user}>' for user in batch)
            await message.channel.send(
                content=f'{mentions}\n🏆 **{item}: ${price}** — {reason} '
                        f'Скупщик: {places}.\n'
                        f'https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}',
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(id=int(user)) for user in batch], replied_user=False))
