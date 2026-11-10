"""Уведомления остаются в ЛС после смены цен и перезапуска."""
import hashlib
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


class PriceDmRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'test.sqlite'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.configure_prices('1', '99', '2')
        self.store.set_price_subscription('1', '123', 'Медь', True)
        self.dm = MagicMock(spec=discord.DMChannel)
        self.dm.id = 50
        self.dm.recipient = SimpleNamespace(id=123)
        self.messages = []
        self.member = SimpleNamespace(send=AsyncMock(side_effect=self.send_dm))
        self.guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=self.member))
        self.channel = SimpleNamespace(
            id=2, guild=self.guild, send=AsyncMock(return_value=SimpleNamespace(id=900)),
            permissions_for=lambda member: SimpleNamespace(view_channel=True),
            fetch_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        )
        self.client = SimpleNamespace(
            user=SimpleNamespace(id=789), intents=SimpleNamespace(message_content=True),
            get_channel=MagicMock(return_value=self.dm),
            fetch_channel=AsyncMock(return_value=self.channel),
        )
        self.make_service()

    def make_service(self):
        self.service = PriceService(self.client, self.store, 1)
        self.service.require_channel = MagicMock()
        self.service.cleanup_channel = AsyncMock()

    async def send_dm(self, *, content, allowed_mentions):
        message = SimpleNamespace(id=1000 + len(self.messages), author=self.client.user,
                                  channel=self.dm, content=content, delete=AsyncMock())
        self.messages.append(message)
        self.dm.fetch_message.return_value = message
        return message

    async def post(self, source=100, content=HIGH, *, notify=True):
        message = SimpleNamespace(id=source, guild=self.guild, channel=self.channel,
                                  author=SimpleNamespace(id=456), content=content,
                                  embeds=[], message_snapshots=[])
        return await self.service.process(message, notify=notify)

    async def test_prices_can_change_without_deleting_delivered_notifications(self):
        local = datetime(2026, 11, 4, 0, 5, 30, tzinfo=MOSCOW)
        with patch('lafamily.modules.prices.queue.datetime') as clock:
            clock.now.return_value = local
            await self.post()
            clock.now.assert_called_once_with(MOSCOW)
        self.assertIn('· **Скупщик** **№1** · максимум', self.messages[0].content)
        self.assertIn('Отправлено: **00:05** 04.11.2026 МСК.', self.messages[0].content)

        self.assertEqual(await self.post(), 'already_done')
        self.assertEqual(await self.post(100, EXAMPLE), 'updated')
        self.assertEqual(await self.post(101, HIGH), 'published')
        self.assertEqual(await self.post(102, EXAMPLE, notify=False), 'published')
        await self.service.queue.drain()

        self.assertEqual(self.member.send.await_count, 2)
        for message in self.messages:
            message.delete.assert_not_awaited()
        self.dm.fetch_message.assert_not_called()
        self.assertEqual(self.store.price_delivery_summary('1', 1)['sent'], 1)
        self.assertEqual(self.store.price_delivery_summary('1', 2)['sent'], 1)

    async def test_new_alert_includes_other_subscriptions_still_matching(self):
        modes = {'Керосин': 'near', 'Хорошее мясо': 'near', 'Бензин': 'near', 'Солярка': 'max'}
        self.store.replace_price_subscriptions('1', '123', list(modes), modes)
        self.store.replace_price_subscriptions('1', '456', ['Керосин'], {'Керосин': 'near'})
        other = SimpleNamespace(send=AsyncMock())
        self.guild.fetch_member.side_effect = {123: self.member, 456: other}.__getitem__
        first = (EXAMPLE.replace('- Керосин: $123 ($147)', '- Керосин: $196 ($235)')
                 .replace('- Хорошее мясо: $987 ($1184)', '- Хорошее мясо: $900 ($1080)'))
        second = first.replace('- Хорошее мясо: $900 ($1080)', '- Хорошее мясо: $990 ($1188)')

        await self.post(100, first)
        self.assertIn('Керосин: $196', self.messages[0].content)
        self.assertNotIn('Хорошее мясо', self.messages[0].content)

        await self.post(101, second)
        self.assertEqual(self.member.send.await_count, 2)
        content = self.messages[1].content
        self.assertIn('Хорошее мясо: $990', content)
        self.assertIn('Керосин: $196', content)
        self.assertNotIn('Бензин', content)
        self.assertNotIn('Солярка', content)
        other.send.assert_awaited_once()

        await self.post(102, second)
        self.assertEqual(self.member.send.await_count, 2)
        other.send.assert_awaited_once()

    async def test_upgrade_ignores_preexisting_pending_deletion(self):
        await self.post()
        message = self.messages[0]
        # Имитируем сохранённую очередь удаления из версии 0.6.0.
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS price_dm_messages (
            attempt_id INTEGER PRIMARY KEY REFERENCES price_delivery_attempts(id),
            bot_id TEXT, channel_id TEXT, message_id TEXT, revision TEXT,
            content_hash TEXT, stale INTEGER, cleanup_error TEXT)''')
        attempt_id = self.store.db.execute('SELECT id FROM price_delivery_attempts').fetchone()[0]
        revision = self.store.db.execute('SELECT revision FROM price_dm_current').fetchone()[0]
        self.store.db.execute('INSERT OR REPLACE INTO price_dm_messages VALUES (?,?,?,?,?,?,?,?)',
                              (attempt_id, '789', '50', str(message.id), revision,
                               hashlib.sha256(message.content.encode()).hexdigest(), 1, 'OSError'))

        self.store.close()
        self.store = Store(self.path)
        self.make_service()
        await self.service.queue.drain()
        await self.post(101, EXAMPLE)

        message.delete.assert_not_awaited()
        self.dm.fetch_message.assert_not_called()
        self.assertEqual(self.store.db.execute('SELECT stale FROM price_dm_messages').fetchone()[0], 1)
        self.assertEqual(self.store.price_subscription_modes('1', '123'), {'Медь': 'max'})
        self.assertEqual(self.store.price_delivery_summary('1', 1)['sent'], 1)
        self.assertFalse(any(row['kind'] == 'cleanup' for row in self.store.unfinished_operations('1')))
