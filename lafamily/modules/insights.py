"""Команды истории, профиля, ведомостей и истории цен."""
import csv
from io import BytesIO, StringIO
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

import discord
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.command_groups import command_group
from lafamily.confirmations import ConfirmAction
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from lafamily.modules.events.time import MOSCOW, display_date, timestamp, week_range
from lafamily.modules.prices.parser import LIMITS

STATES = {'prepared':'Подготовлена','approved':'Утверждена','paid':'Выплачена'}


def sheet_text(sheet):
    return (f'**Ведомость #{sheet["id"]} — {STATES[sheet["state"]]}**\n'
            f'{display_date(sheet["start_at"])} — {display_date(sheet["end_at"])} МСК\n'
            f'Банк: ${sheet["bank"]:,}; участников: {len(sheet["rows"])}.\n'+
            '\n'.join(f'<@{r["user_id"]}> — {r["points"]} балл. → ${r["amount"]:,}' for r in sheet['rows'][:15])+
            '\nПолный снимок — в CSV. Статус выплаты — отметка администратора; деньги бот не переводит.')


def sheet_file(sheet):
    output = StringIO(newline='')
    writer = csv.writer(output,delimiter=';')
    writer.writerow(['Ведомость','Статус','Период с','Период до','Участник','Баллы','Сумма'])
    writer.writerows((sheet['id'],STATES[sheet['state']],sheet['start_at'],sheet['end_at'],
                     r['user_id'],r['points'],r['amount']) for r in sheet['rows'])
    return discord.File(BytesIO(output.getvalue().encode('utf-8-sig')),filename=f'premii-{sheet["id"]}.csv')


