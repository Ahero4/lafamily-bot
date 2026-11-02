import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.ui import ReportView, VoiceRosterPicker

G='111111111111111111'
A='222222222222222222'
B='333333333333333333'
C='444444444444444444'


class VoicePickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.store.configure(G,A,C,'organizer','manager')
        self.service=SimpleNamespace(store=self.store,views={})
        self.draft=Draft(G,A,C,1,'МП',10,'2026-11-02T10:00:00Z',members=[A],win_enabled=True)
        self.view=ReportView(self.service,self.draft);self.addCleanup(self.view.finish)
        self.main_message=SimpleNamespace(edit=AsyncMock())
        self.view.message=self.main_message
        self.voice=MagicMock(spec=discord.VoiceChannel)
        self.voice.id=123;self.voice.guild=SimpleNamespace(id=int(G))
        self.voice.members=[SimpleNamespace(id=int(A),bot=False),SimpleNamespace(id=int(B),bot=False),
                            SimpleNamespace(id=5,bot=True)]
        self.voice.permissions_for.return_value=SimpleNamespace(view_channel=True)

    def interaction(self,role='organizer',user=A):
        return SimpleNamespace(guild_id=int(G),guild=SimpleNamespace(fetch_channel=AsyncMock(return_value=self.voice)),
            user=SimpleNamespace(id=int(user),roles=[SimpleNamespace(id=role)]),
            permissions=discord.Permissions.none(),message=SimpleNamespace(edit=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock(), type=None, is_done=lambda: False, send_message=AsyncMock()),edit_original_response=AsyncMock())

    async def test_import_adds_without_duplicates_bots_or_resetting_deadline(self):
        request=self.interaction();expires=self.view.expires_at
        await self.view.import_voice(request,123)
        await self.view.import_voice(request,123)
        self.assertEqual(self.draft.members,[A,B])
        self.assertEqual(self.draft.voice_channel_id,'123')
        self.assertEqual(expires,self.view.expires_at)
        self.assertIs(self.view.message,self.main_message)
        self.assertEqual(self.main_message.edit.await_count,2)
        request.message.edit.assert_not_awaited()  # личное окно не подменяет общий черновик
        self.assertTrue(request.response.defer.call_args.kwargs['ephemeral'])

    async def test_manager_can_import_but_other_organizer_cannot(self):
        with self.assertRaises(UserError):await self.view.import_voice(self.interaction(user=B),123)
        await self.view.import_voice(self.interaction('manager',B),123)
        self.assertEqual(self.draft.members,[A,B])
        with self.assertRaises(UserError):await self.view.import_voice(self.interaction('member'),123)

    async def test_access_limit_finished_and_expired_block_changes(self):
        self.voice.permissions_for.return_value.view_channel=False
        with self.assertRaises(UserError):await self.view.import_voice(self.interaction(),123)
        self.voice.permissions_for.return_value.view_channel=True
        self.voice.members=[SimpleNamespace(id=100+i,bot=False) for i in range(100)]
        with self.assertRaises(UserError):await self.view.import_voice(self.interaction(),123)
        self.assertEqual(self.draft.members,[A])
        self.assertIsNone(self.draft.voice_channel_id)
        self.main_message.edit.assert_not_awaited()
        self.view.expires_at=time.monotonic()-1
        with self.assertRaises(UserError):await self.view.import_voice(self.interaction(),123)
        self.view.finish()
        with self.assertRaises(UserError):await self.view.import_voice(self.interaction(),123)

    async def test_failed_main_message_edit_rolls_back_channel_and_members(self):
        self.draft.voice_channel_id='99'
        self.main_message.edit.side_effect=RuntimeError('network')
        with self.assertRaises(RuntimeError):await self.view.import_voice(self.interaction(),123)
        self.assertEqual(self.draft.members,[A])
        self.assertEqual(self.draft.voice_channel_id,'99')
        self.assertTrue(self.view.needs_refresh)

    async def test_button_opens_private_voice_only_picker_and_retains_layout(self):
        request=self.interaction()
        await self.view.choose_voice.callback(request)
        payload=request.edit_original_response.call_args.kwargs
        picker=payload['view'];self.addCleanup(picker.stop)
        self.assertTrue(request.response.defer.call_args.kwargs['ephemeral'])
        self.assertEqual(picker.channel_select.channel_types,[discord.ChannelType.voice])
        self.assertLessEqual(picker.timeout,300)
        self.assertLessEqual(len(self.view.to_components()),5)
        self.assertFalse(await picker.interaction_check(self.interaction(user=B)))
        # Отзыв роли после открытия проверяется непосредственно при добавлении.
        with self.assertRaises(UserError):await self.view.import_voice(self.interaction('member'),123)
