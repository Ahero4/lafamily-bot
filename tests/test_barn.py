import unittest
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.barn import parse_amount

class BarnTests(unittest.TestCase):
    def test_amount(self):
        for text in ('157000', '+157к', '157k', '157 000'):
            self.assertEqual(parse_amount(text), 157000)
        for text in ('0', '-100', 'abc', '1.1', '1000000001'):
            with self.assertRaises(UserError): parse_amount(text)

    def test_points_history_idempotency_and_cancel(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        args = ('1', '111111111111111111', '222222222222222222', '3', 'barn:4', 157000)
        with self.assertRaises(UserError): store.create_barn_collection(*args)
        store.configure_barn('1', args[1], 5)
        report = store.create_barn_collection(*args)
        self.assertEqual(report['points'], 5)
        self.assertEqual(report['members'], [args[2]])
        store.configure_barn('1', args[1], 10)
        self.assertEqual(store.create_barn_collection(*args)['id'], report['id'])
        self.assertEqual(store.report('1', report['id'])['points'], 5)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM barn_collections').fetchone()[0], 1)
        store.cancel_report('1', args[1], report['id'], 'Ошибка')
        self.assertIsNotNone(store.report('1', report['id'])['cancelled_at'])
        with self.assertRaises(UserError): store.create_barn_collection('2', *args[1:])


class BarnCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_selected_recipient_receive_points(self):
        import discord
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from lafamily.modules.barn import register_barn_commands
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.configure_barn('1', '111111111111111111', 7)
        client = discord.Client(intents=discord.Intents.none())
        self.addAsyncCleanup(client.close)
        tree = discord.app_commands.CommandTree(client)
        register_barn_commands(tree, store, 1)
        command = tree.get_command('коровник-сбор', guild=discord.Object(id=1))
        for number, selected in enumerate((None, SimpleNamespace(id=222222222222222222))):
            user = SimpleNamespace(id=111111111111111111, bot=False)
            target = selected or user
            interaction = SimpleNamespace(id=number, guild_id=1, channel_id=3, user=user,
                guild=SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(id=target.id, bot=False))),
                response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
            await command.callback(interaction, '+157к', selected)
            report = store.report('1', number+1)
            self.assertEqual(report['members'], [str(target.id)])
            self.assertEqual(report['points'], 7)
            self.assertEqual(interaction.edit_original_response.call_args.kwargs['allowed_mentions'].to_dict(), {'parse': []})
