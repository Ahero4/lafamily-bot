import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from lafamily.async_database import DatabaseWorker
from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.reaction_subscriptions import parse_emoji, react_to_new_message

GUILD = 111111111111111111
PLAYER = 222222222222222222


def new_message(guild_id=GUILD, author_id=PLAYER, webhook=None):
    return SimpleNamespace(guild=SimpleNamespace(id=guild_id), author=SimpleNamespace(id=author_id, bot=False),
                           webhook_id=webhook, channel=SimpleNamespace(id=3), add_reaction=AsyncMock())


class ReactionSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def test_only_one_complete_emoji_can_be_saved(self):
        for emoji in ('🐰', '❤️', '👩🏽‍💻', '🇷🇺', '1️⃣', '🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f',
                      '<a:rabbit:999999999999999999>'):
            with self.subTest(valid=emoji):
                self.assertEqual(str(parse_emoji(emoji, subscription=True)), emoji)
        for invalid in ('', 'привет', '🐰🐰', '🐰\u200d', '🇷', '<:bad:999999999999999999', 'rabbit:999999999999999999!'):
            with self.subTest(invalid=invalid), self.assertRaises(UserError):
                parse_emoji(invalid, subscription=True)

    async def test_reopen_database_and_filter_new_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite'
            async with DatabaseWorker(path) as worker:
                await worker.call('save_reaction_subscription', str(GUILD), str(PLAYER), '🐰')
            async with DatabaseWorker(path) as worker:
                for guild, author, webhook in [(GUILD + 1, PLAYER, None), (GUILD, PLAYER + 1, None), (GUILD, PLAYER, 10)]:
                    message = new_message(guild, author, webhook)
                    await react_to_new_message(message, worker, GUILD)
                    message.add_reaction.assert_not_awaited()
                message = new_message()
                await react_to_new_message(message, worker, GUILD)
                self.assertEqual(str(message.add_reaction.await_args.args[0]), '🐰')

    async def test_private_management_and_live_dispatch(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        bot = FamilyBot(Config('fake', 42, GUILD, Path(':memory:')), store)
        self.addAsyncCleanup(bot.close)
        group = bot.tree.get_command('реакции', guild=discord.Object(id=GUILD))
        manage = group.get_command('подписка').callback
        guild = SimpleNamespace(id=GUILD)
        request = SimpleNamespace(guild=guild, guild_id=GUILD, permissions=discord.Permissions.none(),
                                  response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        player = SimpleNamespace(id=PLAYER, guild=guild, bot=False)
        with self.assertRaises(UserError):
            await manage(request, 'сохранить', player, '🐰')
        request.permissions = discord.Permissions(administrator=True)
        await manage(request, 'сохранить', player, '🐰')
        await manage(request, 'сохранить', реакция='<:rabbit:999999999999999999>', id_аккаунта=str(PLAYER))
        await manage(request)
        request.response.defer.assert_awaited_with(ephemeral=True, thinking=True)
        text = request.edit_original_response.await_args.kwargs['content']
        self.assertIn('1/1', text)
        self.assertIn(f'`{PLAYER}`', text)
        self.assertIn('<:rabbit:999999999999999999>', text)
        bot.prices.handle = AsyncMock()
        message = new_message()
        await bot.on_message(message)
        bot.prices.handle.assert_awaited_once_with(message)
        self.assertEqual(message.add_reaction.await_args.args[0].id, 999999999999999999)
        await manage(request, 'удалить', id_аккаунта=str(PLAYER))
        message.add_reaction.reset_mock()
        await bot.on_message(message)
        message.add_reaction.assert_not_awaited()
