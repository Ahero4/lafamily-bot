"""Доставка анонсов розыгрышей из очереди семейного портала."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import logging
import re
from urllib.parse import quote

import discord

from lafamily.errors import UserError
from lafamily.modules.announcements import check_role
from lafamily.modules.raffles import RAFFLE_ID


logger = logging.getLogger(__name__)
SNOWFLAKE = re.compile(r'[0-9]{17,20}')
SLUG = re.compile(r'[a-z0-9-]{1,80}')
POLL_INTERVAL = 15
# Портал выдаёт аренду на две минуты. Завершаем работу с Discord заранее.
LEASE_SECONDS = 120
DELIVERY_SECONDS = 60


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError('Invalid raffle announcement timestamp')
    return parsed


@dataclass(frozen=True, slots=True)
class RaffleAnnouncement:
    id: str
    slug: str
    title: str
    channel_id: int
    target: str
    claim_id: str
    claimed_at: datetime
    first_attempt_at: datetime

    @classmethod
    def parse(cls, data):
        keys = ('id', 'slug', 'title', 'channelId', 'target', 'claimId', 'firstAttemptAt')
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in keys):
            raise ValueError('Invalid raffle announcement')
        raffle_id, slug, title, channel_id, target, claim_id, first_attempt = (data[key] for key in keys)
        if (not RAFFLE_ID.fullmatch(raffle_id) or not SLUG.fullmatch(slug) or not title.strip()
                or len(title) > 256 or not SNOWFLAKE.fullmatch(channel_id)
                or (target != 'everyone' and not SNOWFLAKE.fullmatch(target))):
            raise ValueError('Invalid raffle announcement fields')
        claimed_at = parse_utc(claim_id)
        first_attempt_at = parse_utc(first_attempt)
        if first_attempt_at > claimed_at:
            raise ValueError('Invalid raffle announcement claim')
        return cls(raffle_id, slug, title, int(channel_id), target, claim_id, claimed_at, first_attempt_at)


class RaffleAnnouncements:
    def __init__(self, client, portal):
        self.client = client
        self.portal = portal
        self.task = None

    def start(self):
        if self.task is not None and not self.task.done():
            return
        try:
            self.portal.credentials()
        except (UserError, ValueError):
            return
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def run(self):
        delay = POLL_INTERVAL
        while True:
            try:
                if self.client.is_ready() and not self.client.backups.restoring:
                    delivered = await self.poll()
                    delay = 1 if delivered else POLL_INTERVAL
                else:
                    delay = POLL_INTERVAL
            except Exception as error:
                logger.warning('Доставка анонса розыгрыша отложена (%s)', type(error).__name__)
                delay = min(max(POLL_INTERVAL, delay * 2), 120)
            await asyncio.sleep(delay)

    async def poll(self):
        response = await self.portal.request('announcement-next')
        if 'announcement' not in response:
            raise ValueError('Missing raffle announcement response')
        data = response['announcement']
        if data is None:
            return False
        announcement = RaffleAnnouncement.parse(data)
        # Проверка срока нужна и после паузы процесса, когда локальный timeout ещё не сработал.
        remaining = self.lease_remaining(announcement)
        if remaining <= 0:
            return False
        try:
            async with asyncio.timeout(min(DELIVERY_SECONDS, remaining)):
                message_id = await self.deliver(announcement)
            if message_id is None:
                return False
            fields = {'messageId': str(message_id)}
        except (discord.Forbidden, discord.NotFound, discord.InvalidData):
            fields = {'error': 'Канал анонсов недоступен боту. Проверьте канал и его права.'}
        except UserError as error:
            fields = {'error': str(error)}
        result = await self.portal.request('announcement-result', raffleId=announcement.id,
                                           claimId=announcement.claim_id, **fields)
        if result.get('ok') is not True:
            raise ValueError('Invalid raffle announcement confirmation')
        return True

    @staticmethod
    def lease_remaining(announcement):
        elapsed = (datetime.now(UTC) - announcement.claimed_at).total_seconds()
        return LEASE_SECONDS - elapsed - 15

    async def deliver(self, announcement):
        guild = self.client.get_guild(self.portal.guild_id)
        if guild is None or guild.me is None or self.client.user is None:
            raise TimeoutError('Discord guild unavailable')
        channel = guild.get_channel(announcement.channel_id) or await guild.fetch_channel(announcement.channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise UserError('Выберите текстовый канал анонсов на сервере семьи.')
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.read_message_history:
            raise UserError('Боту нужны права видеть канал анонсов и читать историю сообщений.')

        marker = f'LaFamily · Розыгрыш {announcement.id}'
        # Сверяем автора и устойчивую метку на случай отправки перед потерей подтверждения.
        after = announcement.first_attempt_at - timedelta(seconds=30)
        async for message in channel.history(limit=None, after=after, oldest_first=False):
            if message.author.id == self.client.user.id and any(embed.footer.text == marker for embed in message.embeds):
                return message.id

        if not permissions.send_messages or not permissions.embed_links:
            raise UserError('Боту нужны права отправлять сообщения и встраивать ссылки в канале анонсов.')
        everyone = announcement.target == 'everyone'
        if everyone:
            if not permissions.mention_everyone:
                raise UserError('Боту нужно право упоминать @everyone в канале анонсов.')
            mention = '@everyone'
            roles = False
        else:
            role = guild.get_role(int(announcement.target))
            check_role(channel, role)
            mention = role.mention
            roles = [role]

        url = f'https://la-family.ru/raffles/{quote(announcement.slug, safe="")}'
        embed = discord.Embed(title=announcement.title, colour=0xd9ac56,
                              description='Запись на розыгрыш открыта. Для участия привяжите Discord к аккаунту портала.')
        embed.set_footer(text=marker)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label='Участвовать на сайте', url=url))
        allowed_mentions = discord.AllowedMentions(everyone=everyone, roles=roles, users=False, replied_user=False)
        nonce = sha256(marker.encode()).hexdigest()[:25]
        check = await self.portal.request('announcement-check', raffleId=announcement.id,
                                          claimId=announcement.claim_id)
        if check.get('ok') is False:
            return None
        if check.get('ok') is not True:
            raise ValueError('Invalid raffle announcement check')
        if self.lease_remaining(announcement) <= 0:
            raise TimeoutError('Raffle announcement lease expired')
        try:
            message = await channel.send(content=mention, embed=embed, view=view,
                                         allowed_mentions=allowed_mentions, nonce=nonce)
        except discord.DiscordException as error:
            # The response can be lost after Discord accepted the message. Keep the original destination for recovery.
            raise RuntimeError('Discord raffle send unconfirmed') from error
        return message.id
