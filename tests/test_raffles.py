import re
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.errors import UserError
from lafamily.modules.raffle_announcements import RaffleAnnouncements
from lafamily.modules.raffles import RaffleButton, RafflePortal, show_raffles


class RaffleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.portal = RafflePortal(111111111111111111)
        self.actor = SimpleNamespace(id=222222222222222222, display_name='Участник', bot=False)
        self.bot_user = SimpleNamespace(id=333333333333333333)
        self.request = SimpleNamespace(client=SimpleNamespace(raffles=self.portal, user=self.bot_user),
            guild_id=self.portal.guild_id, user=self.actor, message=SimpleNamespace(author=self.bot_user),
            response=SimpleNamespace(defer=AsyncMock(), type=discord.InteractionResponseType.deferred_channel_message),
            edit_original_response=AsyncMock())

    async def test_public_list_paginates_and_persistent_button_uses_clicker(self):
        rows = [dict(id=f'raffle-{i}', title=f'Приз {i}') for i in range(6)]
        self.portal.request = AsyncMock(return_value={'raffles': rows})
        await show_raffles(self.request, 2)
        self.request.response.defer.assert_awaited_once_with(thinking=True)
        card = self.request.edit_original_response.await_args.kwargs
        self.assertIn('Приз 5', card['embed'].description)
        self.assertNotIn('Приз 4', card['embed'].description)
        view = card['view']
        self.assertTrue(view.is_persistent())
        button, = view.children
        match = re.fullmatch(RaffleButton.__discord_ui_compiled_template__, button.custom_id)
        restored = await RaffleButton.from_custom_id(self.request, button.item, match)
        second = SimpleNamespace(id=444444444444444444, display_name='Другой', bot=False)
        self.request.user = second
        self.portal.request.return_value = dict(status='ACTIVE', duplicate=False)
        await restored.callback(self.request)
        self.portal.request.assert_awaited_with('join', raffleId='raffle-5',discordId=str(second.id),nickname='Другой')
        self.request.response.defer.assert_awaited_with(thinking=True,ephemeral=True)
        self.assertIn('участвуете', self.request.edit_original_response.await_args.kwargs['content'])

    async def test_repeat_and_moderation_replies(self):
        button = RaffleButton('raffle')
        for status, duplicate, text in [('ACTIVE',True,'уже участвуете'),('PENDING',False,'отправлена на проверку'),
                                         ('PENDING',True,'уже ожидает проверки')]:
            self.portal.request=AsyncMock(return_value=dict(status=status, duplicate=duplicate))
            await button.callback(self.request)
            self.assertIn(text, self.request.edit_original_response.await_args.kwargs['content'])

    async def test_other_guild_bots_and_foreign_messages_cannot_register(self):
        self.portal.request=AsyncMock()
        button=RaffleButton('raffle')
        for guild,bot,author in [(1,False,self.bot_user),(self.portal.guild_id,True,self.bot_user),
                                 (self.portal.guild_id,False,self.actor)]:
            self.request.guild_id=guild
            self.actor.bot=bot
            self.request.message.author=author
            await button.callback(self.request)
            self.portal.request.assert_not_awaited()

    async def test_empty_list_and_closed_card(self):
        self.portal.request=AsyncMock(return_value={'raffles':[]})
        await show_raffles(self.request)
        self.assertIn('нет розыгрышей', self.request.edit_original_response.await_args.kwargs['content'])
        self.portal.request.side_effect=UserError('Регистрация закрыта')
        await RaffleButton('old').callback(self.request)
        self.assertEqual(self.request.edit_original_response.await_args.kwargs['content'],'Регистрация закрыта')

    async def test_http_contract_and_no_redirects(self):
        session = MagicMock()
        post = MagicMock()
        session.return_value.__aenter__.return_value.post = post
        response = post.return_value.__aenter__.return_value
        response.status = 200
        response.json = AsyncMock(return_value={'raffles':[]})
        config=dict(FAMILY_BOT_SYNC_URL='https://example.invalid/api/integrations/family-bot',
                    FAMILY_BOT_SYNC_TOKEN='unit-secret', FAMILY_BOT_GUILD_ID=str(self.portal.guild_id))
        with patch.dict('os.environ',config),patch('lafamily.modules.raffles.aiohttp.ClientSession',session):
            self.assertEqual(await self.portal.request('list'),{'raffles':[]})
            post.assert_called_once_with('https://example.invalid/api/integrations/family-bot/raffles',
                  headers={'Origin':'https://example.invalid','Authorization':'Bearer unit-secret'},
                  json={'action':'list','guildId':str(self.portal.guild_id)}, allow_redirects=False)
            response.status=409
            response.json.return_value={'error':'Приём окончен'}
            with self.assertRaisesRegex(UserError,'Приём окончен'):
                await self.portal.request('join', raffleId='old',discordId=str(self.actor.id),nickname='Ник')
            response.status = 403
            response.json.return_value = {'error': 'Привяжите Discord в личном кабинете'}
            with self.assertRaisesRegex(UserError, 'Привяжите Discord'):
                await self.portal.request('join', raffleId='old', discordId=str(self.actor.id), nickname='Ник')
            response.status=302
            with self.assertRaises(UserError):
                await self.portal.request('list')


class RaffleAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild_id = 111111111111111111
        self.user = SimpleNamespace(id=222222222222222222)
        self.message_id = 333333333333333333
        self.channel_id = 444444444444444444
        self.role_id = 555555555555555555
        self.guild = SimpleNamespace(id=self.guild_id, me=self.user, get_role=MagicMock(),
                                     get_channel=MagicMock(), fetch_channel=AsyncMock())
        self.permissions = discord.Permissions(view_channel=True, read_message_history=True,
                                               send_messages=True, embed_links=True, mention_everyone=True)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.guild = self.guild
        self.channel.permissions_for.return_value = self.permissions
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=self.message_id))
        self.guild.get_channel.return_value = self.channel
        self.role = SimpleNamespace(id=self.role_id, guild=self.guild, is_default=lambda: False,
                                    mention=f'<@&{self.role_id}>', mentionable=True)
        self.guild.get_role.return_value = self.role
        self.client = SimpleNamespace(user=self.user, get_guild=MagicMock(return_value=self.guild))
        self.portal = SimpleNamespace(guild_id=self.guild_id, request=AsyncMock(side_effect=self.request))
        self.service = RaffleAnnouncements(self.client, self.portal)
        self.history = []
        self.channel.history.side_effect = self.channel_history
        claim = datetime.now(UTC).isoformat()
        self.announcement = dict(id='raffle-1', slug='rozygrysh-lafamily', title='Розыгрыш @everyone',
                                 channelId=str(self.channel_id), target=str(self.role_id),
                                 claimId=claim, firstAttemptAt=claim)

    def request(self, action, **fields):
        if action == 'announcement-next':
            return {'announcement': self.announcement}
        if action in ('announcement-check', 'announcement-result'):
            return {'ok': True}
        self.fail(f'Unexpected action: {action}')

    async def channel_history(self, **kwargs):
        for message in self.history:
            yield message

    async def test_role_ping_with_signup_link_and_receipt(self):
        # A role that permits everyone to mention it needs no elevated bot permission.
        self.permissions.mention_everyone = False
        self.assertTrue(await self.service.poll())
        self.guild.get_role.assert_called_once_with(self.role_id)
        data = self.channel.send.await_args.kwargs
        self.assertEqual(data['content'], self.role.mention)
        self.assertEqual(data['allowed_mentions'].to_dict(), {'parse': [], 'roles': [self.role_id]})
        self.assertEqual(data['embed'].title, self.announcement['title'])
        self.assertEqual(data['view'].children[0].url, 'https://la-family.ru/raffles/rozygrysh-lafamily')
        self.assertEqual([call.args[0] for call in self.portal.request.await_args_list],
                         ['announcement-next', 'announcement-check', 'announcement-result'])
        self.portal.request.assert_awaited_with('announcement-result', raffleId='raffle-1',
                                              claimId=self.announcement['claimId'], messageId=str(self.message_id))

    async def test_everyone_and_recovery_after_lost_receipt(self):
        self.announcement['target'] = 'everyone'
        self.portal.request.side_effect = [dict(announcement=self.announcement), {'ok': True},
                                           UserError('Портал недоступен')]
        with self.assertRaises(UserError):
            await self.service.poll()
        data = self.channel.send.await_args.kwargs
        self.assertEqual(data['content'], '@everyone')
        self.assertEqual(data['allowed_mentions'].to_dict(), {'parse': ['everyone']})
        stranger = SimpleNamespace(id=self.user.id + 1)
        self.history = [SimpleNamespace(author=stranger, id=self.message_id + 1, embeds=[data['embed']]),
                        SimpleNamespace(author=self.user, id=self.message_id, embeds=[data['embed']])]
        self.announcement['claimId'] = datetime.now(UTC).isoformat()
        self.portal.request.side_effect = self.request
        restarted = RaffleAnnouncements(self.client, self.portal)
        self.assertTrue(await restarted.poll())
        self.channel.send.assert_awaited_once()
        self.portal.request.assert_awaited_with('announcement-result', raffleId='raffle-1',
                                              claimId=self.announcement['claimId'], messageId=str(self.message_id))

    async def test_everyone_denied_without_channel_permission(self):
        self.announcement['target'] = 'everyone'
        self.permissions.mention_everyone = False
        self.assertTrue(await self.service.poll())
        self.channel.send.assert_not_awaited()
        self.assertIn('@everyone', self.portal.request.await_args.kwargs['error'])
        self.assertNotIn('messageId', self.portal.request.await_args.kwargs)

    async def test_other_guild_channel_is_rejected_before_history(self):
        self.channel.guild = SimpleNamespace(id=self.guild_id + 1)
        self.assertTrue(await self.service.poll())
        self.channel.history.assert_not_called()
        self.channel.send.assert_not_awaited()
        self.assertIn('error', self.portal.request.await_args.kwargs)

    async def test_expired_claim_is_skipped(self):
        expired = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
        self.announcement.update(claimId=expired, firstAttemptAt=expired)
        self.assertFalse(await self.service.poll())
        self.channel.send.assert_not_awaited()
        self.portal.request.assert_awaited_once_with('announcement-next')

    async def test_raffle_closed_after_claim_is_skipped(self):
        self.portal.request.side_effect = [dict(announcement=self.announcement), {'ok': False}]
        self.assertFalse(await self.service.poll())
        self.channel.send.assert_not_awaited()
        self.portal.request.assert_awaited_with('announcement-check', raffleId='raffle-1',
                                              claimId=self.announcement['claimId'])

    async def test_send_failure_leaves_claim_unconfirmed(self):
        self.channel.send.side_effect = discord.DiscordException('response lost')
        with self.assertRaisesRegex(RuntimeError, 'send unconfirmed'):
            await self.service.poll()
        self.assertEqual([call.args[0] for call in self.portal.request.await_args_list],
                         ['announcement-next', 'announcement-check'])
