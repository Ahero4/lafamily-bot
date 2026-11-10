"""Очередь цен сохраняет сигналы при сбоях и учитывает актуальные подписки."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lafamily.database import Store
from lafamily.modules.prices.parser import LIMITS, Price, parse_prices, price_dm_revision
from lafamily.modules.prices.queue import PriceQueue, enqueue_notifications


EXAMPLE = Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')


class PriceQueueRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure_prices('1', '99', '2')
        self.store.replace_price_subscriptions('1', '123', ['Медь', 'Капуста'])
        self.member = SimpleNamespace(send=AsyncMock())
        self.guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=self.member))
        self.channel = SimpleNamespace(id=2, guild=self.guild,
            permissions_for=lambda member: SimpleNamespace(view_channel=True))
        self.client = SimpleNamespace(fetch_channel=AsyncMock(return_value=self.channel))
        self.message = SimpleNamespace(id=100, guild=self.guild, channel=self.channel)
        self.queue = PriceQueue(self.client, self.store, 1)
        self.report = parse_prices(EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: $60 ($72)')
                                  .replace('- Капуста: $720', '- Капуста: $748'))

    async def enqueue(self, source=100):
        self.message.id = source
        self.store.activate_price_dm_revision('1', str(source), price_dm_revision(self.report))
        await enqueue_notifications(self.store, self.message, self.report)

    async def test_queue_failure_rolls_back_flags_and_all_recipient_jobs(self):
        self.store.set_price_subscription('1', '456', 'Медь', True)
        insert = self.store._enqueue_price_job
        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('disk full')
            return insert(*args)

        with patch.object(self.store, '_enqueue_price_job', side_effect=fail_second):
            with self.assertRaises(OSError):
                await self.enqueue()
        for table in ('delivery_jobs', 'price_delivery_runs', 'price_delivery_attempts'):
            self.assertEqual(self.store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        self.assertFalse(any(row[0] for row in self.store.db.execute('SELECT notified FROM price_subscriptions')))
        await self.enqueue()
        await self.queue.drain()
        self.assertEqual(self.member.send.await_count, 2)

    async def test_new_source_replaces_unsent_signals_but_never_repeats_sent(self):
        await self.enqueue()
        self.client.fetch_channel.side_effect = OSError('offline')
        await self.queue.drain()
        self.client.fetch_channel.side_effect = None
        await self.enqueue(101)
        await self.queue.drain()
        self.member.send.assert_awaited_once()
        self.assertIn('/101', self.member.send.call_args.kwargs['content'])
        self.assertEqual(self.store.price_delivery_summary('1', 1)['failed'], 1)
        await self.enqueue(102)
        await self.queue.drain()
        self.member.send.assert_awaited_once()

    async def test_edited_source_replaces_unsent_revision(self):
        await self.enqueue()
        self.report.buyers[2]['Медь'] = Price('Медь', 61, 73)
        await self.enqueue()
        await self.queue.drain()
        self.member.send.assert_awaited_once()
        self.assertIn('Медь: $61', self.member.send.call_args.kwargs['content'])

    async def test_unsubscribe_and_mode_change_filter_pending_message(self):
        await self.enqueue()
        # Проверяем снятие подписки во время получения участника перед отправкой.
        async def fetch_member(user_id):
            self.store.replace_price_subscriptions('1', '123', ['Капуста'])
            return self.member
        self.guild.fetch_member.side_effect = fetch_member
        await self.queue.drain()
        content = self.member.send.call_args.kwargs['content']
        self.assertIn('Капуста', content)
        self.assertNotIn('Медь', content)
        self.assertEqual(self.store.price_delivery_summary('1', 1)['sent'], 1)
        self.assertEqual(self.store.price_delivery_data('1', '2000', '2100')['daily'][0]['sent'], 1)

        self.store.claim_price_alerts('1', '101', [])
        await self.enqueue(102)
        self.guild.fetch_member.side_effect = None
        self.store.replace_price_subscriptions('1', '123', ['Капуста'], {'Капуста': 'near'})
        await self.queue.drain()
        self.member.send.assert_awaited_once()

    async def test_expired_context_does_not_reset_an_earlier_delivered_signal(self):
        self.store.replace_price_subscriptions('1', '123', ['Медь'])
        await self.enqueue()
        await self.queue.drain()
        self.store.set_price_subscription('1', '123', 'Капуста', True)
        await self.enqueue(101)
        self.store.set_price_subscription('1', '123', 'Капуста', False)
        await self.enqueue(102)
        await self.queue.drain()
        self.member.send.assert_awaited_once()

    async def test_ambiguous_send_never_retries_after_a_new_source(self):
        await self.enqueue()
        self.member.send.side_effect = OSError('response lost')
        await self.queue.drain()
        self.member.send.side_effect = None
        await self.enqueue(101)
        await self.queue.drain()
        self.member.send.assert_awaited_once()
        self.assertEqual(self.store.price_delivery_summary('1', 1)['uncertain'], 1)

    async def test_buyer_formatting_is_included_in_message_limit(self):
        self.store.replace_price_subscriptions('1', '123', list(LIMITS))
        for buyer, rows in self.report.buyers.items():
            for item in rows:
                rows[item] = Price(item, LIMITS[item]['base'], 10 ** 12 + buyer)
        for item in self.report.products:
            self.report.products[item] = Price(item, LIMITS[item]['base'], None)
        await self.enqueue()
        await self.queue.drain()
        contents = [call.kwargs['content'] for call in self.member.send.call_args_list]
        self.assertGreater(len(contents), 1)
        self.assertTrue(all(len(content) <= 2000 for content in contents))
        for item in LIMITS:
            self.assertEqual(sum(f'**{item}:' in content for content in contents), 1)

    async def test_legacy_full_message_preserves_all_items_within_discord_limit(self):
        guild_id, channel_id, source_id = '1' * 20, '2' * 20, '3' * 20
        self.guild.id = int(guild_id)
        self.channel.id = int(channel_id)
        self.store.configure_prices(guild_id, '99', channel_id)
        items = [item for item, limit in LIMITS.items() if limit['bonus'] is not None]
        self.store.replace_price_subscriptions(guild_id, '123', items)
        alerts = self.store.claim_price_alerts(guild_id, source_id, items)
        lines = [f'🏆 **{alert["item"]}: $200 ($240)** · №1, №2, №3 · максимум' for alert in alerts]
        padding = 1650 - len('\n'.join(lines))
        lines[0] = lines[0].replace('$200', '$' + '2' * (3 + padding), 1)
        body = '\n'.join(lines)
        self.assertEqual(len(body), 1650)
        self.store.activate_price_dm_revision(guild_id, source_id, 'legacy')
        self.store.enqueue_price_job(guild_id, source_id, 'legacy', '123', channel_id, body,
                                     [alert['attempt_id'] for alert in alerts])
        await self.queue.deliver(self.store.claim_price_job(guild_id))
        self.member.send.assert_awaited_once()
        content = self.member.send.call_args.kwargs['content']
        self.assertLessEqual(len(content), 2000)
        for item in items:
            self.assertIn(f'**{item}:', content)

    def test_old_subscription_flags_survive_schema_upgrade(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.sqlite'
            connection = sqlite3.connect(path)
            try:
                connection.executescript("CREATE TABLE price_subscriptions(guild_id TEXT,user_id TEXT,item TEXT,"
                    "notified INTEGER,PRIMARY KEY(guild_id,user_id,item));"
                    "INSERT INTO price_subscriptions VALUES('1','123','Медь',1);")
            finally:
                connection.close()
            store = Store(path)
            try:
                self.assertEqual(store.claim_price_alerts('1', '100', ['Медь']), [])
                self.assertEqual(store.price_subscription_modes('1', '123'), {'Медь': 'max'})
            finally:
                store.close()
