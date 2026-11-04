"""Время в DM и удаление только учтённых сообщений цен, включая сбои и перезапуск."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.database import Store
from lafamily.modules.events.time import MOSCOW
from lafamily.modules.prices.service import PriceService

EXAMPLE = Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')
HIGH = EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: $60 ($72)')


class PriceDmCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'test.sqlite'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.configure_prices('1', '99', '2')
        self.store.set_price_subscription('1', '123', 'Медь', True)
        self.client = SimpleNamespace(user=SimpleNamespace(id=789),
                                      intents=SimpleNamespace(message_content=True))
        self.messages = {}
        self.dm = MagicMock(spec=discord.DMChannel)
        self.dm.id = 50
        self.dm.recipient = SimpleNamespace(id=123)
        self.dm.fetch_message = AsyncMock(side_effect=self.messages.__getitem__)
        self.client.get_channel = MagicMock(return_value=self.dm)
        self.client.fetch_channel = AsyncMock(return_value=self.dm)
        self.member = SimpleNamespace(id=123, send=AsyncMock(side_effect=self.send_dm))
        self.channel = SimpleNamespace(id=2, send=AsyncMock(return_value=SimpleNamespace(id=900)),
            permissions_for=lambda member: SimpleNamespace(view_channel=True),
            fetch_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))
        self.guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=self.member))
        self.channel.guild=self.guild
        self.client.fetch_channel=AsyncMock(side_effect=lambda number:self.channel if number==2 else self.dm)
        self.service = PriceService(self.client, self.store, 1)
        self.service.require_channel = MagicMock()
        self.service.cleanup_channel = AsyncMock()
        self.source = None

    async def send_dm(self, *, content, allowed_mentions):
        message = SimpleNamespace(id=1000+len(self.messages), author=self.client.user,
                                  channel=self.dm, content=content, delete=AsyncMock())
        self.messages[message.id] = message
        return message

    async def post(self, source=100, content=HIGH, *, notify=True):
        self.source = SimpleNamespace(id=source, guild=self.guild, channel=self.channel,
            author=SimpleNamespace(id=456), content=content, embeds=[], message_snapshots=[])
        return await self.service.process(self.source, notify=notify)

    def registry(self):
        return [dict(row) for row in self.store.db.execute('SELECT * FROM price_dm_messages')]

    async def test_local_date_and_time_are_in_each_dm_and_id_is_saved(self):
        local = datetime(2026, 11, 4, 0, 5, 30, tzinfo=MOSCOW)
        with patch('lafamily.modules.prices.queue.datetime') as clock:
            clock.now.return_value = local
            await self.post()
            clock.now.assert_called_once_with(MOSCOW)
        self.assertIn('Отправлено: **04.11.2026 00:05:30 МСК**.', self.messages[1000].content)
        self.assertEqual(self.registry()[0]['message_id'], '1000')
        self.assertEqual(self.registry()[0]['channel_id'], '50')
        self.assertEqual(self.registry()[0]['stale'], 0)

    async def test_new_report_without_alert_deletes_old_dm_and_keeps_other_messages(self):
        await self.post()
        unrelated = SimpleNamespace(id=2000, author=self.client.user, channel=self.dm,
            content='Ваша заявка одобрена. Ссылка: https://discord.com/channels/1/2/100', delete=AsyncMock())
        participant = SimpleNamespace(id=2001, author=self.member, channel=self.dm,
            content=self.messages[1000].content, delete=AsyncMock())
        self.messages.update({2000: unrelated, 2001: participant})
        await self.post(101, EXAMPLE)
        self.messages[1000].delete.assert_awaited_once()
        unrelated.delete.assert_not_awaited()
        participant.delete.assert_not_awaited()
        self.dm.history.assert_not_called()
        self.member.send.assert_awaited_once()
        self.assertEqual(self.registry(), [])
        self.assertEqual(self.store.price_delivery_summary('1', 1)['sent'], 1)

    async def test_new_source_still_at_maximum_removes_previous_notice_without_repeat(self):
        await self.post()
        await self.post(101)
        self.messages[1000].delete.assert_awaited_once()
        self.member.send.assert_awaited_once()

    async def test_edit_of_same_source_invalidates_old_revision(self):
        await self.post()
        self.assertEqual(await self.post(100, EXAMPLE), 'updated')
        self.messages[1000].delete.assert_awaited_once()

    async def test_duplicate_and_older_reports_preserve_current_dm(self):
        await self.post(200)
        self.assertEqual(await self.post(200), 'already_done')
        await self.post(199, EXAMPLE)
        self.messages[1000].delete.assert_not_awaited()
        self.assertEqual(self.registry()[0]['stale'], 0)

    async def test_manual_processing_cleans_up_without_starting_new_delivery(self):
        await self.post()
        await self.post(101, EXAMPLE, notify=False)
        self.messages[1000].delete.assert_awaited_once()
        self.member.send.assert_awaited_once()

    async def test_failed_delete_survives_restart_and_retries_on_duplicate_source(self):
        await self.post()
        self.messages[1000].delete.side_effect = OSError('network')
        await self.post(101, EXAMPLE)
        self.assertEqual(self.registry()[0]['cleanup_error'], 'OSError')
        self.store.close()
        self.store = Store(self.path)
        self.service = PriceService(self.client, self.store, 1)
        self.service.require_channel = MagicMock()
        self.service.cleanup_channel = AsyncMock()
        self.messages[1000].delete.side_effect = None
        self.client.get_channel.return_value = None
        await self.post(101, EXAMPLE)
        self.client.fetch_channel.assert_any_await(50)
        self.assertEqual(self.messages[1000].delete.await_count, 2)
        self.assertEqual(self.registry(), [])
        self.member.send.assert_awaited_once()

    async def test_failed_delete_does_not_stop_new_alert_or_change_delivery_counts(self):
        await self.post()
        self.messages[1000].delete.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='closed'), 'closed')
        await self.post(101, EXAMPLE)
        await self.post(102)
        self.assertEqual(self.member.send.await_count, 2)
        self.messages[1001].delete.assert_not_awaited()
        self.assertEqual({row['state'] for row in self.store.db.execute(
            'SELECT state FROM price_delivery_attempts')}, {'sent'})

    async def test_already_deleted_message_is_removed_from_queue(self):
        await self.post()
        self.dm.fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason='gone'), 'gone')
        await self.post(101, EXAMPLE)
        self.assertEqual(self.registry(), [])

    async def test_one_failed_deletion_does_not_block_another_product(self):
        await self.post()
        self.messages[1000].delete.side_effect = OSError('network')
        await self.post(101, EXAMPLE)
        await self.post(102)
        await self.post(103,EXAMPLE)
        self.messages[1001].delete.assert_awaited_once()
        self.assertEqual([row['message_id'] for row in self.registry()], ['1000'])

    async def test_rejected_publication_does_not_delete_previous_dm(self):
        await self.post()
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='denied'), 'denied')
        from lafamily.errors import UserError
        with self.assertRaises(UserError):
            await self.post(101, EXAMPLE)
        self.messages[1000].delete.assert_not_awaited()
        self.assertEqual(self.registry()[0]['stale'], 0)

    async def test_registry_failure_does_not_mark_successful_dm_as_failed_or_stop_others(self):
        self.store.set_price_subscription('1', '123', 'Капуста', True)
        with patch.object(self.store, 'remember_price_dm', side_effect=RuntimeError('write failed')):
            await self.post(content=HIGH.replace('- Капуста: $720', '- Капуста: $748'))
        self.assertEqual(self.member.send.await_count, 1)
        self.assertEqual(self.store.price_delivery_summary('1', 1)['sent'], 1)
        self.assertEqual(self.registry(), [])

    async def test_ownership_content_and_recipient_are_checked_before_delete(self):
        for mismatch in ('author', 'content', 'recipient', 'channel', 'message'):
            with self.subTest(mismatch=mismatch):
                # Независимый цикл позволяет проверить все защитные условия на новых DM.
                source = 100 + len(self.messages) * 2
                await self.post(source)
                message = self.messages[max(self.messages)]
                if mismatch == 'author':
                    message.author = self.member
                elif mismatch == 'content':
                    message.content = 'Другое сообщение бота'
                elif mismatch == 'recipient':
                    self.dm.recipient = SimpleNamespace(id=456)
                elif mismatch == 'channel':
                    message.channel = SimpleNamespace(id=77)
                else:
                    message.id += 9999
                await self.post(source + 1, EXAMPLE)
                message.delete.assert_not_awaited()
                self.assertEqual(self.registry()[-1]['stale'], 2)
                self.dm.recipient = SimpleNamespace(id=123)

    async def test_other_guild_and_other_bot_dms_are_preserved(self):
        await self.post()
        self.store.set_price_subscription('2', '123', 'Медь', True)
        alert = self.store.claim_price_alerts('2', '200', ['Медь'])[0]
        self.store.activate_price_dm_revision('2', '200', 'rev')
        self.store.remember_price_dm(alert['attempt_id'], '789', '50', '2000', 'rev', 'hash')
        self.store.activate_price_dm_revision('2', '201', 'new')
        self.client.user = SimpleNamespace(id=888)
        await self.post(101, EXAMPLE)
        self.dm.fetch_message.assert_not_awaited()
        self.assertEqual(len(self.registry()), 2)
        self.assertEqual(len(self.store.stale_price_dms('1')), 1)
        self.assertEqual(len(self.store.stale_price_dms('2')), 1)

    async def test_old_schema_migration_preserves_delivery_history_and_subscriptions(self):
        await self.post()
        self.store.db.executescript('DROP TABLE price_dm_messages; DROP TABLE price_dm_current;')
        before = self.store.price_subscription_modes('1', '123')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.price_subscription_modes('1', '123'), before)
        self.assertEqual(self.store.claim_price_alerts('1', '101', ['Медь']), [])
        self.assertEqual(self.store.price_delivery_summary('1', 1)['sent'], 1)
        self.assertEqual(self.registry(), [])


if __name__ == '__main__':
    unittest.main()
