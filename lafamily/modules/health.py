"""Диагностика, сообщения администратору и безопасные действия восстановления."""
import asyncio
import hashlib
from datetime import UTC,datetime,timedelta
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import tasks

from lafamily.async_database import database_call
from lafamily.command_groups import command_group
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from lafamily.modules.events.ui import report_embed
from lafamily.modules.events.time import timestamp


class HealthService:
    def __init__(self,client,store,guild_id):
        self.client,self.store,self.guild_id=client,store,guild_id
        self.lock=asyncio.Lock()

    async def inspect(self):
        snapshot=await database_call(self.store,'health_snapshot',str(self.guild_id))
        warnings=list(snapshot['warnings']);channels=[]
        configured=await database_call(self.store,'diagnostic_channels',str(self.guild_id))
        fetched={}
        for setting in configured:
            label=setting['label']+f' (<#{setting["id"]}>)'
            try:
                if setting['id'] not in fetched:fetched[setting['id']]=await self.client.fetch_channel(int(setting['id']))
                channel=fetched[setting['id']]
                if not isinstance(channel,discord.TextChannel) or channel.guild.id!=self.guild_id:
                    raise UserError('Неверный канал')
                permissions=channel.permissions_for(channel.guild.me)
                needed=['view_channel','send_messages',*setting['needs']]
                missing=[p for p in needed if not getattr(permissions,p,False)]
                if missing:warnings.append(f'{label}: не хватает прав '+', '.join(missing))
                channels.append(f'{label}: '+('не хватает прав' if missing else 'доступен'))
                if setting['label']=='Проверка заявок':
                    global_permissions=getattr(channel.guild.me,'guild_permissions',None)
                    for permission in ('manage_roles','manage_nicknames'):
                        if not getattr(global_permissions,permission,False):warnings.append(f'Заявки: не хватает {permission}.')
            except Exception:
                warnings.append(f'{label}: канал недоступен');channels.append(f'{label}: недоступен')
        backup=snapshot['backup']
        failed=sum(1 for r in snapshot['operations'] if r['kind']=='delivery')
        if failed:warnings.append(f'В последних попытках есть проблемные ЛС: {failed}. Проверьте /бот операции и /скупщик статистика.')
        if not snapshot['reports']:warnings.append('История отчётов пуста. Если бот уже использовался, проверьте выбранную БД и резервные копии.')
        if not backup or not backup['channel_id']:
            warnings.append('Автоматические бэкапы не настроены.')
        elif not backup.get('last_success_at'):
            warnings.append('Нет сохранённого времени последнего успешного бэкапа; дождитесь следующей копии.')
        elif backup['last_success_at']<timestamp(datetime.now(UTC)-timedelta(hours=7)):
            warnings.append('Последний успешный бэкап старше 7 часов.')
        snapshot.update(warnings=warnings,channels=channels)
        return snapshot

    def start(self):
        if not self.poll.is_running():self.poll.start()

    async def close(self):
        task=self.poll.get_task();self.poll.cancel()
        if task:
            try:await task
            except asyncio.CancelledError:pass

    @tasks.loop(minutes=10)
    async def poll(self):
        if getattr(getattr(self.client,'backups',None),'restoring',False):return
        try:
            if not self.client.is_ready():return
            async with self.lock:
                data=await self.inspect()
                channel_id=await database_call(self.store,'admin_report_channel',str(self.guild_id))
                if not channel_id:return
                fingerprint=hashlib.sha256('\n'.join(data['warnings']).encode()).hexdigest()
                previous=await database_call(self.store,'health_notice',str(self.guild_id))
                if previous==fingerprint:return
                if data['warnings']:
                    channel=await self.client.fetch_channel(int(channel_id))
                    await database_call(self.store,'ensure_channel_separation',str(self.guild_id),str(channel.id))
                    await channel.send(content='**Диагностика LaFamily**\n'+'\n'.join(data['warnings'])[:1700]+'\nПодробности: /бот диагностика',allowed_mentions=discord.AllowedMentions.none())
                await database_call(self.store,'save_health_notice',str(self.guild_id),fingerprint)
        except Exception as error:log_error('Не удалось выполнить диагностику бота',error)

    async def recover_report(self,guild,number):
        async with self.lock:
            report=await database_call(self.store,'report',str(guild.id),number)
            channel=await self.client.events.channel(guild,report['channel_id'],allow_thread='barn_amount' in report)
            if report['message_id']:
                message=await channel.fetch_message(int(report['message_id']))
                await message.edit(embed=report_embed(report),allowed_mentions=discord.AllowedMentions.none())
                return 'Карточка отчёта обновлена. Начисления не менялись.'
            if not channel.permissions_for(guild.me).read_message_history:
                raise UserError('Для безопасного поиска карточки нужно право чтения истории.')
            expected=report_embed(report)
            titles={expected.title,report_embed({**report,'cancelled_at':None}).title}
            async for message in channel.history(limit=None,after=datetime.fromisoformat(report['created_at'].replace('Z','+00:00'))):
                if message.author.id==self.client.user.id and message.embeds and message.embeds[0].title in titles:
                    await database_call(self.store,'attach_message',str(guild.id),number,str(message.id))
                    await message.edit(embed=expected,allowed_mentions=discord.AllowedMentions.none())
                    return 'Существующая карточка найдена и привязана. Нового начисления нет.'
            if not await database_call(self.store,'claim_report_recovery',str(guild.id),number):
                raise UserError('Предыдущая публикация не подтверждена. Новая отправка заблокирована; проверьте канал вручную.')
            try:
                message=await channel.send(embed=expected,allowed_mentions=discord.AllowedMentions.none())
                await database_call(self.store,'attach_message',str(guild.id),number,str(message.id))
            except discord.HTTPException as error:
                if 400<=error.status<500 and error.status!=408:
                    await database_call(self.store,'release_report_recovery',str(guild.id),number)
                raise
            return 'Карточка опубликована; отчёт и баллы повторно не создавались.'


