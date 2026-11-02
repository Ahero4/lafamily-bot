import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.ui import ReportView


class HourDraftTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service=SimpleNamespace(views={},store=None,confirm=AsyncMock(return_value='Сохранено'))
        self.draft=Draft('1','2','3',1,'МП',10,'2026-11-02T10:00:00Z',members=['2'],
                         win_enabled=True,available_win_bonus=5)
        self.created=time.monotonic()
        self.view=ReportView(self.service,self.draft)
        self.addCleanup(self.view.finish)
        self.interaction=SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                        edit_original_response=AsyncMock())

    async def test_hour_lifetime_and_click_at_30_minutes(self):
        self.assertEqual(self.view.timeout,3600)
        self.assertAlmostEqual(self.view.expires_at-self.created,3600,delta=1)
        self.assertIn('1 час',self.view.payload()['content'])
        expires=self.view.expires_at
        self.interaction.guild_id=1
        self.interaction.user=SimpleNamespace(id=2)
        with patch('lafamily.modules.events.ui.time.monotonic',return_value=self.created+1800), \
             patch('lafamily.modules.events.ui.require_access_async',new=AsyncMock()):
            await self.view.dispatch('outcome',self.interaction,'win')
        self.assertEqual(self.view.expires_at,expires)
        with patch('lafamily.modules.events.ui.time.monotonic',return_value=self.created+3601):
            with self.assertRaises(UserError):await self.view.validate(self.interaction)

    async def test_expiry_edits_message_without_expired_interaction_token(self):
        original=SimpleNamespace(edit_original_response=AsyncMock(side_effect=RuntimeError('token expired')))
        self.view.bind(original)
        message=SimpleNamespace(edit=AsyncMock())
        self.view.message=message
        await self.view.on_timeout()
        original.edit_original_response.assert_not_awaited()
        self.assertIsNone(message.edit.call_args.kwargs['view'])
        self.assertTrue(self.view.finished)

    async def test_outcome_default_follows_selection_and_survives_other_updates(self):
        self.view.validate=AsyncMock()
        for selected in ('win','no_win'):
            await self.view.dispatch('outcome',self.interaction,selected)
            await self.view.dispatch('clear',self.interaction)
            self.assertEqual([o.value for o in self.view.select_outcome.options if o.default],[selected])
            components=self.view.to_components()
            result=next(c for r in components for c in r['components'] if c.get('custom_id','').startswith('mp:outcome:'))
            self.assertEqual([o['value'] for o in result['options'] if o['default']],[selected])

    async def test_failed_selection_restores_previous_default(self):
        self.view.validate=AsyncMock()
        await self.view.dispatch('outcome',self.interaction,'win')
        self.interaction.edit_original_response.side_effect=RuntimeError('network')
        with self.assertRaises(RuntimeError):await self.view.dispatch('outcome',self.interaction,'no_win')
        self.assertEqual(self.draft.outcome,'win')
        self.assertEqual([o.value for o in self.view.select_outcome.options if o.default],['win'])
