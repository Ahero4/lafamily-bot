import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.release_updates import ReleaseUpdates, TITLE_PREFIX
from lafamily.release import VERSION, RELEASE_NOTES, LATEST_UPDATE, parse_releases

G = 111111111111111111


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:'); self.addCleanup(self.store.close)
        self.guild = SimpleNamespace(id=G, me=object())
        self.releases = {'2.2.1': 'First', '2.2.2': 'Second', '2.2.3': 'Third'}
        self.patch = patch('lafamily.modules.release_updates.RELEASES', self.releases)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.messages = []
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 123; self.channel.guild = self.guild; self.channel.mention = '<#123>'
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
        self.channel.history.side_effect = self.history
        self.channel.send = AsyncMock(side_effect=self.send)
        self.client = SimpleNamespace(fetch_channel=AsyncMock(return_value=self.channel), user=SimpleNamespace(id=42))
        self.service = ReleaseUpdates(self.client, self.store, G)

    async def history(self, **kwargs):
        for message in list(self.messages):
            yield message

    def post(self, version, author=42):
        message = SimpleNamespace(id=456+len(self.messages), author=SimpleNamespace(id=author),
                                  embeds=[discord.Embed(title=TITLE_PREFIX+version)])
        self.messages.append(message)
        return message

    async def send(self, **kwargs):
        version = kwargs['embed'].title[len(TITLE_PREFIX):]
        return self.post(version)

    async def test_no_channel_does_not_send(self):
        await self.service.announce()
        self.client.fetch_channel.assert_not_awaited()

    async def test_empty_channel_all_versions_in_order_and_no_repeats(self):
        self.store.configure_release_channel(str(G), '1', '123')
        await asyncio.gather(self.service.announce(), self.service.announce())
        await ReleaseUpdates(self.client, self.store, G).announce()
        self.assertEqual([m.embeds[0].title for m in self.messages], [TITLE_PREFIX+v for v in self.releases])
        self.assertEqual(self.channel.send.await_count, 3)
        self.assertTrue(all(c.kwargs['allowed_mentions'].to_dict() == {'parse': []} for c in self.channel.send.call_args_list))

    async def test_existing_versions_without_db_state_are_skipped(self):
        self.post('2.2.1'); self.post('2.2.2')
        self.store.configure_release_channel(str(G), '1', '123')
        await self.service.announce()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.call_args.kwargs['embed'].title, TITLE_PREFIX+'2.2.3')

    async def test_other_authors_do_not_count_and_deleted_posts_are_restored(self):
        self.post('2.2.1', author=999)
        self.store.configure_release_channel(str(G), '1', '123')
        await self.service.announce()
        self.assertEqual(self.channel.send.await_count, 3)
        self.messages = [m for m in self.messages if m.embeds[0].title != TITLE_PREFIX+'2.2.2']
        await self.service.announce()
        self.assertEqual(self.channel.send.await_count, 4)

    async def test_new_channel_has_independent_history(self):
        self.store.configure_release_channel(str(G), '1', '123')
        await self.service.announce()
        self.messages.clear(); self.channel.id = 789
        self.store.configure_release_channel(str(G), '1', '789')
        await self.service.announce()
        self.assertEqual(self.channel.send.await_count, 6)

    async def test_ambiguous_failure_blocked_then_recovered_from_history(self):
        self.store.configure_release_channel(str(G), '1', '123')
        self.channel.send.side_effect = RuntimeError('network')
        with self.assertLogs('lafamily.errors', level='ERROR'):
            await self.service.announce()
        state = self.store.db.execute('SELECT state FROM release_deliveries').fetchone()[0]
        self.assertEqual(state, 'uncertain')
        self.channel.send.side_effect = self.send
        with self.assertLogs('lafamily.errors', level='ERROR'):
            await self.service.announce()
        self.assertEqual(self.channel.send.await_count, 3)  # ошибка + два оставшихся выпуска
        self.post('2.2.1')  # отправка всё же дошла, теперь её видно в истории
        await self.service.announce()
        self.assertEqual(self.channel.send.await_count, 3)

    async def test_confirmed_http_rejection_allows_retry_after_fix(self):
        with patch.dict(self.releases, {'2.2.1': 'First'}, clear=True):
            for status in (400, 401, 403, 404):
                with self.subTest(status=status):
                    self.channel.id = 1000 + status
                    self.channel.send.reset_mock()
                    self.channel.send.side_effect = discord.HTTPException(
                        SimpleNamespace(status=status, reason='Rejected'), 'Rejected')
                    with self.assertLogs('lafamily.errors', level='ERROR'):
                        with self.assertRaises(UserError) as caught:
                            await self.service._publish(self.channel, {})
                    self.assertIn('повтор', str(caught.exception).lower())
                    row = self.store.db.execute('SELECT state FROM release_deliveries WHERE channel_id=?',
                                                (str(self.channel.id),)).fetchone()
                    self.assertIsNone(row, 'Confirmed rejection must release only its pending claim')
                    self.channel.send.side_effect = self.send
                    result = await self.service._publish(self.channel, {})
                    self.assertEqual(result['published'], ['2.2.1'])
                    self.assertEqual(result['blocked'], [])
                    self.assertEqual(self.channel.send.await_count, 2)

    async def test_timeout_and_ambiguous_http_failure_remain_blocked(self):
        errors = [TimeoutError('response lost')] + [discord.HTTPException(
            SimpleNamespace(status=status, reason='Unknown'), 'Unknown') for status in (408, 429, 500)]
        with patch.dict(self.releases, {'2.2.1': 'First'}, clear=True):
            for index, error in enumerate(errors):
                with self.subTest(error=repr(error)):
                    self.channel.id = 2000 + index
                    self.channel.send.reset_mock()
                    self.channel.send.side_effect = error
                    with self.assertLogs('lafamily.errors', level='ERROR'):
                        with self.assertRaises(UserError):
                            await self.service._publish(self.channel, {})
                    row = self.store.db.execute('SELECT state FROM release_deliveries WHERE channel_id=?',
                                                (str(self.channel.id),)).fetchone()
                    self.assertEqual(row['state'], 'uncertain')
                    self.channel.send.side_effect = self.send
                    result = await self.service._publish(self.channel, {})
                    self.assertEqual(result['blocked'], ['2.2.1'])
                    self.assertEqual(self.channel.send.await_count, 1)

    async def test_post_send_save_failure_never_releases_claim(self):
        errors = [OSError('disk full'), discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'Not a send rejection')]
        with patch.dict(self.releases, {'2.2.1': 'First'}, clear=True):
            for index, error in enumerate(errors):
                with self.subTest(error=repr(error)):
                    self.channel.id = 3000 + index
                    self.channel.send.reset_mock()
                    with patch.object(self.store, 'save_release_delivery', side_effect=error):
                        with self.assertLogs('lafamily.errors', level='ERROR'):
                            with self.assertRaises(UserError):
                                await self.service._publish(self.channel, {})
                    row = self.store.db.execute('SELECT state FROM release_deliveries WHERE channel_id=?',
                                                (str(self.channel.id),)).fetchone()
                    self.assertEqual(row['state'], 'uncertain')
                    result = await self.service._publish(self.channel, {})
                    self.assertEqual(result['blocked'], ['2.2.1'])
                    self.assertEqual(self.channel.send.await_count, 1)

    async def test_history_failure_and_permissions_do_not_send(self):
        self.store.configure_release_channel(str(G), '1', '123')
        self.channel.permissions_for.return_value.read_message_history = False
        with self.assertLogs('lafamily.errors', level='ERROR'):
            await self.service.announce()
        self.channel.permissions_for.return_value.read_message_history = True
        async def partial(**kwargs):
            yield self.post('2.2.1')
            raise RuntimeError('incomplete history')
        self.channel.history.side_effect = partial
        with self.assertLogs('lafamily.errors', level='ERROR'):
            await self.service.announce()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM release_deliveries').fetchone()[0], 0)

    async def test_command_only_admin_and_immediately_fills_channel(self):
        bot = FamilyBot(Config('token', 123, G, Path(':memory:')), self.store)
        self.addAsyncCleanup(bot.close)
        bot.release_updates.client = self.client
        command = bot.tree.get_command('бот', guild=discord.Object(id=G)).get_command('обновления')
        interaction = SimpleNamespace(guild_id=G, guild=self.guild, user=SimpleNamespace(id=1, roles=[]),
            permissions=discord.Permissions.none(), response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock())
        with self.assertRaises(UserError): await command.callback(interaction, self.channel)
        self.assertIsNone(self.store.release_channel(str(G)))
        interaction.permissions = discord.Permissions(administrator=True)
        await command.callback(interaction, self.channel)
        self.assertEqual(self.channel.send.await_count, 3)
        self.assertIn('Опубликовано: 3', interaction.edit_original_response.call_args.kwargs['content'])
        await command.callback(interaction, self.channel)
        self.assertEqual(self.channel.send.await_count, 3)
        self.channel.guild = SimpleNamespace(id=999, me=object())
        with self.assertRaises(UserError): await command.callback(interaction, self.channel)

    def test_parse_order_future_and_metadata(self):
        releases = parse_releases('## 2.2.10\nTen\n## 2.2.3\nThree\n## 2.2.2\nTwo\n## 3.0.0\nFuture', '2.2.10')
        self.assertEqual(list(releases), ['2.2.2', '2.2.3', '2.2.10'])
        for text in ('## 1.0.0\n', '## 1.0.0\nOne\n## 1.0.0\nTwo'):
            with self.assertRaises(ValueError): parse_releases(text, '1.0.0')
        history = (Path(__file__).resolve().parent.parent/'CHANGELOG.md').read_text(encoding='utf-8')
        self.assertIn(f'## {VERSION}', history)
        self.assertTrue(RELEASE_NOTES.startswith(LATEST_UPDATE))

    def test_release_failed_delivery_preserves_other_keys_and_ambiguous_states(self):
        target = ('1', '2', '1.0.0')
        other_keys = [('9', '2', '1.0.0'), ('1', '9', '1.0.0'), ('1', '2', '9.0.0')]
        protected = [('1', '2', 'uncertain'), ('1', '2', 'published'), ('1', '2', 'pending-with-message')]
        for key in [target, *other_keys, *protected]:
            self.store.claim_release_delivery(*key)
        self.store.uncertain_release_delivery(*protected[0])
        self.store.save_release_delivery(*protected[1], '77')
        self.store.db.execute("UPDATE release_deliveries SET message_id='88' WHERE version='pending-with-message'")
        before = {tuple(row) for row in self.store.db.execute('SELECT * FROM release_deliveries')}
        self.assertTrue(self.store.release_failed_release_delivery(*target))
        for key in protected:
            self.assertFalse(self.store.release_failed_release_delivery(*key))
        after = {tuple(row) for row in self.store.db.execute('SELECT * FROM release_deliveries')}
        self.assertEqual(after, before - {(*target, 'pending', None)})
        self.assertFalse(self.store.release_failed_release_delivery(*target))
        self.assertTrue(self.store.claim_release_delivery(*target))

    def test_delivery_claims_persist_and_old_table_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'releases.sqlite'
            store = Store(path)
            store.claim_release(str(G), VERSION); store.save_release(str(G), VERSION, '99')
            store.claim_release_delivery(str(G), '123', VERSION)
            store.uncertain_release_delivery(str(G), '123', VERSION); store.close()
            store = Store(path)
            try:
                self.assertFalse(store.claim_release_delivery(str(G), '123', VERSION))
                self.assertTrue(store.claim_release_delivery(str(G), '789', VERSION))
                self.assertTrue(store.claim_release_delivery('other', '123', VERSION))
                self.assertEqual(store.release_publication(str(G), VERSION)['message_id'], '99')
            finally: store.close()
