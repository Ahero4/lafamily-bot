"""Пример пользователя, пороги 90%, пересылки, права и защита от дублирования."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.bot import FamilyBot
from lafamily.config import Config, load_config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.prices.parser import parse_prices, extract_prices, Price
from lafamily.modules.prices.formatting import render_prices, near_maximum, formatted_price, reference_mark
from lafamily.modules.prices.service import PriceService

EXAMPLE = Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')
GUILD = 111111111111111111
CHANNEL = 222222222222222222
SOURCE = 333333333333333333
RESULT = 444444444444444444
ACTOR = 555555555555555555


class ParserTests(unittest.IsolatedAsyncioTestCase):
    def test_full_user_example_including_inline_code_fences(self):
        report = parse_prices(EXAMPLE)
        self.assertEqual(len(report.buyers), 3)
        self.assertEqual(len(report.buyers[1]), 16)
        self.assertEqual(len(report.products), 4)
        self.assertEqual(report.buyers[1]['Алмаз'].base, 2903)
        self.assertEqual(report.buyers[3]['Идеальное мясо'].bonus, 1693)
        self.assertEqual(report.stamp, '27.10.2026 23:05 МСК')

    def test_bad_partial_duplicate_unknown_and_inconsistent_reports_are_rejected(self):
        variants = [EXAMPLE.replace('- Медь: $36 ($43)\n', ''),
                    EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: $36 ($43)\n- Медь: $36 ($43)'),
                    EXAMPLE.replace('- Медь: $36 ($43)', '- Неизвестный товар: $36 ($43)'),
                    EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: неизвестно'),
                    EXAMPLE.replace('- Медь: $36 ($43)', '- Медь: $36'),
                    EXAMPLE.split(':small_orange_diamond:_**Скупщик продуктов**_')[0]]
        for source in variants:
            with self.subTest(source=source[:50]), self.assertRaises(ValueError): parse_prices(source)

    def test_grouped_prices_and_forwarded_snapshots_or_embeds(self):
        self.assertEqual(parse_prices(EXAMPLE.replace('$2903', '$2 903')).buyers[1]['Алмаз'].base, 2903)
        forwarded = SimpleNamespace(content='', embeds=[], message_snapshots=[SimpleNamespace(content=EXAMPLE, embeds=[])])
        self.assertIsNotNone(extract_prices(forwarded))
        embedded = SimpleNamespace(content='', embeds=[discord.Embed(description=EXAMPLE)], message_snapshots=[])
        self.assertIsNotNone(extract_prices(embedded))

    async def test_best_prices_and_reference_thresholds_are_different(self):
        payload = render_prices(parse_prices(EXAMPLE))
        view = payload['view']
        first, second, third, products = [box.children[1] for box in view.children[1:]]
        self.assertIn('Медь: **$36** (**$43**) ✅', first.content)
        self.assertIn('Рубин: **$675** (**$810**) ✅ 🏆', second.content)
        self.assertIn('Солярка: **$198** (**$237**) ✅ 🏆', first.content)
        self.assertIn('Идеальное мясо: **$1411** (**$1693**) ✅ ⚠️', third.content)
        self.assertIn('Капуста: $720 🏆', products.content)
        self.assertIn('Тыква: $653\n', products.content)
        self.assertNotIn('```', ''.join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)))
        self.assertLessEqual(view.content_length(), 4000)
        self.assertTrue(view.has_components_v2())
        self.assertEqual(len(view.children[1:]), 4)
        self.assertTrue(all(isinstance(box, discord.ui.Container) for box in view.children[1:]))
        self.assertNotIn('embeds', payload)
        self.assertNotIn('content', payload)
        self.assertEqual(payload['allowed_mentions'].to_dict(), {'parse': []})

    async def test_ties_and_bonus_best_are_handled_independently(self):
        report = parse_prices(EXAMPLE.replace('- Медь: $23 ($27)', '- Медь: $36 ($44)'))
        first, second, *_ = [box.children[1] for box in render_prices(report)['view'].children[1:]]
        self.assertIn('Медь: **$36** ($43)', first.content)
        self.assertIn('Медь: **$36** (**$44**)', second.content)

    def test_exact_ten_percent_boundary_and_distinct_bonus_threshold(self):
        self.assertFalse(near_maximum(3597, 3997))
        self.assertTrue(near_maximum(3598, 3997))
        self.assertTrue(near_maximum(3997, 3997))
        self.assertFalse(near_maximum(3998, 3997))
        self.assertIn('⚠️ обычная', formatted_price(Price('Медь', 54, 64)))
        self.assertIn('⚠️ +20%', formatted_price(Price('Медь', 53, 65)))


    def test_reference_mark_boundaries_and_no_warning_together_with_trophy(self):
        for price, expected in [(179, ''), (180, '⚠️'), (189, '⚠️'), (190, '🏆'), (200, '🏆'), (201, '')]:
            with self.subTest(price=price):
                self.assertEqual(reference_mark(price, 200), expected)
        self.assertEqual(formatted_price(Price('Бензин', 100, 120), 100, 120), '**$100** (**$120**) ✅')
        self.assertEqual(formatted_price(Price('Бензин', 190, 228), 200, 240), '$190 ($228) 🏆')
        self.assertNotIn('✅', formatted_price(Price('Капуста', 720, None)))


class PriceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure_prices(str(GUILD), str(ACTOR), str(CHANNEL))
        self.client = SimpleNamespace(user=SimpleNamespace(id=999999999999999999), intents=SimpleNamespace(message_content=True))
        self.guild = SimpleNamespace(id=GUILD, me=SimpleNamespace(id=999999999999999999))
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = CHANNEL
        self.permissions = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        self.channel.permissions_for.return_value = self.permissions
        self.output = SimpleNamespace(id=RESULT, edit=AsyncMock())
        self.channel.send = AsyncMock(return_value=self.output)
        self.channel.fetch_message = AsyncMock(return_value=self.output)
        self.source = SimpleNamespace(id=SOURCE, guild=self.guild, channel=self.channel, author=SimpleNamespace(id=ACTOR, bot=True),
                                      content=EXAMPLE, embeds=[], message_snapshots=[], delete=AsyncMock())
        self.service = PriceService(self.client, self.store, GUILD)

    async def test_publish_once_keep_original_and_ignore_duplicate_after_service_restart(self):
        self.assertEqual(await self.service.process(self.source), 'published')
        self.source.delete.assert_not_awaited()
        restarted = PriceService(self.client, self.store, GUILD)
        self.assertEqual(await restarted.process(self.source), 'already_done')
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.price_publication(str(GUILD), str(SOURCE))['result_id'], str(RESULT))

    async def test_incomplete_other_channel_other_guild_and_own_output_are_ignored(self):
        self.source.content = 'not a price report'
        self.assertEqual(await self.service.process(self.source), 'unrecognized')
        self.source.content = EXAMPLE
        self.channel.id = CHANNEL + 1
        self.assertEqual(await self.service.process(self.source), 'ignored')
        self.channel.id = CHANNEL
        self.guild.id = GUILD + 1
        self.assertEqual(await self.service.process(self.source), 'ignored')
        self.guild.id = GUILD
        self.source.author.id = self.client.user.id
        self.assertEqual(await self.service.process(self.source), 'ignored')
        self.channel.send.assert_not_awaited()
        self.source.delete.assert_not_awaited()

    async def test_edited_source_updates_output_instead_of_creating_copy(self):
        await self.service.process(self.source)
        self.source.content = EXAMPLE.replace('$36 ($43)', '$54 ($64)')
        self.assertEqual(await self.service.process(self.source), 'updated')
        self.output.edit.assert_awaited_once()
        self.assertIsNone(self.output.edit.call_args.kwargs["content"])
        self.assertEqual(self.output.edit.call_args.kwargs["embeds"], [])
        self.assertTrue(self.output.edit.call_args.kwargs["view"].has_components_v2())
        self.channel.send.assert_awaited_once()
        self.source.delete.assert_not_awaited()

    async def test_send_failure_preserves_source_and_never_retries_ambiguous_send(self):
        self.channel.send.side_effect = RuntimeError('simulated network failure')
        with self.assertLogs('lafamily.errors', level='ERROR'), self.assertRaises(UserError):
            await self.service.process(self.source)
        with self.assertRaises(UserError): await self.service.process(self.source)
        self.channel.send.assert_awaited_once()
        self.source.delete.assert_not_awaited()
        self.assertEqual(self.store.price_publication(str(GUILD), str(SOURCE))['state'], 'uncertain')

    async def test_database_failure_after_send_does_not_delete_source_or_resend(self):
        with patch.object(self.store, 'save_price_publication', side_effect=sqlite3.OperationalError('test')):
            with self.assertRaises(sqlite3.OperationalError): await self.service.process(self.source)
        with self.assertRaises(UserError): await self.service.process(self.source)
        self.channel.send.assert_awaited_once()
        self.source.delete.assert_not_awaited()

    async def test_parallel_events_do_not_duplicate_publication(self):
        await asyncio.gather(self.service.process(self.source), self.service.process(self.source))
        self.channel.send.assert_awaited_once()

    async def test_missing_permissions_and_disabled_content_intent_do_not_publish(self):
        self.permissions.send_messages = False
        with self.assertRaises(UserError): await self.service.process(self.source)
        self.permissions.send_messages = True
        self.client.intents.message_content = False
        await self.service.handle(self.source)
        self.channel.send.assert_not_awaited()


    async def test_legacy_embed_publication_is_upgraded_without_duplicate(self):
        import hashlib
        import json
        from dataclasses import asdict
        old_hash = hashlib.sha256(json.dumps(asdict(parse_prices(EXAMPLE)), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.store.claim_price_publication(str(GUILD), str(CHANNEL), str(SOURCE), old_hash)
        self.store.save_price_publication(str(GUILD), str(SOURCE), str(RESULT), old_hash)
        self.assertEqual(await self.service.process(self.source), 'updated')
        self.channel.send.assert_not_awaited()
        payload = self.output.edit.call_args.kwargs
        self.assertIsNone(payload['content'])
        self.assertEqual(payload['embeds'], [])
        self.assertTrue(payload['view'].has_components_v2())
        self.assertEqual(await self.service.process(self.source), 'already_done')
        self.output.edit.assert_awaited_once()


class PriceCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure(str(GUILD), str(ACTOR), str(CHANNEL), 'organizer', ['manager-one', 'manager-two'])
        self.bot = FamilyBot(Config('fake', 123456789012345678, GUILD, Path(':memory:'), message_content_intent=True), self.store)
        self.addAsyncCleanup(self.bot.close)
        self.settings = self.bot.tree.get_command('скупщик-настройка', guild=discord.Object(id=GUILD))

    def request(self, role=None, admin=False):
        return SimpleNamespace(guild_id=GUILD, guild=SimpleNamespace(), user=SimpleNamespace(id=ACTOR, roles=[SimpleNamespace(id=role)] if role else []),
                               permissions=discord.Permissions(administrator=admin), response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())

    async def test_manager_any_configured_role_and_admin_can_configure(self):
        channel = SimpleNamespace(id=CHANNEL + 1)
        self.bot.prices.require_channel = MagicMock()
        for role, admin in [('manager-one', False), ('manager-two', False), (None, True)]:
            await self.settings.callback(self.request(role, admin), channel)
            self.assertEqual(self.store.price_settings(str(GUILD))['channel_id'], str(CHANNEL + 1))
        self.assertEqual(self.settings.extras['access'], 'manager')
        self.assertIsNone(self.settings.default_permissions)
        self.assertTrue(self.bot.intents.guild_messages)
        self.assertTrue(self.bot.intents.message_content)

    async def test_organizer_and_regular_member_cannot_configure(self):
        for role in [None, 'organizer']:
            with self.assertRaises(UserError): await self.settings.callback(self.request(role), SimpleNamespace(id=CHANNEL))
        self.assertIsNone(self.store.price_settings(str(GUILD)))

    async def test_invalid_message_id_is_rejected_and_content_flag_defaults_off(self):
        process = self.bot.tree.get_command('скупщик-обработать', guild=discord.Object(id=GUILD))
        with self.assertRaises(UserError): await process.callback(self.request('manager-one'), 'not-an-id')
        env = {'DISCORD_TOKEN': 'fake', 'DISCORD_CLIENT_ID': '123456789012345678', 'DISCORD_GUILD_ID': str(GUILD)}
        with patch('lafamily.config.load_dotenv'), patch.dict('os.environ', env, clear=True):
            self.assertFalse(load_config().message_content_intent)
            with patch.dict('os.environ', {'DISCORD_MESSAGE_CONTENT_INTENT': 'true'}): self.assertTrue(load_config().message_content_intent)
            with patch.dict('os.environ', {'DISCORD_MESSAGE_CONTENT_INTENT': 'bad'}):
                with self.assertRaises(UserError): load_config()


class PricePersistenceTests(unittest.TestCase):
    def test_processed_message_and_channel_persist_in_sqlite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prices.sqlite'
            store = Store(path)
            try:
                store.configure_prices(str(GUILD), str(ACTOR), str(CHANNEL))
                self.assertTrue(store.claim_price_publication(str(GUILD), str(CHANNEL), str(SOURCE), 'hash'))
                store.save_price_publication(str(GUILD), str(SOURCE), str(RESULT), 'hash')
            finally: store.close()
            store = Store(path)
            try:
                self.assertEqual(store.price_settings(str(GUILD))['channel_id'], str(CHANNEL))
                self.assertFalse(store.claim_price_publication(str(GUILD), str(CHANNEL), str(SOURCE), 'hash'))
                self.assertEqual(store.price_publication(str(GUILD), str(SOURCE))['state'], 'published')
                self.assertIsNone(store.price_publication(str(GUILD + 1), str(SOURCE)))
            finally: store.close()


class LayoutSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_discord_payload_uses_v2_flag_and_clears_legacy_embeds_on_edit(self):
        from discord.http import handle_message_parameters
        payload = render_prices(parse_prices(EXAMPLE))
        with handle_message_parameters(**payload) as parameters:
            self.assertTrue(parameters.payload['flags'] & discord.MessageFlags(components_v2=True).value)
            self.assertNotIn('content', parameters.payload)
            self.assertNotIn('embeds', parameters.payload)
            containers = parameters.payload['components'][1:]
            self.assertEqual(len(containers), 4)
            self.assertTrue(all(box['type'] == discord.ComponentType.container.value for box in containers))
        with handle_message_parameters(content=None, embeds=[], **payload) as parameters:
            self.assertIsNone(parameters.payload['content'])
            self.assertEqual(parameters.payload['embeds'], [])
