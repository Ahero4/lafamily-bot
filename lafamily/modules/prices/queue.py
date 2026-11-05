"""Сохраняемая очередь компактных ЛС. Отправленные задачи не повторяются."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
import discord
from discord.ext import tasks
from lafamily.async_database import database_call
from lafamily.diagnostics import log_error
from lafamily.modules.events.time import MOSCOW
from .parser import LIMITS, price_dm_revision
from .notifications import bonus_price_text
from .delivery_reports import send_admin_delivery_report


async def enqueue_notifications(store,message,report):
    revision=price_dm_revision(report)
    maxima,near,prices={},{},{}
    for item,limit in LIMITS.items():
        rows=[(buyer,values[item]) for buyer,values in report.buyers.items()] if limit['bonus'] is not None else [('продукты',report.products[item])]
        best=max(row.base for _,row in rows)
        places=', '.join(f'№{buyer}' if buyer!='продукты' else buyer for buyer,row in rows if row.base==best)
        prices[item]=(best,places)
        if best>=limit['base']:maxima[item]=True
        if limit['base']*95<=best*100<=limit['base']*100:near[item]=True
    alerts=await database_call(store,'claim_price_alerts',str(message.guild.id),str(message.id),list(maxima),list(near))
    users={}
    for alert in alerts:users.setdefault(alert['user_id'],[]).append(alert)
    for user,items in users.items():
        lines=[];attempts=[]
        for alert in items:
            item=alert['item'];price,places=prices[item]
            label='95–100%' if alert['threshold']=='near' else 'максимум'
            line=f'🏆 **{item}: ${price}{bonus_price_text(report,item,price)}** · {places} · {label}'
            if sum(len(v)+1 for v in lines)+len(line)>1650:
                await database_call(store,'enqueue_price_job',str(message.guild.id),str(message.id),revision,user,str(message.channel.id),'\n'.join(lines),attempts)
                lines=[];attempts=[]
            lines.append(line);attempts.append(alert['attempt_id'])
        if lines:
            await database_call(store,'enqueue_price_job',str(message.guild.id),str(message.id),revision,user,str(message.channel.id),'\n'.join(lines),attempts)


class PriceQueue:
    def __init__(self,client,store,guild_id):
        self.client,self.store,self.guild_id=client,store,guild_id
        self.lock=asyncio.Lock();self.recovered=False

    def start(self):
        if not self.poll.is_running():self.poll.start()

    async def close(self):
        task=self.poll.get_task();self.poll.cancel()
        if task:
            try:await task
            except asyncio.CancelledError:pass

    @tasks.loop(seconds=5)
    async def poll(self):
        try:
            if self.client.is_ready():await self.drain()
        except Exception as error:log_error('Ошибка очереди ЛС скупщика',error)

    async def drain(self):
        async with self.lock:
            if not self.recovered:
                await database_call(self.store,'recover_price_jobs',str(self.guild_id));self.recovered=True
            for _ in range(20):
                if getattr(getattr(self.client,'backups',None),'restoring',False):return
                job=await database_call(self.store,'claim_price_job',str(self.guild_id))
                if not job:break
                await self.deliver(job)
            for run in await database_call(self.store,'queued_admin_reports',str(self.guild_id)):
                try:
                    channel=await self.client.fetch_channel(int(run['channel_id']))
                    await send_admin_delivery_report(self.store,SimpleNamespace(id=int(run['source_id']),guild=channel.guild,channel=channel),run['id'])
                except Exception as error:log_error('Не завершён отчёт очереди ЛС',error)

    async def deliver(self,job):
        if not await database_call(self.store,'current_price_job',job):
            await database_call(self.store,'finish_price_job',job['id'],'failed','ExpiredPrice');return
        sending = False
        message = None
        state,error_type='sent',None
        try:
            channel=await self.client.fetch_channel(int(job['channel_id']))
            if str(channel.guild.id)!=job['guild_id']:raise ValueError('GuildMismatch')
            member=await channel.guild.fetch_member(int(job['user_id']))
            if not channel.permissions_for(member).view_channel:
                state,error_type='forbidden','ChannelAccessDenied'
            else:
                if not await database_call(self.store,'current_price_job',job):
                    await database_call(self.store,'finish_price_job',job['id'],'failed','ExpiredPrice');return
                content=(job['body']+'\nОтправлено: **'+datetime.now(MOSCOW).strftime('%d.%m.%Y %H:%M:%S МСК')+'**.\n'+
                    f'https://discord.com/channels/{job["guild_id"]}/{job["channel_id"]}/{job["source_id"]}')
                sending=True
                await member.send(content=content, allowed_mentions=discord.AllowedMentions.none())
            message=SimpleNamespace(id=int(job['source_id']),guild=channel.guild,channel=channel)
        except Exception as error:
            error_type=type(error).__name__
            if isinstance(error,discord.Forbidden):state='forbidden'
            elif isinstance(error,discord.NotFound):state='not_found'
            elif not sending and job['tries']<2 and not isinstance(error,discord.HTTPException):
                await database_call(self.store,'defer_price_job',job['id'],error_type);return
            elif sending and (not isinstance(error,discord.HTTPException) or error.status>=500):state='uncertain'
            else:state='failed'
            log_error('Ошибка доставки ЛС из очереди',error)
        await database_call(self.store,'finish_price_job',job['id'],state,error_type)
        if message:
            run=await database_call(self.store,'price_job_run',job['id'])
            summary=await database_call(self.store,'price_delivery_summary',job['guild_id'],run)
            if summary and not summary['pending'] and summary['admin_state']=='pending':
                await send_admin_delivery_report(self.store,message,run)
