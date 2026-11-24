import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.errors import UserError
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
            response.status=302
            with self.assertRaises(UserError):
                await self.portal.request('list')
