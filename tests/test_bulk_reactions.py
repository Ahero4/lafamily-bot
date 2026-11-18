import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from lafamily.modules.bulk_reactions import Result, apply_reactions


async def items(*values):
    for value in values:
        yield value


class BulkReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_history_and_processes_each_thread_once(self):
        player = 11
        def message(author, reactions=()):
            return SimpleNamespace(author=SimpleNamespace(id=author), reactions=reactions, add_reaction=AsyncMock())
        already = message(player, [SimpleNamespace(emoji='❤', me=True)])
        other, target = message(22), message(player)
        thread = Mock(spec=discord.Thread)
        thread.id = 1
        thread.history.return_value = items(other, already, target)
        thread.permissions_for.return_value = discord.Permissions.all()
        forum = Mock(spec=discord.ForumChannel)
        forum.permissions_for.return_value = discord.Permissions.all()
        forum.archived_threads.return_value = items(thread)
        stale = Mock(spec=discord.Thread)
        stale.id = 2
        stale.permissions_for.side_effect = discord.ClientException('Parent channel not found')
        guild = SimpleNamespace(active_threads=AsyncMock(return_value=[stale, thread]), channels=[forum], me=object())
        result = Result()
        emoji = discord.PartialEmoji.from_str('❤️')
        before = discord.Object(id=55)
        await apply_reactions(guild, player, emoji, before, result)
        self.assertEqual(result, Result(added=1, existing=1, unavailable=1))
        thread.history.assert_called_once_with(limit=None, before=before)
        forum.archived_threads.assert_called_once_with(limit=None)
        target.add_reaction.assert_awaited_once_with(emoji)
        other.add_reaction.assert_not_awaited()
        already.add_reaction.assert_not_awaited()
