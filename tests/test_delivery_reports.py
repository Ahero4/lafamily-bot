"""Результаты рассылок, агрегаты, экспорт, права и изоляция серверов."""
import asyncio
import csv
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.time import MOSCOW
from lafamily.modules.prices.commands import register_price_commands
from lafamily.modules.prices.delivery_reports import csv_file, format_dynamics, report_period
from lafamily.modules.prices.notifications import notify_maximum_prices
from lafamily.modules.prices.parser import parse_prices

EXAMPLE = Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')


class DeliveryReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure_prices('1', '99', '2')
        self.store.set_price_subscription('1', '123', 'Медь', True)
        self.store.set_price_subscription('1', '123', 'Капуста', True)
        self.store.set_price_subscription('1', '456', 'Медь', True)
        self.report = parse_prices(EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: $60 ($72)').replace('- Капуста: $720', '- Капуста: $748'))
        self.members = {i: SimpleNamespace(send=AsyncMock()) for i in (123,456)}
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 3
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=900))
        self.guild = SimpleNamespace(id=1, me=object(), get_member=self.members.get,
                                    fetch_member=AsyncMock(side_effect=self.members.get),
                                    fetch_channel=AsyncMock(return_value=self.channel))
        self.message = SimpleNamespace(id=100, guild=self.guild, channel=SimpleNamespace(id=2, send=AsyncMock(),
            permissions_for=lambda member:SimpleNamespace(view_channel=True)))
        self.client = discord.Client(intents=discord.Intents.none())
        self.addAsyncCleanup(self.client.close)
        self.tree = discord.app_commands.CommandTree(self.client)
        self.service = SimpleNamespace(store=self.store, guild_id=1, require_enabled=MagicMock(), require_channel=MagicMock())
        register_price_commands(self.tree, self.service, 1)
        self.interaction = SimpleNamespace(guild_id=1, guild=self.guild, user=SimpleNamespace(id=99),
            permissions=SimpleNamespace(administrator=True), response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock())

    def command(self, name):
        parts = name.split()
        command = self.tree.get_command(parts[0], guild=discord.Object(id=1))
        for part in parts[1:]:
            command = command.get_command(part)
        return command.callback

    def data(self, details=True):
        return self.store.price_delivery_data('1', '2000', '2100', details)

    async def test_report_counts_unique_people_and_individual_messages(self):
        self.store.configure_admin_report_channel('1', '99', '3')
        await notify_maximum_prices(self.store, self.message, self.report)
        self.channel.send.assert_awaited_once()
        content = self.channel.send.call_args.kwargs['content']
        self.assertIn('**2 из 2**', content)
        self.assertIn('**3 из 3**', content)
        self.assertEqual(self.channel.send.call_args.kwargs['allowed_mentions'].to_dict(), discord.AllowedMentions.none().to_dict())
        self.message.channel.send.assert_not_awaited()
        daily = self.data()['daily'][0]
        self.assertEqual((daily['recipients'], daily['delivered'], daily['sent'], daily['subscribers']), (2,2,3,2))
        self.assertEqual({a['state'] for a in self.data()['attempts']}, {'sent'})
        self.assertEqual({a['admin_state'] for a in self.data()['attempts']}, {'sent'})
        self.message.id += 1
        await notify_maximum_prices(self.store, self.message, self.report)
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(self.data()['attempts']), 3)

    async def test_closed_dm_and_uncertain_send_are_recorded_separately(self):
        self.members[123].send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='closed'), 'closed')
        self.members[456].send.side_effect = OSError('network')
        await notify_maximum_prices(self.store, self.message, self.report)
        row = self.data()['daily'][0]
        self.assertEqual((row['delivered'], row['sent'], row['failed'], row['uncertain']), (0,0,2,1))
        self.assertEqual({a['error_type'] for a in self.data()['attempts']}, {'Forbidden','OSError'})
        self.assertEqual({a['admin_state'] for a in self.data()['attempts']}, {'disabled'})

    async def test_report_failure_keeps_delivery_history_and_does_not_retry(self):
        self.store.configure_admin_report_channel('1', '99', '3')
        self.channel.send.side_effect = OSError('ambiguous')
        await notify_maximum_prices(self.store, self.message, self.report)
        self.assertEqual(self.data()['daily'][0]['sent'], 3)
        self.assertEqual({a['admin_state'] for a in self.data()['attempts']}, {'uncertain'})
        await notify_maximum_prices(self.store, self.message, self.report)
        self.channel.send.assert_awaited_once()

    async def test_pending_survives_restart_without_automatic_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'history.sqlite'
            store = Store(path)
            store.set_price_subscription('1','123','Медь',True)
            alerts = store.claim_price_alerts('1','100',['Медь'])
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.claim_price_alerts('1','101',['Медь']), [])
                summary = store.price_delivery_summary('1', alerts[0]['run_id'])
                self.assertEqual((summary['pending'], summary['sent']), (1,0))
                store.finish_price_delivery(alerts[0]['attempt_id'], 'sent')
                store.finish_price_delivery(alerts[0]['attempt_id'], 'uncertain')
                self.assertEqual(store.price_delivery_summary('1',alerts[0]['run_id'])['sent'],1)
            finally:
                store.close()

    async def test_claim_and_pending_are_atomic(self):
        with patch.object(self.store, 'track_price_alerts', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                self.store.claim_price_alerts('1','100',['Медь'])
        self.assertEqual(len(self.store.claim_price_alerts('1','100',['Медь'])),2)

    async def test_moscow_boundaries_guild_isolation_and_daily_unique_people(self):
        await notify_maximum_prices(self.store, self.message, self.report)
        self.store.claim_price_alerts('1','101',[])
        self.message.id = 102
        await notify_maximum_prices(self.store, self.message, self.report)
        self.store.db.execute("UPDATE price_delivery_runs SET created_at='2026-11-02T21:00:00.000Z'")
        start,end = report_period(1,datetime(2026,11,3,12,tzinfo=MOSCOW))
        self.assertEqual(start,'2026-11-02T21:00:00.000Z')
        data = self.store.price_delivery_data('1',start,end,True)
        self.assertEqual(data['daily'][0]['day'],'2026-11-03')
        self.assertEqual((data['daily'][0]['delivered'],data['daily'][0]['runs'],data['daily'][0]['sent']),(2,2,6))
        self.assertEqual(self.store.price_delivery_data('other',start,end,True),{'daily':[],'attempts':[]})
        self.store.db.execute('UPDATE price_delivery_runs SET created_at=?',(end,))
        self.assertEqual(self.store.price_delivery_data('1',start,end)['daily'],[])

    async def test_admin_commands_access_configuration_and_disable(self):
        self.interaction.permissions.administrator = False
        for name in ('бот отчёты','скупщик статистика'):
            with self.assertRaises(UserError):
                await self.command(name)(self.interaction)
        self.interaction.permissions.administrator = True
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True,send_messages=True)
        await self.command('бот отчёты')(self.interaction,self.channel)
        self.assertEqual(self.store.admin_report_channel('1'),'3')
        await self.command('бот отчёты')(self.interaction)
        self.assertIsNone(self.store.admin_report_channel('1'))
        self.channel.id = 2
        with self.assertRaises(UserError):
            await self.command('бот отчёты')(self.interaction,self.channel)
        self.channel.id = 3
        self.channel.permissions_for.return_value.send_messages = False
        with self.assertRaises(UserError):
            await self.command('бот отчёты')(self.interaction,self.channel)

    async def test_price_channel_cannot_replace_admin_channel(self):
        self.store.configure_admin_report_channel('1','99','3')
        with self.assertRaises(UserError):
            await self.command('скупщик настройки')(self.interaction,self.channel)
        self.assertEqual(self.store.price_settings('1')['channel_id'],'2')

    async def test_no_admin_post_when_channels_collide(self):
        # Прежняя некорректная конфигурация: новые setters такое уже запрещают.
        self.store.db.execute("INSERT INTO admin_report_settings VALUES ('1','2')")
        await notify_maximum_prices(self.store,self.message,self.report)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.data()['daily'][0]['sent'],3)

    async def test_statistics_returns_private_table_list_and_csv(self):
        await notify_maximum_prices(self.store,self.message,self.report)
        await self.command('скупщик статистика')(self.interaction,30,'table',True)
        args = self.interaction.edit_original_response.call_args.kwargs
        self.assertIn('```',args['content'])
        self.assertEqual([f.filename for f in args['attachments']], ['buyer-daily.csv','buyer-deliveries.csv'])
        self.interaction.response.defer.assert_awaited_with(ephemeral=True,thinking=True)
        await self.command('скупщик статистика')(self.interaction,30,'list',False)
        self.assertIn('• ',self.interaction.edit_original_response.call_args.kwargs['content'])

    async def test_csv_headers_unicode_and_format_limits(self):
        await notify_maximum_prices(self.store,self.message,self.report)
        rows = self.data()['attempts']
        file = csv_file(rows,'test.csv')
        try:
            parsed = list(csv.DictReader(io.StringIO(file.fp.read().decode('utf-8-sig')),delimiter=';'))
            self.assertEqual(len(parsed),3)
            self.assertEqual({r['item'] for r in parsed},{'Медь','Капуста'})
        finally:
            file.close()
        daily = self.data()['daily'] * 366
        for view in ('table','list'):
            self.assertLessEqual(len(format_dynamics(daily,view)),2000)
        self.assertIn('нет',format_dynamics([], 'table'))
        for days in (0,367):
            with self.assertRaises(UserError):
                report_period(days)

    async def test_cancelled_delivery_keeps_pending(self):
        self.members[123].send.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await notify_maximum_prices(self.store,self.message,self.report)
        self.assertEqual(self.data()['daily'][0]['pending'],3)

    async def test_export_respects_discord_attachment_limit(self):
        await notify_maximum_prices(self.store,self.message,self.report)
        self.guild.filesize_limit = 1
        with self.assertRaises(UserError):
            await self.command('скупщик статистика')(self.interaction,30,'table',True)
        self.interaction.edit_original_response.assert_not_awaited()

    async def test_detail_export_limit_is_explicit_without_truncation(self):
        alerts = self.store.claim_price_alerts('1','100',['Медь'])
        run = alerts[0]['run_id']
        with self.store.transaction():
            self.store.db.executemany('INSERT INTO price_delivery_attempts (run_id,user_id,item,threshold) VALUES (?,?,?,?)',
                ((run, str(i), 'Рубин', 'max') for i in range(1000,51001)))
        with self.assertRaises(UserError):
            self.data(details=True)
        self.assertEqual(self.data(details=False)['daily'][0]['messages'],50003)
