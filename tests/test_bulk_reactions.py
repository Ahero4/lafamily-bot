import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from lafamily.errors import UserError
from lafamily.modules.bulk_reactions import Result, apply_reactions


def hit(message_id, player=11, **extra):
    return {'id': str(message_id), 'channel_id': '444', 'author': {'id': str(player)}, **extra}


class BulkReactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(spec=discord.Client)
        self.client.http = SimpleNamespace(request=AsyncMock())
        self.channel = self.client.get_partial_messageable.return_value
        self.add = self.channel.get_partial_message.return_value.add_reaction = AsyncMock()
        self.emoji = discord.PartialEmoji.from_str('🐰')
        self.result = Result()

    async def run_search(self, *pages):
        self.client.http.request.side_effect = pages
        await apply_reactions(self.client, 123, 11, self.emoji, 100, self.result)

    async def test_author_search_pages_by_id_and_only_reacts_to_unique_matching_results(self):
        await self.run_search(
            {'messages': [[hit(99), hit(80, player=12)], [hit(99), hit(98), hit(60, hit=False)],
                          [hit(1, guild_id='555'), hit(105)]], 'total_results': 10001},
            {'messages': [[hit(97)]], 'total_results': 1, 'doing_deep_historical_index': True},
            {'messages': [], 'total_results': 0})
        calls = self.client.http.request.await_args_list
        self.assertEqual([c.kwargs['params']['max_id'] for c in calls], ['100', '98', '97'])
        for c in calls:
            route, = c.args
            self.assertEqual((route.method, route.url.split('/api/v10')[-1]), ('GET', '/guilds/123/messages/search'))
            self.assertEqual(c.kwargs['params']['author_id'], ['11'])
            self.assertEqual(c.kwargs['params']['include_nsfw'], 'true')
        self.assertEqual([c.args[0] for c in self.channel.get_partial_message.call_args_list], [99, 98, 97])
        self.assertTrue(all(c.args == (self.emoji,) for c in self.add.await_args_list))
        self.assertEqual(self.result, Result(confirmed=3, indexing=True))

    async def test_index_retry_and_partial_failure(self):
        missing = discord.NotFound(SimpleNamespace(status=404, reason='Gone'), {'code': 10008, 'message': 'Deleted'})
        self.add.side_effect = [missing, None]
        with patch('lafamily.modules.bulk_reactions.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.run_search({'code': 110000, 'retry_after': 2},
                                  {'messages': [[hit(80)], [hit(79)]]}, {'messages': []})
        sleep.assert_awaited_once_with(2)
        self.assertEqual(self.result, Result(confirmed=1, failed=1))

    async def test_unavailable_search_preserves_completed_work(self):
        denied = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Forbidden')
        with self.assertRaisesRegex(UserError, 'запретил поиск'):
            await self.run_search({'messages': [[hit(70)]]}, denied)
        self.assertEqual(self.result, Result(confirmed=1))