def history_graph(item, rows):
    """Самостоятельный SVG без внешних ресурсов; открывается в браузере."""
    top = max(r['maximum'] for r in rows) or 1
    count = max(1,len(rows)-1)
    points = ' '.join(f'{65+i*650/count:.1f},{290-r["maximum"]*240/top:.1f}' for i,r in enumerate(rows))
    labels = ''.join(f'<text x="{65+i*650/count:.1f}" y="315" font-size="11" text-anchor="middle">{escape(r["day"][5:])}</text>'
                     for i,r in enumerate(rows) if i in {0,len(rows)-1,len(rows)//2})
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="360" viewBox="0 0 800 360">'
           f'<rect width="800" height="360" fill="white"/><g font-family="sans-serif" fill="#222">'
           f'<text x="65" y="27" font-size="18">{escape(item)} — лучшая цена дня, $ (Москва)</text>'
           f'<path d="M65 50V290H740" fill="none" stroke="#888"/>'
           f'<text x="5" y="55">{top}</text><text x="40" y="290">0</text>'
           f'<polyline points="{points}" fill="none" stroke="#5865f2" stroke-width="3"/>{labels}</g></svg>')
    return discord.File(BytesIO(svg.encode()),filename='prices.svg')


def register_insights(tree, client, store, guild_id):
    points = command_group(tree,guild_id,'баллы','Баллы семьи')
    prices = command_group(tree,guild_id,'скупщик','Цены и подписки')
    profile = command_group(tree,guild_id,'участник','Карточка участника')
    sheets = app_commands.Group(name='ведомости',description='Сохраняемые ведомости премий',guild_only=True)
    points.add_command(sheets)

    @points.command(name='история',description='Начисления, бонусы и отмены ваших баллов',extras={'access':'all'})
    @app_commands.rename(page='страница')
    async def history(interaction: discord.Interaction,page:app_commands.Range[int,1,10000]=1):
        await interaction.response.defer(ephemeral=True)
        rows = await database_call(store,'points_history',str(interaction.guild_id),str(interaction.user.id),(page-1)*5,5)
        lines = []
        for r in rows:
            status = f'Отменён: {discord.utils.escape_markdown(r["cancel_reason"] or "без причины")[:100]}' if r['cancelled_at'] else f'+{r["awarded"]} балл.'
            if r['cancelled_at'] and r['cancelled_by']:status+=f' · <@{r["cancelled_by"]}>'
            lines.append(f'**#{r["id"]} {discord.utils.escape_markdown(r["type_name"])[:80]}** · {display_date(r["occurred_at"])} МСК\n'
                         f'База {r["base_points"]} + победа {r["win_bonus"]} + MVP {r["mvp_points"]}. {status}')
        await interaction.edit_original_response(content=f'**История баллов • страница {page}**\n'+('\n\n'.join(lines) or 'Записей нет.'),allowed_mentions=discord.AllowedMentions.none())

    @profile.command(name='профиль',description='Баллы, участие, победы и MVP',extras={'access':'all'})
    @app_commands.rename(member='участник')
    async def participant(interaction:discord.Interaction,member:discord.Member|None=None):
        await interaction.response.defer(ephemeral=True)
        member = member or interaction.user
        data = await database_call(store,'participant_profile',str(interaction.guild_id),str(member.id),week_range())
        text = (f'**Карточка <@{member.id}>**\nВсего баллов: **{data["total"]}**; за неделю: **{data["week"]}**.\n'
                f'Участий: {data["events"]}; побед: {data["wins"]}; MVP: {data["mvps"]}.\nОтменённые отчёты исключены.')
        if member.id == interaction.user.id:
            selected = await database_call(store,'price_subscriptions',str(interaction.guild_id),str(member.id))
            text += '\nВаши подписки: '+(', '.join(selected) or 'нет')
        await interaction.edit_original_response(content=text,allowed_mentions=discord.AllowedMentions.none())

    @sheets.command(name='создать',description='Сохранить снимок недельной премии',extras={'access':'admin'})
    @app_commands.rename(bank='банк',week='неделя')
    @app_commands.choices(week=[app_commands.Choice(name='Прошлая',value=-1),app_commands.Choice(name='Текущая',value=0)])
    async def prepare(interaction:discord.Interaction,bank:app_commands.Range[int,1,1000000000],week:int=-1):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'admin')
        sheet = await database_call(store,'create_payout_sheet',str(interaction.guild_id),str(interaction.user.id),str(interaction.id),week_range(week),bank)
        await interaction.edit_original_response(content=sheet_text(sheet),attachments=[sheet_file(sheet)],allowed_mentions=discord.AllowedMentions.none())

    @sheets.command(name='список',description='Сохранённые ведомости премий',extras={'access':'admin'})
    @app_commands.rename(page='страница')
    async def list_sheets(interaction:discord.Interaction,page:app_commands.Range[int,1,10000]=1):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'admin')
        rows = await database_call(store,'payout_sheets',str(interaction.guild_id),(page-1)*15)
        await interaction.edit_original_response(content='**Ведомости**\n'+('\n'.join(
            f'#{r["id"]} · {display_date(r["start_at"])} · ${r["bank"]:,} · {STATES[r["state"]]}' for r in rows) or 'Нет ведомостей.'))

    @sheets.command(name='посмотреть',description='Снимок премий и полная выгрузка CSV',extras={'access':'admin'})
    @app_commands.rename(number='номер')
    async def view_sheet(interaction:discord.Interaction,number:app_commands.Range[int,1]):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'admin')
        sheet = await database_call(store,'payout_sheet',str(interaction.guild_id),number)
        await interaction.edit_original_response(content=sheet_text(sheet),attachments=[sheet_file(sheet)],allowed_mentions=discord.AllowedMentions.none())

    @sheets.command(name='статус',description='Утвердить ведомость или отметить выплату',extras={'access':'admin'})
    @app_commands.rename(number='номер',state='статус')
    @app_commands.choices(state=[app_commands.Choice(name='Утверждена',value='approved'),app_commands.Choice(name='Выплачена',value='paid')])
    async def status(interaction:discord.Interaction,number:app_commands.Range[int,1],state:str):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'admin')
        sheet = await database_call(store,'payout_sheet',str(interaction.guild_id),number)
        async def apply(current):
            updated = await database_call(store,'transition_payout',str(current.guild_id),str(current.user.id),number,state)
            return sheet_text(updated)
        await interaction.edit_original_response(content=f'Ведомость #{number}: ${sheet["bank"]:,}. Отметить «{STATES[state]}»?',
                                                  view=ConfirmAction(store,interaction,'admin',apply))

    @prices.command(name='история',description='Динамика цены товара: таблица, CSV и график',extras={'access':'all'})
    @app_commands.rename(item='товар',days='дней')
    async def price_history(interaction:discord.Interaction,item:str,days:app_commands.Range[int,1,366]=30):
        await interaction.response.defer(ephemeral=True)
        if item not in LIMITS:
            raise UserError('Выберите товар из подсказок.')
        settings = await database_call(store, 'price_settings', str(interaction.guild_id))
        if not settings:
            raise UserError('Канал цен ещё не настроен.')
        channel = await interaction.guild.fetch_channel(int(settings['channel_id']))
        if not channel.permissions_for(interaction.user).view_channel:
            raise UserError('Нет доступа к каналу цен.')
        end = datetime.now(MOSCOW).replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
        rows = await database_call(store,'price_history_data',str(interaction.guild_id),item,timestamp(end-timedelta(days=days)),timestamp(end))
        if not rows:
            await interaction.edit_original_response(content='История за период пуста. Цены начнут сохраняться после установки этой версии.')
            return
        output = StringIO(newline=''); writer = csv.writer(output,delimiter=';')
        writer.writerow(['Дата МСК','Минимум','Максимум','Отчётов'])
        writer.writerows((r['day'],r['minimum'],r['maximum'],r['reports']) for r in rows)
        table = '\n'.join(f'{r["day"]} | ${r["minimum"]}–${r["maximum"]} | отчётов {r["reports"]}' for r in rows[-15:])
        await interaction.edit_original_response(content=f'**{item} • последние 15 дней с данными**\n{table}\nПолный период — в CSV; график — файл SVG.',
            attachments=[discord.File(BytesIO(output.getvalue().encode('utf-8-sig')),filename='prices.csv'),history_graph(item,rows)])

    @price_history.autocomplete('item')
    async def item_choices(interaction,current):
        return [app_commands.Choice(name=i,value=i) for i in LIMITS if current.casefold() in i.casefold()][:25]

    @prices.command(name='проверить-лс',description='Отправить себе тестовое личное сообщение',extras={'access':'all'})
    async def test_private(interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        now = datetime.now(UTC)
        # Ограничение на пользователя, а не на подписку: тест не меняет её циклы.
        previous = getattr(client,'private_checks',{}).get(interaction.user.id)
        if previous and (now-previous).total_seconds()<60:
            raise UserError('Повторная проверка доступна через минуту.')
        if not hasattr(client,'private_checks'):
            client.private_checks = {}
        client.private_checks[interaction.user.id] = now
        try:
            await interaction.user.send('✅ Проверка ЛС LaFamily: личные сообщения доступны. Это тест, подписки не изменены.',allowed_mentions=discord.AllowedMentions.none())
            text = 'Тестовое ЛС отправлено. Уведомления скупщика также требуют доступа к каналу цен.'
        except discord.Forbidden:
            text = 'Discord отклонил ЛС. Проверьте настройки конфиденциальности и блокировку бота.'
        except Exception:
            text = 'Отправка не подтверждена. Проверьте ЛС; автоматического повтора нет.'
        await interaction.edit_original_response(content=text)
