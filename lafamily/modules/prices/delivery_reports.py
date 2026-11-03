"""Админские отчёты рассылки, динамика по дням и CSV-выгрузка."""
import csv
import io
from datetime import datetime, timedelta

import discord
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.command_groups import command_group
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.modules.events.time import MOSCOW, timestamp
from lafamily.permissions import require_access_async


def report_period(days, now=None):
    if not 1 <= days <= 366:
        raise UserError('Период: от 1 до 366 дней.')
    now = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return timestamp(end - timedelta(days=days)), timestamp(end)


def csv_file(rows, name):
    stream = io.StringIO(newline='')
    if rows:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=';')
        writer.writeheader()
        writer.writerows(rows)
    return discord.File(io.BytesIO(stream.getvalue().encode('utf-8-sig')), filename=name)


def format_dynamics(daily, view):
    if not daily:
        return 'За этот период рассылок скупщика нет. История собирается с момента обновления бота.'
    # В чате показываем последние 10 дней с рассылками; CSV содержит весь выбранный период.
    rows = daily[-10:]
    prefix = ('**Динамика уведомлений скупщика · дни по Москве**\n'
              'Люди: получено хотя бы одно DM / получатели. Сообщения: отправлено / всего.\n')
    if view == 'table':
        lines = ['Дата       | Подп. | Люди | DM | Ошибки | ? | Ожид.', '-----------|-------|------|----|--------|---|------']
        for r in rows:
            lines.append(f"{r['day']} | {r['subscribers']} | {r['delivered']}/{r['recipients']} | {r['sent']}/{r['messages']} | "
                         f"{r['failed']} | {r['uncertain']} | {r['pending']}")
        body = '```\n' + '\n'.join(lines) + '\n```'
    else:
        body = '\n'.join(f"• {r['day']}: подп. {r['subscribers']}, люди {r['delivered']}/{r['recipients']}, DM {r['sent']}/{r['messages']}, "
            f"ошибки {r['failed']}, ? {r['uncertain']}, ожид. {r['pending']}" for r in rows)
    tail = ('\n? — доставка не подтверждена; ожид. — результат не записан (в том числе прерванная рассылка). '
            'Подп. — снимок на последнюю рассылку дня. Доставка не означает прочтение. Люди уникальны внутри дня; показаны дни с рассылками.')
    if len(daily) > len(rows):
        tail += '\nПоказаны последние 10 дней с рассылками; полный период доступен через выгрузку.'
    return prefix + body + tail


async def send_admin_delivery_report(store, message, run_id):
    guild_id = str(message.guild.id)
    channel_id = await database_call(store, 'admin_report_channel', guild_id)
    if not channel_id:
        await database_call(store, 'finish_price_admin_report', run_id, 'disabled')
        return
    try:
        prices = await database_call(store, 'price_settings', guild_id)
        if channel_id == str(message.channel.id) or (prices and channel_id == prices['channel_id']):
            raise UserError('Канал админских отчётов совпадает с каналом цен. Выберите отдельный канал.')
        summary = await database_call(store, 'price_delivery_summary', guild_id, run_id)
        channel = await message.guild.fetch_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise UserError('Для админских отчётов нужен текстовый канал.')
        posted = await channel.send(
            content=('**Отчёт рассылки скупщика**\n'
                f"Подписчиков на момент рассылки: **{summary['subscribers']}**.\n"
                f"Получили хотя бы одно DM: **{summary['delivered']} из {summary['recipients']}** получателей.\n"
                f"Сообщения: успешно **{summary['sent']} из {summary['messages']}**, "
                f"ошибок **{summary['failed']}**, не подтверждено **{summary['uncertain']}**, "
                f"без результата **{summary['pending']}**.\n"
                'Доставка подтверждена Discord; прочтение не отслеживается.\n'
                f'https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}'),
            allowed_mentions=discord.AllowedMentions.none())
    except Exception as error:
        await database_call(store, 'finish_price_admin_report', run_id, 'uncertain')
        log_error('Не подтверждён админский отчёт рассылки; автоповтор отключён', error)
    else:
        await database_call(store, 'finish_price_admin_report', run_id, 'sent', str(posted.id))


def register_delivery_commands(tree, service, guild):
    bot_group = command_group(tree, guild.id, 'бот', 'Настройки, состояние и служебные отчёты бота')
    price_group = command_group(tree, guild.id, 'скупщик', 'Цены, подписки и история доставки')
    @bot_group.command(name='отчёты', description='Выбрать канал служебных отчётов или отключить их',
                  extras={'access': 'admin'})
    @app_commands.guild_only()
    @app_commands.rename(channel='канал')
    @app_commands.describe(channel='Отдельный админский канал; без параметра — отключить отчёты')
    async def configure(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, 'admin')
        if channel is not None:
            prices = await database_call(service.store, 'price_settings', str(interaction.guild_id))
            if prices and str(channel.id) == prices['channel_id']:
                raise UserError('Выберите отдельный админский канал, отличный от канала цен.')
            permissions = channel.permissions_for(interaction.guild.me)
            if not permissions.view_channel or not permissions.send_messages:
                raise UserError('Боту нужны просмотр и отправка сообщений в админском канале.')
        await database_call(service.store, 'configure_admin_report_channel', str(interaction.guild_id),
                            str(interaction.user.id), str(channel.id) if channel else None)
        await interaction.edit_original_response(
            content=f'Админские отчёты: <#{channel.id}>.' if channel else
                    'Админские отчёты отключены. История доставки продолжает сохраняться в БД.',
            allowed_mentions=discord.AllowedMentions.none())

    @price_group.command(name='статистика', description='Динамика доставки DM по дням и CSV-выгрузка',
                  extras={'access': 'admin'})
    @app_commands.guild_only()
    @app_commands.rename(days='дней', view='вид', export='выгрузка')
    @app_commands.describe(days='Период до сегодняшнего дня включительно, по Москве (1–366)',
                           view='Таблица или список', export='Приложить CSV по дням и отдельным попыткам доставки')
    @app_commands.choices(view=[app_commands.Choice(name='Таблица', value='table'),
                                app_commands.Choice(name='Список', value='list')])
    async def statistics(interaction: discord.Interaction, days: app_commands.Range[int, 1, 366] = 30,
                         view: str = 'table', export: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, 'admin')
        if view not in {'table', 'list'}:
            raise UserError('Выберите таблицу или список.')
        start, end = report_period(days)
        data = await database_call(service.store, 'price_delivery_data', str(interaction.guild_id), start, end, export)
        files = []
        try:
            if export and data['daily']:
                files = [csv_file(data['daily'], 'buyer-daily.csv'), csv_file(data['attempts'], 'buyer-deliveries.csv')]
                limit = getattr(interaction.guild, 'filesize_limit', 8 * 1024 * 1024)
                for file in files:
                    size = file.fp.seek(0, 2)
                    file.fp.seek(0)
                    if size > limit:
                        raise UserError('CSV превышает лимит вложения сервера. Уменьшите период.')
            await interaction.edit_original_response(content=format_dynamics(data['daily'], view), attachments=files,
                                                     allowed_mentions=discord.AllowedMentions.none())
        finally:
            for file in files:
                file.close()
