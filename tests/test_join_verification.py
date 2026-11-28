from datetime import UTC, datetime, timedelta
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

import discord

from lafamily.errors import UserError
from lafamily.modules.join_verification import JoinConfirmButton, JoinVerification


GUILD_ID = 111111111111111111
PLAYER_ID = 222222222222222222
BOT_ID = 333333333333333333


class JoinVerificationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = SimpleNamespace(send=AsyncMock(), history=MagicMock())
        self.member = SimpleNamespace(
            id=PLAYER_ID, name='novarole', discriminator='0', bot=False, create_dm=AsyncMock(return_value=self.channel),
        )
        self.guild = SimpleNamespace(
            query_members=AsyncMock(return_value=[self.member]),
            fetch_member=AsyncMock(return_value=self.member),
        )
        self.client = SimpleNamespace(get_guild=MagicMock(return_value=self.guild), user=SimpleNamespace(id=BOT_ID))
        self.service = JoinVerification(self.client, GUILD_ID)
        self.client.join_verification = self.service
        self.now = datetime.now(UTC)
        self.application = dict(
            id='test-join', discord='@NovaRole', discordId=None, gameNick='Test_Player', gameId='42',
            claimId=self.now.isoformat(), createdAt=self.now.isoformat(),
        )

    def mock_request(self, *responses):
        self.service.portal.request = AsyncMock(side_effect=responses)
        return self.service.portal.request

    def assert_delivery(self, request, status):
        request.assert_awaited_with('delivery', applicationId='test-join',
                                    claimId=self.application['claimId'], status=status)

    async def test_resolves_exact_login_before_dm(self):
        near = SimpleNamespace(id=PLAYER_ID + 1, name='different', display_name='novarole', discriminator='0', bot=False)
        self.guild.query_members.return_value = [near, self.member]
        request = self.mock_request(dict(application=self.application), dict(ok=True), dict(ok=True), dict(ok=True))
        checks = []

        async def send(**kwargs):
            checks.append(request.await_args)

        self.channel.send.side_effect = send
        self.assertTrue(await self.service.poll())
        self.guild.query_members.assert_awaited_once_with('novarole', limit=100, cache=False)
        self.guild.fetch_member.assert_awaited_once_with(PLAYER_ID)
        self.assertEqual(checks, [call('resolve', applicationId='test-join',
                                       claimId=self.application['claimId'], discordId=str(PLAYER_ID))])
        message = self.channel.send.await_args.kwargs
        self.assertIn('https://la-family.ru/join', message['embed'].description)
        self.assertEqual(message['view'].children[0].custom_id, 'lafjoin:test-join')
        self.assertEqual(message['allowed_mentions'].to_dict(), {'parse': []})
        self.assert_delivery(request, 'sent')

    async def test_refuses_expired_lease_and_failed_binding(self):
        expired = (self.now - timedelta(seconds=121)).isoformat()
        request = self.mock_request(dict(application=dict(self.application, claimId=expired, createdAt=expired)))
        self.assertFalse(await self.service.poll())
        request.assert_awaited_once_with('claim')
        self.guild.query_members.assert_not_awaited()

        request = self.mock_request(dict(application=self.application), dict(ok=False))
        self.assertFalse(await self.service.poll())
        self.assertEqual(request.await_count, 2)
        self.member.create_dm.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_does_not_send_when_application_is_replaced_after_opening_dm(self):
        request = self.mock_request(dict(application=self.application), dict(ok=True), dict(ok=False))
        self.assertFalse(await self.service.poll())
        self.assertEqual([event.args[0] for event in request.await_args_list], ['claim', 'resolve', 'resolve'])
        self.member.create_dm.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_only_exact_legacy_tag_and_current_name_are_accepted(self):
        self.application.update(discord='NovaRole#1234')
        candidate = SimpleNamespace(id=PLAYER_ID, name='NovaRole', discriminator='1234', bot=False)
        self.guild.query_members.return_value = [candidate]
        self.member.name = 'Renamed'
        request = self.mock_request(dict(application=self.application), dict(ok=True))
        self.assertTrue(await self.service.poll())
        self.guild.query_members.assert_awaited_once_with('NovaRole', limit=100, cache=False)
        self.assertEqual(request.await_count, 2)
        self.assert_delivery(request, 'not_found')
        self.member.create_dm.assert_not_awaited()

    async def test_closed_dm_reported_but_unrelated_forbidden_is_retried(self):
        response = SimpleNamespace(status=403, reason='Forbidden')
        self.channel.send.side_effect = discord.Forbidden(response, {'code': 50007, 'message': 'Cannot send'})
        request = self.mock_request(dict(application=self.application), dict(ok=True), dict(ok=True), dict(ok=True))
        self.assertTrue(await self.service.poll())
        self.assert_delivery(request, 'dm_closed')

        self.channel.send.side_effect = discord.Forbidden(response, {'code': 50013, 'message': 'Missing permissions'})
        request = self.mock_request(dict(application=self.application), dict(ok=True), dict(ok=True))
        with self.assertRaises(discord.Forbidden):
            await self.service.poll()
        self.assertEqual(request.await_count, 3)

    async def test_recovers_sent_dm_without_sending_twice(self):
        self.application.update(discordId=str(PLAYER_ID))
        self.member.name = 'renamed'
        embed = discord.Embed()
        embed.set_footer(text='LaFamily · Заявка test-join')

        async def history(**kwargs):
            yield SimpleNamespace(author=SimpleNamespace(id=PLAYER_ID), embeds=[embed])
            yield SimpleNamespace(author=self.client.user, embeds=[embed])

        self.channel.history.side_effect = history
        request = self.mock_request(dict(application=self.application), dict(ok=True), dict(ok=True))
        self.assertTrue(await self.service.poll())
        self.channel.history.assert_called_once()
        self.guild.query_members.assert_not_awaited()
        self.guild.fetch_member.assert_awaited_once_with(PLAYER_ID)
        self.channel.send.assert_not_awaited()
        self.assert_delivery(request, 'sent')

    async def test_callback_restores_button_and_reports_portal_result(self):
        pattern = JoinConfirmButton.__discord_ui_compiled_template__
        match = re.fullmatch(pattern, 'lafjoin:old-application')
        interaction = SimpleNamespace(
            guild_id=None, channel=SimpleNamespace(type=discord.ChannelType.private),
            user=SimpleNamespace(id=PLAYER_ID, bot=False), message=SimpleNamespace(author=self.client.user),
            client=self.client, response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        button = await JoinConfirmButton.from_custom_id(interaction, None, match)
        request = self.mock_request(dict(ok=True), dict(ok=True, duplicate=True), dict(ok=False), UserError('Unavailable'))
        interaction.guild_id = GUILD_ID
        await button.callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        request.assert_not_awaited()
        interaction.guild_id = None
        for message in ('Discord подтверждён', 'уже подтверждена', 'недоступно', 'чуть позже'):
            await button.callback(interaction)
            request.assert_awaited_with('confirm', applicationId='old-application', discordId=str(PLAYER_ID))
            self.assertIn(message, interaction.edit_original_response.await_args.kwargs['content'])

    async def test_portal_transport_preserves_authentication_and_disallows_redirects(self):
        session = MagicMock()
        post = MagicMock()
        session.return_value.__aenter__.return_value.post = post
        response = post.return_value.__aenter__.return_value
        response.status = 200
        response.json = AsyncMock(return_value={'application': None})
        config = dict(FAMILY_BOT_SYNC_URL='https://example.invalid/api/integrations/family-bot',
                      FAMILY_BOT_SYNC_TOKEN='unit-secret', FAMILY_BOT_GUILD_ID=str(GUILD_ID))
        with patch.dict('os.environ', config), patch('lafamily.portal_endpoint.aiohttp.ClientSession', session):
            self.assertFalse(await self.service.poll())
            post.assert_called_once_with(
                'https://example.invalid/api/integrations/family-bot/join',
                headers={'Origin': 'https://example.invalid', 'Authorization': 'Bearer unit-secret'},
                json={'action': 'claim', 'guildId': str(GUILD_ID)}, allow_redirects=False,
            )
            response.status = 302
            with self.assertRaises(UserError):
                await self.service.poll()
