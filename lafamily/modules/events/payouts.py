"""Расчёт недельной премии без изменения баллов и перевода игровых денег."""
from io import BytesIO
import csv
from io import StringIO

import discord

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from .time import display_date, week_range


def allocate_bank(bank: int, rows: list[dict]) -> list[dict]:
    if type(bank) is not int or not 1 <= bank <= 1_000_000_000:
        raise UserError("Банк денег: целое число от 1 до 1 000 000 000 долларов.")
    eligible = [dict(row) for row in rows if row['points'] > 0]
    total = sum(row['points'] for row in eligible)
    if not total:
        raise UserError("За выбранную неделю нет положительных баллов. Банк не распределён.")
    # Целочисленная арифметика исключает ошибки float. Метод наибольших остатков
    # распределяет весь банк; ID служит стабильным порядком при равных остатках.
    for row in eligible:
        row['amount'], row['remainder'] = divmod(bank * row['points'], total)
    left = bank - sum(row['amount'] for row in eligible)
    ordered = sorted(eligible, key=lambda row: (-row['remainder'], int(row['user_id'])))
    for row in ordered[:left]:
        row['amount'] += 1
    return sorted(eligible, key=lambda row: (-row['points'], int(row['user_id'])))


async def send_payout(interaction, store, bank, week):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await require_access_async(interaction, store, 'admin')
    period = week_range(week)
    # LIMIT -1 в SQLite: весь состав одним запросом, без ограничения страницей рейтинга.
    rows = await database_call(store, 'statistics', str(interaction.guild_id), period, limit=-1)
    payouts = allocate_bank(bank, rows)
    total = sum(row['points'] for row in payouts)
    lines = [f"<@{row['user_id']}> — {row['points']} балл. → **${row['amount']:,}**"
             for row in payouts[:20]]
    embed = discord.Embed(title='МП • расчёт премий', colour=0x43B581,
        description=(f"{display_date(period[0])} — {display_date(period[1])} (Москва, конец не включён).\n"
                     f"Банк: **${bank:,}** · Всего баллов: **{total}** · Участников: **{len(payouts)}**\n"
                     f"Цена балла: ${bank:,} / {total} ≈ **${bank / total:,.6f}**.\n"
                     "Расчёт: банк × баллы участника / общие баллы.\n\n" + '\n'.join(lines)))
    embed.set_footer(text='Распределён весь банк. Это расчёт к выдаче; баллы не списываются, деньги не переводятся.')
    attachments = []
    if len(payouts) > 20:
        embed.description += '\n\nПоказаны первые 20 участников. Полная ведомость — в CSV.'
        output = StringIO(newline='')
        writer = csv.writer(output, delimiter=';')
        writer.writerow(['Discord ID', 'Баллы', 'К выплате, $'])
        writer.writerows((row['user_id'], row['points'], row['amount']) for row in payouts)
        attachments = [discord.File(BytesIO(output.getvalue().encode('utf-8-sig')), filename='premii.csv')]
    await interaction.edit_original_response(embed=embed, attachments=attachments,
                                             allowed_mentions=discord.AllowedMentions.none())
