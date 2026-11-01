"""Сохранение подписок и защита от повторных тегов между почасовыми отчётами."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from lafamily.modules.prices.service import PriceService

from lafamily.database import Store
from lafamily.modules.prices.notifications import notify_maximum_prices
from lafamily.modules.prices.parser import parse_prices
from lafamily.modules.prices.commands import register_price_commands
import discord

EXAMPLE = Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')


class PriceAlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.set_price_subscription('1', '123', 'Медь', True)
        self.channel = SimpleNamespace(id=2, send=AsyncMock())
        self.message = SimpleNamespace(id=100, guild=SimpleNamespace(id=1), channel=self.channel)
        self.high = parse_prices(EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: $60 ($72)'))
        self.low = parse_prices(EXAMPLE)

    async def test_maximum_repeat_reset_and_older_report(self):
        await notify_maximum_prices(self.store, self.message, self.high)
        self.channel.send.assert_awaited_once()
        payload = self.channel.send.call_args.kwargs
        self.assertIn('<@123>', payload['content'])
        self.assertIn('Медь: $60', payload['content'])
        self.assertEqual(payload['allowed_mentions'].to_dict()['users'], [123])
        self.message.id = 101
        await notify_maximum_prices(self.store, self.message, self.high)
        self.message.id = 99
        await notify_maximum_prices(self.store, self.message, self.low)
        self.message.id = 102
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.channel.send.await_count, 1)
        self.message.id = 103
        await notify_maximum_prices(self.store, self.message, self.low)
        self.message.id = 104
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_duplicate_subscribe_unsubscribe_and_guild_isolation(self):
        await notify_maximum_prices(self.store, self.message, self.high)
        self.store.set_price_subscription('1', '123', 'Медь', True)
        self.store.set_price_subscription('other', '456', 'Медь', True)
        self.message.id = 101
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(self.store.set_price_subscription('1', '123', 'Медь', False), [])
        self.assertEqual(self.store.claim_price_alerts('1', '102', []), [])
        self.assertEqual(self.store.claim_price_alerts('1', '103', ['Медь']), [])

    async def test_failed_send_is_not_automatically_repeated(self):
        self.channel.send.side_effect = RuntimeError('ambiguous send')
        with self.assertRaises(RuntimeError):
            await notify_maximum_prices(self.store, self.message, self.high)
        self.message.id = 101
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_products_and_exact_threshold(self):
        self.store.set_price_subscription('1', '123', 'Капуста', True)
        report = parse_prices(EXAMPLE.replace('- Капуста: $720', '- Капуста: $748'))
        await notify_maximum_prices(self.store, self.message, report)
        self.channel.send.assert_awaited_once()
        self.assertIn('Капуста: $748', self.channel.send.call_args.kwargs['content'])

    def test_subscription_and_state_survive_reopening_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'test.sqlite'
            store = Store(path)
            store.set_price_subscription('1', '123', 'Медь', True)
            self.assertEqual(len(store.claim_price_alerts('1', '100', ['Медь'])), 1)
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.claim_price_alerts('1', '101', ['Медь']), [])
                store.claim_price_alerts('1', '102', [])
                self.assertEqual(len(store.claim_price_alerts('1', '103', ['Медь'])), 1)
            finally:
                store.close()

    async def test_subscription_command_registered_for_everyone(self):
        client = discord.Client(intents=discord.Intents.none())
        self.addAsyncCleanup(client.close)
        tree = discord.app_commands.CommandTree(client)
        register_price_commands(tree, SimpleNamespace(store=self.store, guild_id=1), 1)
        command = tree.get_command('скупщик-уведомление', guild=discord.Object(id=1))
        self.assertEqual(command.extras['access'], 'all')
        self.assertEqual(command.parameters, [])


    async def test_service_publishes_comparison_and_alert_but_manual_processing_is_silent(self):
        self.store.configure_prices('1', '123', '2')
        self.channel.send.return_value = SimpleNamespace(id=900)
        self.message.author = SimpleNamespace(id=456)
        self.message.content = EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: $60 ($72)')
        self.message.embeds = []
        self.message.message_snapshots = []
        client = SimpleNamespace(user=SimpleNamespace(id=789), intents=SimpleNamespace(message_content=True))
        service = PriceService(client, self.store, 1)
        with patch.object(service, 'require_channel'), patch.object(service, 'cleanup_channel', new=AsyncMock()):
            self.assertEqual(await service.process(self.message, notify=False), 'published')
            self.assertEqual(self.channel.send.await_count, 1)
            self.message.id = 101
            self.assertEqual(await service.process(self.message), 'published')
            self.assertEqual(self.channel.send.await_count, 3)
            self.message.id = 102
            self.assertEqual(await service.process(self.message), 'published')
            self.assertEqual(self.channel.send.await_count, 4)

    async def test_many_subscribers_are_batched_without_role_mentions(self):
        for user in range(200, 245):
            self.store.set_price_subscription('1', str(user), 'Медь', True)
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.channel.send.await_count, 3)
        count = 0
        for call in self.channel.send.call_args_list:
            payload = call.kwargs
            mentions = payload['allowed_mentions'].to_dict()
            self.assertEqual(mentions['parse'], [])
            self.assertLessEqual(len(mentions['users']), 20)
            self.assertLessEqual(len(payload['content']), 2000)
            count += len(mentions['users'])
        self.assertEqual(count, 46)


    async def test_cleanup_preserves_current_pair_and_pinned_messages(self):
        self.store.claim_price_publication('1', '2', '100', 'x')
        self.store.save_price_publication('1', '100', '110', 'x')
        messages = [SimpleNamespace(id=i, pinned=i == 80, content='', embeds=[], message_snapshots=[], delete=AsyncMock()) for i in (70,80,90,100,110,120)]
        async def history(**kwargs):
            for message in messages:
                yield message
        channel = SimpleNamespace(id=2, history=history,
            permissions_for=lambda member: SimpleNamespace(manage_messages=True, read_message_history=True),
            fetch_message=AsyncMock(side_effect=lambda number: next(message for message in messages if message.id == number)))
        source = SimpleNamespace(id=100, guild=SimpleNamespace(me=None), channel=channel)
        # Закрепление, появившееся после чтения истории, тоже сохраняется.
        async def fetch(number):
            message = next(message for message in messages if message.id == number)
            if number == 90:
                message.pinned = True
            return message
        channel.fetch_message.side_effect = fetch
        service = PriceService(None, self.store, 1)
        await service.cleanup_channel(source)
        for message in messages:
            if message.id in (70,120):
                message.delete.assert_awaited_once()
            else:
                message.delete.assert_not_awaited()


    async def test_cleanup_keeps_current_notification_but_removes_previous_one(self):
        self.store.claim_price_publication('1', '2', '100', 'x')
        self.store.save_price_publication('1', '100', '110', 'x')
        current = SimpleNamespace(id=120, pinned=False, author=SimpleNamespace(id=789),
            content='<@123>\n🏆 Медь — достигнут справочный максимум!\nhttps://discord.com/channels/1/2/100', delete=AsyncMock())
        old = SimpleNamespace(id=90, pinned=False, author=SimpleNamespace(id=789),
            content='<@123>\n🏆 Медь — достигнут справочный максимум!\nhttps://discord.com/channels/1/2/80', delete=AsyncMock())
        async def history(**kwargs):
            for message in (current, old): yield message
        channel = SimpleNamespace(id=2, history=history,
            permissions_for=lambda member: SimpleNamespace(manage_messages=True, read_message_history=True),
            fetch_message=AsyncMock(return_value=old))
        source = SimpleNamespace(id=100, guild=SimpleNamespace(me=None), channel=channel)
        service = PriceService(SimpleNamespace(user=SimpleNamespace(id=789)), self.store, 1)
        await service.cleanup_channel(source)
        await service.cleanup_channel(source)
        current.delete.assert_not_awaited()
        self.assertEqual(old.delete.await_count, 2)
