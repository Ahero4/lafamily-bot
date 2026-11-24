"""Outbound-only HTTPS exchange with the family portal."""
import asyncio
import logging
import os
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
import discord

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.portal_store import check_command_deadline
from lafamily.modules.events.models import DIRECT_REPORT_REQUEST_PREFIX
from lafamily.modules.planning import signup_count_embed

logger = logging.getLogger(__name__)
SNOWFLAKE = re.compile(r'[0-9]{17,20}')


POLL_INTERVAL = 0.5
DRAIN_INTERVAL = 0.05


class PortalSync:
    def __init__(self, client):
        self.client = client
        self.guild = str(client.config.guild_id)
        self.url = os.getenv('FAMILY_BOT_SYNC_URL', '').strip()
        self.token = os.getenv('FAMILY_BOT_SYNC_TOKEN', '').strip()
        self.channels = set(filter(None, re.split(r'[,\s]+', os.getenv('FAMILY_BOT_SYNC_CHANNEL_IDS', '').strip())))
        self.task = None
        self.next_reconcile = 0
        self.bootstrap_id = str(uuid4())
        self.command_task = self.card_task = None
        self.pending_commands = {}
        self.wake = asyncio.Event()

    def start(self):
        if not self.url or self.task and not self.task.done():
            return
        target = urlsplit(self.url)
        if (target.scheme != 'https' or not target.hostname or target.username or target.password
                or target.query or target.fragment or not self.token or not self.channels
                or not all(SNOWFLAKE.fullmatch(c) for c in self.channels)
                or os.getenv('FAMILY_BOT_GUILD_ID') != self.guild):
            logger.error('Проверьте HTTPS-адрес, токен, сервер и открытые каналы синхронизации портала.')
            return
        self.origin = f'{target.scheme}://{target.netloc}'
        self.task = asyncio.create_task(self.run())
        self.card_task = asyncio.create_task(self.maintain_cards())

    async def close(self):
        tasks = [t for t in (self.task, self.card_task, self.command_task) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def db(self, method, *args):
        return await database_call(self.client.store, method, *args)

    def drafts(self):
        return {
            'draft:' + str(view.message.id): view for view in self.client.events.views.values()
            if view.message and not view.finished and time.monotonic() < view.expires_at
            and str(view.message.channel.id) in self.channels and view.draft.guild_id == self.guild
            and view.draft.request_id.startswith(DIRECT_REPORT_REQUEST_PREFIX)
        }

    def draft_snapshots(self):
        return [dict(id=key, kind='draft', name=view.draft.type_name, startsAt=view.draft.occurred_at,
                     capacity=100, closed=False, going=list(view.draft.members), reserve=[], maybe=[])
                for key, view in self.drafts().items()]

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {'Authorization': 'Bearer ' + self.token, 'Origin': self.origin}
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            delay = 2
            while True:
                try:
                    if self.client.is_ready() and not self.client.backups.restoring:
                        if time.monotonic() >= self.next_reconcile:
                            await self.db('portal_resend', self.guild)
                            self.next_reconcile = time.monotonic() + 86400
                        work = await self.exchange(session)
                        delay = DRAIN_INTERVAL if work else POLL_INTERVAL
                    else:
                        delay = POLL_INTERVAL
                except Exception as error:
                    # Do not log request headers, payloads, URLs or member identifiers.
                    logger.warning('Синхронизация портала отложена (%s)', type(error).__name__)
                    delay = min(max(1, delay * 2), 60)
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                self.wake.clear()

    async def exchange(self, session):
        await self.db('portal_scan', self.guild, self.channels, self.draft_snapshots())
        batch = await self.db('portal_batch', self.guild)
        batch['bootstrapId'] = self.bootstrap_id
        batch['caughtUp'] = len(batch['events']) < 10 and len(batch['profiles']) < 10
        async with session.post(self.url, json=batch, allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError(f'Portal HTTP {response.status}')
            data = await response.json()
        commands = data['commands']
        if not isinstance(commands, list):
            raise ValueError('Invalid portal command list')
        await self.db('portal_ack', batch)
        if commands and not self.client.backups.restoring:
            for cmd in commands:
                self.pending_commands.setdefault(cmd['id'], cmd)
            if not self.command_task or self.command_task.done():
                self.command_task = asyncio.create_task(self.apply_commands())
        return any(batch[key] for key in ('events', 'profiles', 'results'))

    async def apply_commands(self):
        while self.pending_commands:
            key = next(iter(self.pending_commands))
            cmd = self.pending_commands[key]
            if self.client.backups.restoring:
                return
            try:
                await self.command(cmd)
                self.wake.set()
            except Exception as error:
                # Unacknowledged commands stay in the portal queue and are retried.
                logger.warning('Обработка команды портала отложена (%s)', type(error).__name__)
            finally:
                self.pending_commands.pop(key, None)

    async def deleted(self, guild, ids):
        if str(guild) != self.guild or self.client.backups.restoring or not self.task:
            return
        for key, view in self.drafts().items():
            if view.message.id in ids:
                async with view.lock:
                    view.finish()
        await self.db('portal_deleted_cards', self.guild, ids)
        self.wake.set()

    async def message_for(self, guild, event):
        try:
            channel_id = int(event['channel_id'])
            channel = guild.get_channel_or_thread(channel_id) or await guild.fetch_channel(channel_id)
            message = await channel.fetch_message(int(event['message_id']))
        except discord.NotFound:
            await self.db('portal_deleted_cards', self.guild, [event['message_id']])
            self.wake.set()
            raise
        return channel, message

    async def maintain_cards(self):
        # Discord API rate limits must not stall the portal heartbeat.
        verify = []
        next_scan = 0
        while True:
            try:
                if self.client.is_ready() and not self.client.backups.restoring:
                    guild = self.client.get_guild(int(self.guild))
                    for number in await self.db('portal_cards', self.guild):
                        try:
                            await self.refresh_card(number)
                        except discord.HTTPException as error:
                            logger.warning('Обновление карточки МП отложено (Discord %s)', error.status)
                    if not verify and time.monotonic() >= next_scan:
                        verify = await self.db('portal_active_cards', self.guild, self.channels)
                        next_scan = time.monotonic() + 300
                    if verify:
                        await self.message_for(guild, verify.pop())
            except discord.NotFound:
                pass  # message_for has already closed the missing card.
            except Exception as error:
                logger.warning('Проверка карточек МП отложена (%s)', type(error).__name__)
            await asyncio.sleep(1)

    async def command(self, cmd):
        key = cmd['id']
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError('Invalid portal command ID')
        if await self.db('portal_replay', self.guild, key):
            return
        try:
            check_command_deadline(cmd)
            member_id, event_id = cmd.get('discordId'), cmd.get('eventId')
            if not isinstance(member_id, str) or not SNOWFLAKE.fullmatch(member_id) or not isinstance(event_id, str):
                raise UserError('Некорректные данные записи.')
            guild = self.client.get_guild(int(self.guild))
            if guild is None:
                raise RuntimeError('Guild cache unavailable')
            view = self.drafts().get(event_id)
            if view:
                channel = view.message.channel
            else:
                if not re.fullmatch(r'[1-9][0-9]{0,17}', event_id):
                    raise UserError('Мероприятие закрыто или недоступно.')
                event = await self.db('planned_event', self.guild, int(event_id))
                if not event['message_id'] or event['channel_id'] not in self.channels:
                    raise UserError('Мероприятие недоступно.')
                channel = guild.get_channel_or_thread(int(event['channel_id']))
                if channel is None:
                    channel, _ = await self.message_for(guild, event)
            member = await guild.fetch_member(int(member_id))
            if member.bot or channel.guild.id != guild.id or not channel.permissions_for(member).view_channel:
                raise UserError('Нет доступа к этому мероприятию.')
            if isinstance(channel, discord.Thread) and channel.is_private() and not channel.permissions_for(member).manage_threads:
                await channel.fetch_member(member.id)
            if view:
                async with view.lock:
                    check_command_deadline(cmd)
                    if self.drafts().get(event_id) is not view or cmd.get('choice') != 'going':
                        raise UserError('Для открытого черновика доступна только запись на участие.')
                    if view.draft.voice_channel_id and str(getattr(getattr(member.voice, 'channel', None), 'id', '')) != view.draft.voice_channel_id:
                        raise UserError('Для записи войдите в голосовой канал МП.')
                    if member_id not in view.draft.members:
                        if len(view.draft.members) + len(view.pending) >= 100:
                            raise UserError('Достигнут лимит участников.')
                        await view.update_members_with(view.message.edit, [*view.draft.members, member_id])
                    await self.db('portal_result', self.guild, key)
            else:
                _, message = await self.message_for(guild, event)
                if message.author.id != self.client.user.id:
                    raise UserError('Карточка мероприятия недоступна.')
                async with self.client.planning_card_lock:
                    check_command_deadline(cmd)
                    await self.db('portal_signup', self.guild, cmd)
        except (discord.NotFound, discord.Forbidden):
            await self.db('portal_result', self.guild, key, 'Карточка или участник недоступны в Discord.')
        except UserError as error:
            await self.db('portal_result', self.guild, key, str(error))

    async def refresh_card(self, number):
        async with self.client.planning_card_lock:
            revision = await self.db('portal_card_revision', self.guild, number)
            event = await self.db('planned_event', self.guild, number)
            if event['message_id'] and event['channel_id'] in self.channels:
                guild = self.client.get_guild(int(self.guild))
                try:
                    _, message = await self.message_for(guild, event)
                except discord.NotFound:
                    await self.db('portal_card_done', self.guild, number, revision)
                    return
                if message.author.id == self.client.user.id and message.embeds:
                    await message.edit(embed=signup_count_embed(message.embeds[0], event),
                                       allowed_mentions=discord.AllowedMentions.none())
            await self.db('portal_card_done', self.guild, number, revision)
