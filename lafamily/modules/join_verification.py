"""Доставка подтверждения анкеты портала и обработка кнопки в Discord."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import logging
import re

import discord

from lafamily.errors import UserError
from lafamily.portal_endpoint import PortalEndpoint


logger = logging.getLogger(__name__)
APPLICATION_ID = r'[A-Za-z0-9_-]{1,64}'
DISCORD_TAG = re.compile(r'@?(?P<name>[A-Za-z0-9_.]{2,32})(?:#(?P<discriminator>[0-9]{4}))?')
SNOWFLAKE = re.compile(r'[0-9]{17,20}')
POLL_INTERVAL = 5
LEASE_SECONDS = 120
DELIVERY_SECONDS = 60


def portal_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError('Expected UTC timestamp from portal')
    return parsed


@dataclass(frozen=True, slots=True)
class JoinApplication:
    id: str
    username: str
    discriminator: str | None
    discord_id: int | None
    game_nick: str
    game_id: str
    claim_id: str
    claimed_at: datetime
    created_at: datetime

    @classmethod
    def parse(cls, data):
        keys = ('id', 'discord', 'gameNick', 'gameId', 'claimId', 'createdAt')
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in keys):
            raise ValueError('Invalid join application')
        app_id, tag, game_nick, game_id, claim_id, created_at = (data[key] for key in keys)
        tag_match = DISCORD_TAG.fullmatch(tag)
        discord_id = data['discordId']
        if (not re.fullmatch(APPLICATION_ID, app_id) or tag_match is None
                or not game_nick.strip() or len(game_nick) > 40
                or not re.fullmatch(r'[0-9A-Za-z_-]{1,20}', game_id)
                or (discord_id is not None and
                    (not isinstance(discord_id, str) or not SNOWFLAKE.fullmatch(discord_id)))):
            raise ValueError('Invalid join application fields')
        created = portal_time(created_at)
        claimed = portal_time(claim_id)
        if created > claimed:
            raise ValueError('Invalid join application lease')
        return cls(app_id, tag_match['name'], tag_match['discriminator'], int(discord_id) if discord_id else None,
                   game_nick, game_id, claim_id, claimed, created)

    def matches(self, member):
        if member.bot:
            return False
        if self.discriminator is not None:
            return member.name == self.username and member.discriminator == self.discriminator
        return member.discriminator == '0' and member.name == self.username.lower()

    def lease_remaining(self):
        return LEASE_SECONDS - (datetime.now(UTC) - self.claimed_at).total_seconds() - 10


class JoinConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=rf'lafjoin:(?P<id>{APPLICATION_ID})'):
    def __init__(self, application_id):
        self.application_id = application_id
        super().__init__(discord.ui.Button(label='Подтвердить', custom_id=f'lafjoin:{application_id}',
                                           style=discord.ButtonStyle.success))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['id'])

    async def callback(self, interaction):
        if (interaction.guild_id is not None
                or getattr(interaction.channel, 'type', None) is not discord.ChannelType.private
                or interaction.user.bot or interaction.message is None or interaction.client.user is None
                or interaction.message.author.id != interaction.client.user.id):
            await interaction.response.send_message('Откройте личное сообщение от LaFamily Bot.', ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            result = await interaction.client.join_verification.portal.request(
                'confirm', applicationId=self.application_id, discordId=str(interaction.user.id),
            )
        except (UserError, ValueError):
            message = 'Не удалось связаться с la-family.ru. Нажмите «Подтвердить» чуть позже.'
        else:
            if result.get('ok') is True:
                message = ('Заявка уже подтверждена.' if result.get('duplicate') is True
                           else 'Discord подтверждён и привязан к заявке на вступление.')
            else:
                message = 'Подтверждение этой заявки недоступно. Проверьте её статус на la-family.ru.'
        await interaction.edit_original_response(content=message, allowed_mentions=discord.AllowedMentions.none())


def embed_value(value):
    # Кодовый span не превращает введённые ссылки и упоминания в кликабельные элементы.
    clean = ''.join('ˋ' if char == '`' else char if char.isprintable() else ' ' for char in value)
    return f'`{clean.strip()}`'


class JoinVerification:
    def __init__(self, client, guild_id):
        self.client = client
        self.portal = PortalEndpoint(guild_id, 'join', 'Подтверждение заявок с портала не настроено.')
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
                if self.client.is_ready():
                    delay = 1 if await self.poll() else POLL_INTERVAL
                else:
                    delay = POLL_INTERVAL
            except Exception as error:
                logger.warning('Подтверждение заявки отложено (%s)', type(error).__name__)
                delay = min(max(POLL_INTERVAL, delay * 2), 120)
            await asyncio.sleep(delay)

    async def poll(self):
        response = await self.portal.request('claim')
        data = response['application']
        if data is None:
            return False
        application = JoinApplication.parse(data)
        remaining = application.lease_remaining()
        if remaining <= 0:
            return False
        async with asyncio.timeout(min(DELIVERY_SECONDS, remaining)):
            status = await self.deliver(application)
        if status is None or application.lease_remaining() <= 0:
            return False
        result = await self.portal.request('delivery', applicationId=application.id,
                                           claimId=application.claim_id, status=status)
        if not isinstance(result.get('ok'), bool):
            raise ValueError('Invalid delivery response from portal')
        return result['ok']

    async def recipient(self, guild, application):
        discord_id = application.discord_id
        if discord_id is None:
            query = application.username if application.discriminator else application.username.lower()
            candidates = await guild.query_members(query, limit=100, cache=False)
            matches = {member.id for member in candidates if application.matches(member)}
            if len(matches) != 1:
                return None
            discord_id = matches.pop()
        try:
            member = await guild.fetch_member(discord_id)
        except discord.NotFound:
            return None
        # Имя могло измениться между поиском и получением участника по ID.
        if member.bot or (application.discord_id is None and not application.matches(member)):
            return None
        return member

    async def resolve_recipient(self, application, member_id):
        if application.lease_remaining() <= 0:
            return False
        result = await self.portal.request('resolve', applicationId=application.id, claimId=application.claim_id,
                                           discordId=str(member_id))
        if not isinstance(result.get('ok'), bool):
            raise ValueError('Invalid resolve response from portal')
        return result['ok'] and application.lease_remaining() > 0

    async def deliver(self, application):
        guild = self.client.get_guild(self.portal.guild_id)
        if guild is None or self.client.user is None:
            raise TimeoutError('Discord guild unavailable')
        member = await self.recipient(guild, application)
        if member is None:
            return 'not_found'
        if not await self.resolve_recipient(application, member.id):
            return None
        marker = f'LaFamily · Заявка {application.id}'
        try:
            channel = await member.create_dm()
            if application.discord_id is not None:
                # Discord мог принять отправку до того, как портал получил ответ.
                since = application.created_at - timedelta(seconds=30)
                async for message in channel.history(limit=None, after=since):
                    if (message.author.id == self.client.user.id
                            and any(embed.footer.text == marker for embed in message.embeds)):
                        return 'sent'
            embed = discord.Embed(
                title='Привязка Discord к заявке',
                description='Нажмите «Подтвердить», если вы запросили привязку к своей заявке на https://la-family.ru/join.',
                colour=0xd9ac56,
            )
            embed.add_field(name='Игровой ник', value=embed_value(application.game_nick))
            embed.add_field(name='Игровой ID (статик)', value=embed_value(application.game_id))
            embed.set_footer(text=marker)
            view = discord.ui.View(timeout=None)
            view.add_item(JoinConfirmButton(application.id))
            nonce = sha256(f'lafjoin:{application.id}'.encode()).hexdigest()[:25]
            # Заявку могли заменить или просрочить, пока Discord открывал ЛС и возвращал историю.
            if not await self.resolve_recipient(application, member.id):
                return None
            await channel.send(embed=embed, view=view, nonce=nonce, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden as error:
            if error.code == 50007:
                return 'dm_closed'
            raise
        return 'sent'
