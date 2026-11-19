import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.menu import MenuLauncher, OPEN_ID


class FamilyMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        self.bot = FamilyBot(Config('fake', 1, 2, Path(':memory:')), store)
        self.addAsyncCleanup(self.bot.close)
        self.request = SimpleNamespace(guild_id=2, guild=SimpleNamespace(id=2), user=SimpleNamespace(id=3),
            permissions=discord.Permissions.none(), response=SimpleNamespace(send_message=AsyncMock(),
            edit_message=AsyncMock(), defer=AsyncMock()), edit_original_response=AsyncMock())

    async def test_public_button_opens_private_view_and_real_personal_garden(self):
        launcher = MenuLauncher(self.bot.menu)
        self.assertTrue(launcher.is_persistent())
        self.assertEqual(launcher.children[0].custom_id, OPEN_ID)
        await launcher.open.callback(self.request)
        payload = self.request.response.send_message.await_args.kwargs
        self.assertTrue(payload['ephemeral'])
        view = payload['view']
        self.assertNotIn('Администрирование', [b.label for b in view.children])
        garden = next(b for b in view.children if b.label == 'Сад')
        await garden.callback(self.request)
        self.request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        card = self.request.edit_original_response.await_args.kwargs
        self.assertEqual(card['view'].owner, 3)
        self.assertTrue(card['embed'].fields)
        self.request.response.edit_message.assert_not_awaited()
        self.request.user.id = 4
        with self.assertRaises(UserError):
            await view.interaction_check(self.request)

    async def test_private_admin_navigation_rechecks_rights_and_restore(self):
        self.request.permissions = discord.Permissions(administrator=True)
        cmd = self.bot.tree.get_command('меню', guild=discord.Object(id=2))
        await cmd.callback(self.request)
        home = self.request.response.send_message.await_args.kwargs['view']
        await next(b for b in home.children if b.label == 'Администрирование').callback(self.request)
        admin = self.request.response.edit_message.await_args.kwargs['view']
        self.request.permissions = discord.Permissions.none()
        with self.assertRaises(UserError):
            await admin.interaction_check(self.request)
        self.bot.backups.restoring = True
        with self.assertRaises(UserError):
            await self.bot.menu.open(self.request)

    async def test_publication_reuses_pinned_card(self):
        self.request.permissions = discord.Permissions(administrator=True)
        channel = Mock(spec=discord.TextChannel)
        channel.id = 12
        channel.guild = self.request.guild
        self.request.guild.me = object()
        self.request.guild.fetch_channel = AsyncMock(return_value=channel)
        channel.permissions_for.return_value = discord.Permissions.all()
        self.bot._connection.user = SimpleNamespace(id=10)
        pinned = []
        async def pins(**kwargs):
            for message in pinned:
                yield message
        channel.pins.side_effect = pins
        posted = SimpleNamespace(author=self.bot.user, jump_url='https://discord.com/channels/2/12/4',
                                 components=[SimpleNamespace(children=[SimpleNamespace(custom_id=OPEN_ID)])])
        posted.pin = AsyncMock(side_effect=lambda **kwargs: pinned.append(posted))
        channel.send = AsyncMock(return_value=posted)
        for _ in range(2):
            await self.bot.menu.publish(self.request, 12)
        channel.send.assert_awaited_once()
        posted.pin.assert_awaited_once()
        self.assertIn('уже закреплена', self.request.edit_original_response.await_args.kwargs['content'])

        pinned.clear()
        posted.pin.side_effect = OSError('connection lost')
        with self.assertRaisesRegex(UserError, 'Закрепление не подтверждено') as failure:
            await self.bot.menu.publish(self.request, 12)
        self.assertIn(posted.jump_url, str(failure.exception))
        self.assertEqual(channel.send.await_count, 2)
