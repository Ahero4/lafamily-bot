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
        self.dm = SimpleNamespace(send=AsyncMock())
        self.members = {}
        def get_member(user_id):
            return self.members.setdefault(user_id, SimpleNamespace(id=user_id, send=AsyncMock(wraps=self.dm.send)))
        self.message = SimpleNamespace(id=100, guild=SimpleNamespace(id=1, get_member=get_member,
            fetch_member=AsyncMock()), channel=self.channel)
        self.high = parse_prices(EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: $60 ($72)'))
        self.low = parse_prices(EXAMPLE)

    async def test_maximum_repeat_reset_and_older_report(self):
        await notify_maximum_prices(self.store, self.message, self.high)
        self.dm.send.assert_awaited_once()
        payload = self.dm.send.call_args.kwargs
        self.members[123].send.assert_awaited_once()
        self.channel.send.assert_not_awaited()
        self.assertIn('Медь: $60', payload['content'])
        self.assertEqual(payload['allowed_mentions'].to_dict(), discord.AllowedMentions.none().to_dict())
        self.message.id = 101
        await notify_maximum_prices(self.store, self.message, self.high)
        self.message.id = 99
        await notify_maximum_prices(self.store, self.message, self.low)
        self.message.id = 102
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.dm.send.await_count, 1)
        self.message.id = 103
        await notify_maximum_prices(self.store, self.message, self.low)
        self.message.id = 104
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.dm.send.await_count, 2)

    async def test_duplicate_subscribe_unsubscribe_and_guild_isolation(self):
        await notify_maximum_prices(self.store, self.message, self.high)
        self.store.set_price_subscription('1', '123', 'Медь', True)
        self.store.set_price_subscription('other', '456', 'Медь', True)
        self.message.id = 101
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.dm.send.await_count, 1)
        self.assertEqual(self.store.set_price_subscription('1', '123', 'Медь', False), [])
        self.assertEqual(self.store.claim_price_alerts('1', '102', []), [])
        self.assertEqual(self.store.claim_price_alerts('1', '103', ['Медь']), [])

    async def test_failed_send_is_not_automatically_repeated(self):
        self.dm.send.side_effect = RuntimeError('ambiguous send')
        await notify_maximum_prices(self.store, self.message, self.high)
        self.message.id = 101
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.dm.send.await_count, 1)

    async def test_products_and_exact_threshold(self):
        self.store.set_price_subscription('1', '123', 'Капуста', True)
        report = parse_prices(EXAMPLE.replace('- Капуста: $720', '- Капуста: $748'))
        await notify_maximum_prices(self.store, self.message, report)
        self.dm.send.assert_awaited_once()
        self.assertIn('Капуста: $748', self.dm.send.call_args.kwargs['content'])

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
            self.dm.send.assert_not_awaited()
            self.message.id = 101
            self.assertEqual(await service.process(self.message), 'published')
            self.assertEqual(self.channel.send.await_count, 2)
            self.assertEqual(self.dm.send.await_count, 1)
            self.message.id = 102
            self.assertEqual(await service.process(self.message), 'published')
            self.assertEqual(self.channel.send.await_count, 3)

    async def test_many_subscribers_receive_individual_dms(self):
        for user in range(200, 245):
            self.store.set_price_subscription('1', str(user), 'Медь', True)
        await notify_maximum_prices(self.store, self.message, self.high)
        self.assertEqual(self.dm.send.await_count, 46)
        self.channel.send.assert_not_awaited()
        for member in self.members.values():
            member.send.assert_awaited_once()
            payload = member.send.call_args.kwargs
            self.assertNotIn('<@', payload['content'])
            self.assertEqual(payload['allowed_mentions'].to_dict(), discord.AllowedMentions.none().to_dict())
            self.assertLessEqual(len(payload['content']), 2000)

    async def test_delivery_failures_do_not_stop_other_subscribers(self):
        for failure in (discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'closed'),
                        discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'left'),
                        discord.HTTPException(SimpleNamespace(status=500, reason='Error'), 'failed'),
                        OSError('network')):
            with self.subTest(failure=type(failure).__name__):
                self.store.claim_price_alerts('1', str(self.message.id + 1), [])
                self.message.id += 2
                self.store.set_price_subscription('1', '456', 'Медь', True)
                self.message.guild.get_member(123).send.side_effect = failure
                other = self.message.guild.get_member(456)
                other.send.reset_mock()
                await notify_maximum_prices(self.store, self.message, self.high)
                other.send.assert_awaited_once()
                self.channel.send.assert_not_awaited()
                self.assertIn('Медь', self.store.price_subscription_modes('1', '123'))

    async def test_uncached_member_is_fetched(self):
        self.message.guild.get_member = lambda user_id: None
        self.message.guild.fetch_member.return_value = self.dm
        await notify_maximum_prices(self.store, self.message, self.high)
        self.message.guild.fetch_member.assert_awaited_once_with(123)
        self.dm.send.assert_awaited_once()
        self.channel.send.assert_not_awaited()

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

    async def test_near_boundaries_and_independent_maximum_cycles(self):
        self.store.replace_price_subscriptions('1','123',['Медь'],{'Медь':'near'})
        self.store.set_price_subscription('1','456','Медь',True)
        # Максимум меди 60: 57 — ровно 95%, 56 — ниже порога.
        def report(price):
            return parse_prices(EXAMPLE.replace('- Медь: $36 ($43)',f'- Медь: ${price} ($72)'))
        await notify_maximum_prices(self.store,self.message,report(56))
        self.dm.send.assert_not_awaited()
        self.message.id+=1
        await notify_maximum_prices(self.store,self.message,report(57))
        self.assertEqual(self.dm.send.await_count,1)
        self.members[123].send.assert_awaited_once()
        self.assertIn('95–100%',self.dm.send.call_args.kwargs['content'])
        self.message.id+=1
        await notify_maximum_prices(self.store,self.message,report(60))
        self.assertEqual(self.dm.send.await_count,2)
        self.members[456].send.assert_awaited_once()
        self.message.id+=1
        await notify_maximum_prices(self.store,self.message,report(60))
        self.assertEqual(self.dm.send.await_count,2)
        self.message.id+=1
        await notify_maximum_prices(self.store,self.message,report(56))
        self.message.id+=1
        await notify_maximum_prices(self.store,self.message,report(60))
        self.assertEqual(self.dm.send.await_count,4)

    def test_mode_preserves_flags_and_survives_reopening(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'modes.sqlite'
            store=Store(path)
            store.set_price_subscription('1','123','Медь',True)
            store.claim_price_alerts('1','100',['Медь'])
            store.replace_price_subscriptions('1','123',['Медь'],{'Медь':'max'})
            self.assertEqual(store.claim_price_alerts('1','101',['Медь']),[])
            store.replace_price_subscriptions('1','123',['Медь'],{'Медь':'near'})
            self.assertEqual(len(store.claim_price_alerts('1','102',[],['Медь'])),1)
            store.close()
            store=Store(path)
            try:
                self.assertEqual(store.price_subscription_modes('1','123'),{'Медь':'near'})
                self.assertEqual(store.claim_price_alerts('1','103',[],['Медь']),[])
            finally:store.close()

    def test_legacy_migration_keeps_max_mode_and_notification_flag(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'old.sqlite'
            connection=sqlite3.connect(path)
            connection.executescript("CREATE TABLE price_subscriptions(guild_id TEXT,user_id TEXT,item TEXT,notified INTEGER DEFAULT 0,PRIMARY KEY(guild_id,user_id,item)); INSERT INTO price_subscriptions VALUES('1','123','Медь',1);")
            connection.close()
            store=Store(path)
            try:
                self.assertEqual(store.price_subscription_modes('1','123'),{'Медь':'max'})
                self.assertEqual(store.claim_price_alerts('1','100',['Медь']),[])
            finally:store.close()

    def test_near_notices_are_preserved_by_cleanup(self):
        from lafamily.modules.prices.service import is_current_price_notice
        message=SimpleNamespace(author=SimpleNamespace(id=42),content='🏆 Медь — цена 95–100% от максимума!\nhttps://discord.com/channels/1/2/100')
        self.assertTrue(is_current_price_notice(message,42,1,2,100))
        self.assertFalse(is_current_price_notice(message,42,1,2,101))
