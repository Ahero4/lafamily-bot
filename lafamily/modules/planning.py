"""Запись на будущие МП и перенос подтверждённого состава в черновик отчёта."""
from datetime import datetime
import asyncio
import csv
from io import BytesIO,StringIO

import discord
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.command_groups import command_group
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from lafamily.responses import send_error
from lafamily.modules.events.models import Draft
from lafamily.modules.events.time import MOSCOW,timestamp


def signup_embed(event):
    embed=discord.Embed(title=f'Запись на МП #{event["id"]}: {event["name"]}',colour=0x5865F2,
        description=f'Начало: <t:{int(datetime.fromisoformat(event["starts_at"].replace("Z","+00:00")).timestamp())}:F>\n'
                    f'Мест: {event["capacity"]}; записано: {len(event["going"])}; резерв: {len(event["reserve"])}; возможно: {len(event["maybe"])}.')
    embed.set_footer(text=f'lafamily:planned:{event["id"]}')
    return embed


class SignupView(discord.ui.View):
    def __init__(self,store):
        super().__init__(timeout=None);self.store=store;self.lock=asyncio.Lock()

    async def on_error(self,interaction,error,item):await send_error(interaction,error)

    async def choose(self,interaction,choice):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild or interaction.user.bot or interaction.message.author.id!=interaction.client.user.id:
            raise UserError('Записаться может участник сервера в карточке этого бота.')
        if not interaction.channel.permissions_for(interaction.user).view_channel:
            raise UserError('Нет доступа к каналу МП.')
        try:
            event=await database_call(self.store,'planned_by_message',str(interaction.guild_id),str(interaction.message.id))
        except UserError:
            footer=interaction.message.embeds[0].footer.text if interaction.message.embeds else ''
            prefix='lafamily:planned:'
            if not footer or not footer.startswith(prefix) or not footer[len(prefix):].isdigit():raise
            event=await database_call(self.store,'planned_event',str(interaction.guild_id),int(footer[len(prefix):]))
            if event['channel_id']!=str(interaction.channel_id) or event['message_id']:raise UserError('Карточка не совпала с записью.')
            await database_call(self.store,'bind_planned_message',str(interaction.guild_id),event['id'],str(interaction.message.id))
        async with self.lock:
            event=await database_call(self.store,'signup_planned',str(interaction.guild_id),event['id'],str(interaction.user.id),choice)
            try:
                embed=interaction.message.embeds[0].copy()
                for index in reversed(range(len(embed.fields))):
                    if embed.fields[index].name=='Запись: участников':embed.remove_field(index)
                embed.add_field(name='Запись: участников',value=f'Основной состав: {len(event["going"])} / {event["capacity"]}; резерв: {len(event["reserve"])}; возможно: {len(event["maybe"])}.',inline=False)
                await interaction.message.edit(embed=embed,allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass  # Запись уже сохранена; актуальный состав доступен командой.
        where='Вы отказались от участия.' if choice=='no' else ('Вы в списке «Возможно».' if choice=='maybe' else
            ('Вы в основном составе.' if str(interaction.user.id) in event['going'] else 'Вы в резерве; освободившиеся места распределяются по порядку записи.'))
        await interaction.edit_original_response(content=where)

    @discord.ui.button(label='Участвую',custom_id='lafamily:planned:going',style=discord.ButtonStyle.success)
    async def going(self,interaction,button):await self.choose(interaction,'going')
    @discord.ui.button(label='Возможно',custom_id='lafamily:planned:maybe')
    async def maybe(self,interaction,button):await self.choose(interaction,'maybe')
    @discord.ui.button(label='Отказаться',custom_id='lafamily:planned:no')
    async def no(self,interaction,button):await self.choose(interaction,'no')


def register_planning(tree,client,store,guild_id):
    root=command_group(tree,guild_id,'мп','Мероприятия')
    group=app_commands.Group(name='запись',description='Планирование и запись на МП',guild_only=True)
    root.add_command(group)

    @group.command(name='создать',description='Открыть запись на будущее МП',extras={'access':'organizer'})
    @app_commands.rename(name='название',start='начало',capacity='мест',reminder='напоминание')
    @app_commands.describe(reminder='Напомнить в этом канале за 15 минут до начала. По умолчанию включено.')
    async def create(interaction:discord.Interaction,name:app_commands.Range[str,1,80],start:str,
                     capacity:app_commands.Range[int,1,100]=100,reminder:bool=True):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'organizer')
        try: begins=timestamp(datetime.strptime(start,'%Y-%m-%d %H:%M').replace(tzinfo=MOSCOW))
        except ValueError:raise UserError('Начало: ГГГГ-ММ-ДД ЧЧ:ММ по Москве.')
        await client.events.channel(interaction.guild,str(interaction.channel_id))
        event=await database_call(store,'create_planned_event',str(interaction.guild_id),str(interaction.user.id),str(interaction.id),str(interaction.channel_id),name,begins,capacity,reminder=reminder)
        if event['message_id']:
            await interaction.edit_original_response(content=f'Запись #{event["id"]} уже опубликована.')
            return
        message=await interaction.channel.send(embed=signup_embed(event),view=SignupView(store),allowed_mentions=discord.AllowedMentions.none())
        await database_call(store,'bind_planned_message',str(interaction.guild_id),event['id'],str(message.id))
        notice = '\nБот напомнит в этом канале за 15 минут до начала.' if event['reminder_state'] == 'pending' else ''
        await interaction.edit_original_response(content=f'Запись #{event["id"]} опубликована: {message.jump_url}{notice}')

    @group.command(name='состав',description='Основной состав, резерв и возможные участники',extras={'access':'all'})
    @app_commands.rename(number='номер')
    async def roster(interaction:discord.Interaction,number:app_commands.Range[int,1]):
        await interaction.response.defer(ephemeral=True)
        event=await database_call(store,'planned_event',str(interaction.guild_id),number)
        channel=await interaction.guild.fetch_channel(int(event['channel_id']))
        if not channel.permissions_for(interaction.user).view_channel:raise UserError('Нет доступа к каналу МП.')
        embed=signup_embed(event)
        output=StringIO(newline='');writer=csv.writer(output,delimiter=';');writer.writerow(['Группа','Участник'])
        for label,key in [('Основной состав','going'),('Резерв','reserve'),('Возможно','maybe')]:
            names=event[key]
            writer.writerows((label,user) for user in names)
            for offset in range(0,max(1,min(50,len(names))),25):
                embed.add_field(name=f'{label} ({len(names)})',value=' '.join(f'<@{u}>' for u in names[offset:offset+25]) or 'Нет участников.',inline=False)
        embed.set_footer(text='В сообщении до 50 человек в группе; полный состав — в CSV.')
        await interaction.edit_original_response(embed=embed,attachments=[discord.File(BytesIO(output.getvalue().encode('utf-8-sig')),filename=f'roster-{number}.csv')],allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='закрыть',description='Закрыть запись на МП',extras={'access':'manager'})
    @app_commands.rename(number='номер')
    async def close(interaction:discord.Interaction,number:app_commands.Range[int,1]):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'manager')
        await database_call(store,'close_planned',str(interaction.guild_id),str(interaction.user.id),number)
        await interaction.edit_original_response(content='Запись закрыта. Состав сохранён.')

    @group.command(name='отчёт',description='Подготовить отчёт с основным составом записи',extras={'access':'organizer'})
    @app_commands.rename(number='номер',kind='вид')
    async def report(interaction:discord.Interaction,number:app_commands.Range[int,1],kind:str):
        await interaction.response.defer(thinking=True)
        await require_access_async(interaction,store,'organizer')
        await database_call(store, 'ensure_channel_separation', str(interaction.guild_id), str(interaction.channel_id))
        event=await database_call(store,'planned_event',str(interaction.guild_id),number)
        if event['starts_at']>timestamp():raise UserError('До начала МП отчёт создать нельзя.')
        if not kind.isdigit():raise UserError('Выберите вид МП из подсказки.')
        item=await database_call(store,'event_type',str(interaction.guild_id),int(kind))
        if not item:raise UserError('Вид МП недоступен.')
        settings=await database_call(store,'settings',str(interaction.guild_id))
        if not settings:raise UserError('Сначала /бот настройки.')
        source=await interaction.guild.fetch_channel(int(event['channel_id']))
        if not source.permissions_for(interaction.user).view_channel:raise UserError('Нет доступа к исходной записи.')
        draft=Draft(str(interaction.guild_id),str(interaction.user.id),settings['channel_id'],item['id'],item['name'],item['points'],event['starts_at'],
            note=f'По записи #{number}. Проверьте фактическое участие.',members=list(event['going']),
            win_enabled=bool(item['win_enabled']),available_win_bonus=item['win_bonus'],available_mvp_bonus=item['mvp_bonus'])
        view=await client.events.new_view_async(draft)
        view.bind(interaction)
        message = await interaction.edit_original_response(**view.payload())
        view.message = interaction.channel.get_partial_message(message.id)

    @report.autocomplete('kind')
    async def kinds(interaction,current):
        rows=await database_call(store,'types',str(interaction.guild_id))
        return [app_commands.Choice(name=r['name'],value=str(r['id'])) for r in rows if current.casefold() in r['name'].casefold()][:25]
