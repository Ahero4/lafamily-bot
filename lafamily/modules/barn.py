"""Учёт сборов коровника: фиксированная награда за сбор в общей истории баллов."""
import re
from decimal import Decimal

import discord
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.permissions import require_access_async


def parse_amount(value: str) -> int:
    # Принимаем привычное +157к, тысячи с пробелами и целую сумму в долларах.
    if len(value) > 40:
        raise UserError('Слишком длинная сумма. Пример: +157к.')
    cleaned = re.sub(r'[ \u00a0\u202f]', '', value.strip().lower())
    match = re.fullmatch(r'\+?([0-9]+(?:[.,][0-9]{1,3})?)([кk]?)', cleaned)
    if not match:
        raise UserError('Укажите сумму, например 157000 или +157к.')
    amount = Decimal(match[1].replace(',', '.')) * (1000 if match[2] else 1)
    if amount != int(amount) or not 1 <= amount <= 1_000_000_000:
        raise UserError('Сумма должна быть целым числом от 1 до 1 000 000 000.')
    return int(amount)


def register_barn_commands(tree, store, guild_id):
    guild = discord.Object(id=guild_id)

    @tree.command(name='коровник-настройка', description='Задать количество баллов за один сбор коровника', guild=guild, extras={'access': 'admin'})
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(points='баллы')
    async def settings(interaction: discord.Interaction, points: app_commands.Range[int, 0, 1000000]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, store, 'admin')
        await database_call(store, 'configure_barn', str(interaction.guild_id), str(interaction.user.id), points)
        await interaction.edit_original_response(content=f'За один новый сбор коровника: **{points} балл.** Сумма сбора на награду не влияет.')

    @tree.command(name='коровник-сбор', description='Записать сумму сбора коровника и начислить баллы участнику', guild=guild, extras={'access': 'all'})
    @app_commands.guild_only()
    @app_commands.rename(amount='сумма', member='участник')
    @app_commands.describe(amount='Например: 157000 или +157к', member='Кто собрал; если не выбран — автор команды')
    async def collect(interaction: discord.Interaction, amount: str, member: discord.Member | None = None):
        await interaction.response.defer(thinking=True)
        total = parse_amount(amount)
        recipient = member or interaction.user
        # Проверяем участника адресно, без загрузки списка сервера и дополнительного Intent.
        recipient = await interaction.guild.fetch_member(recipient.id)
        if recipient.bot:
            raise UserError('Нельзя начислить баллы боту.')
        report = await database_call(store, 'create_barn_collection', str(interaction.guild_id),
            str(interaction.user.id), str(recipient.id), str(interaction.channel_id),
            f'barn:{interaction.id}', total)
        embed = discord.Embed(title=f'🐄 Коровник — сбор #{report["id"]}', colour=0x43B581)
        embed.add_field(name='Собрал', value=f'<@{recipient.id}>')
        embed.add_field(name='Сумма', value=f'${total:,}'.replace(',', ' '))
        embed.add_field(name='Начислено', value=f'{report["points"]} балл.')
        embed.set_footer(text='Баллы учтены в общей статистике. Ошибка? Обратитесь к руководству с номером сбора.')
        # Начисление уже сохранено: при сбое ответа повторно подавать сбор не требуется.
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())
