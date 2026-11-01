import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord
from lafamily.database import Store
from lafamily.modules.events.ui import ReportView
from lafamily.modules.events.models import Draft
from lafamily.errors import UserError

class SignupTests(unittest.IsolatedAsyncioTestCase):
    async def test_signup_immediately_joins_roster_and_duplicate_is_ignored(self):
        draft = Draft('1', '2', '3', 1, 'МП', 5, '2026-10-28T10:00:00+00:00')
        store = Store(':memory:')
        self.addCleanup(store.close)
        view = ReportView(SimpleNamespace(views={}, store=store), draft)
        self.addCleanup(view.finish)
        interaction = SimpleNamespace(guild_id=1, guild=SimpleNamespace(), permissions=discord.Permissions.none(), user=SimpleNamespace(id=4, bot=False),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        await view.join.callback(interaction)
        self.assertEqual(view.pending, [])
        self.assertEqual(draft.members, ['4'])
        self.assertFalse(view.confirm.disabled)
        await view.join.callback(interaction)
        self.assertEqual(view.pending, [])
        interaction.response.send_message.assert_awaited_once()
        with self.assertRaises(UserError):
            await view.validate(interaction)
        view.finish()
        with self.assertRaises(UserError):
            await view.join.callback(interaction)


    async def test_23_signups_cannot_be_silently_saved_as_13(self):
        draft = Draft('1', '2', '3', 1, 'МП', 5, '2026-10-28T10:00:00+00:00')
        draft.members = [str(100+i) for i in range(13)]
        service = SimpleNamespace(views={}, confirm=AsyncMock(return_value='Сохранено'), store=None)
        view = ReportView(service, draft)
        self.addCleanup(view.finish)
        view.pending = [str(200+i) for i in range(10)]
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        view.validate = AsyncMock()
        with self.assertRaises(UserError):
            await view.dispatch('confirm', interaction)
        service.confirm.assert_not_awaited()
        self.assertEqual(len(view.pending) + len(draft.members), 23)
        draft.members.extend(view.pending)
        view.pending = []
        await view.dispatch('confirm', interaction)
        service.confirm.assert_awaited_once()
        self.assertEqual(len(draft.members), 23)


    async def test_voice_channel_required_for_signup_and_outsider_gets_link(self):
        draft = Draft('1', '2', '3', 1, 'МП', 5, '2026-10-28T10:00:00+00:00', voice_channel_id='55')
        view = ReportView(SimpleNamespace(views={}), draft)
        self.addCleanup(view.finish)
        interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=4, bot=False, voice=None),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        await view.join.callback(interaction)
        self.assertEqual(draft.members, [])
        self.assertIn('https://discord.com/channels/1/55', interaction.response.send_message.call_args.args[0])
        self.assertEqual(interaction.response.send_message.call_args.kwargs['allowed_mentions'].users, [interaction.user])
        interaction.user.voice = SimpleNamespace(channel=SimpleNamespace(id=55))
        await view.join.callback(interaction)
        self.assertEqual(draft.members, ['4'])