def register_health(tree,service,guild_id):
    group=command_group(tree,guild_id,'бот','Управление ботом')
    store=service.store
    @group.command(name='диагностика',description='Данные, каналы, права и последний успешный бэкап',extras={'access':'admin'})
    async def diagnostics(interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'admin')
        data=await service.inspect()
        text=(f'**Диагностика**\nSQLite: {data["integrity"]}\nОтчётов: {data["reports"]}; участников истории: {data["members"]}; баллов: {data["active_points"]}.\n'+
              '\n'.join(data['channels'])+'\n'+('\n'.join(data['warnings']) or 'Проблем не обнаружено.')+
              f'\nНезавершённых операций в выборке: {len(data["operations"])}. Подробности: /бот операции.')
        await interaction.edit_original_response(content=text[:1750]+'\nПолная диагностика — в приложенном файле.',
            attachments=[discord.File(BytesIO(text.encode('utf-8-sig')),filename='diagnostics.txt')],allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='операции',description='Незавершённые публикации, заявки и доставки',extras={'access':'admin'})
    @app_commands.rename(page='страница')
    async def operations(interaction:discord.Interaction,page:app_commands.Range[int,1,100]=1):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'admin')
        rows=await database_call(store,'unfinished_operations',str(interaction.guild_id))
        labels={'report':'Отчёт','prices':'Сводка цен','delivery':'ЛС','signup':'Карточка записи','onboarding':'Заявка'}
        lines=[f'{labels.get(r["kind"],r["kind"])} #{r["id"]}: {r["state"]}'+(' · восстановление доступно' if r['safe'] else ' · ручная проверка') for r in rows[(page-1)*15:page*15]]
        await interaction.edit_original_response(content=f'**Операции • страница {page} • по 15 записей**\n'+('\n'.join(lines) or 'На этой странице записей нет.')+
            '\nВосстановление: /бот восстановить-операцию. Заявки: кнопка восстановления в карточке. Сводки цен и неоднозначные ЛС вслепую не повторяются.')

    @group.command(name='восстановить-операцию',description='Обновить или восстановить карточку отчёта',extras={'access':'admin'})
    @app_commands.rename(kind='операция',number='номер')
    @app_commands.choices(kind=[app_commands.Choice(name='Карточка отчёта',value='report')])
    async def recover(interaction:discord.Interaction,kind:str,number:app_commands.Range[int,1]=1):
        await interaction.response.defer(ephemeral=True)
        await require_access_async(interaction,store,'admin')
        if kind != 'report':
            raise UserError('Доступно восстановление карточки отчёта.')
        text = await service.recover_report(interaction.guild, number)
        await interaction.edit_original_response(content=text)
