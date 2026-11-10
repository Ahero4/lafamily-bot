"""Меню нескольких товаров: черновой выбор, подтверждение и сохранение флагов уведомлений."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.prices.subscriptions import PriceSubscriptionView


class SubscriptionMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure_prices('1', '123', '2')
        self.store.set_price_subscription('1', '123', 'Медь', True)
        self.store.claim_price_alerts('1', '100', ['Медь'])
        self.channel = SimpleNamespace(permissions_for=lambda user: SimpleNamespace(view_channel=True))
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=123),
            guild=SimpleNamespace(fetch_channel=AsyncMock(return_value=self.channel)),
            response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.service = SimpleNamespace(store=self.store, require_enabled=MagicMock(), require_channel=MagicMock())
        self.view = PriceSubscriptionView(self.service, self.interaction, ['Медь'])
        self.addCleanup(self.view.stop)

    async def test_multi_selection_waits_for_confirmation_and_preserves_existing_alert_flag(self):
        self.assertTrue(next(option for option in self.view.selector.options if option.value == 'Медь').default)
        self.assertEqual(self.view.selector.max_values, 20)
        self.assertEqual(self.view.selector.min_values, 0)
        await self.view.choose(self.interaction, ['Медь', 'Рубин', 'Карп'])
        self.assertEqual(self.store.price_subscriptions('1', '123'), ['Медь'])
        await self.view.save(self.interaction)
        self.assertEqual(set(self.store.price_subscriptions('1', '123')), {'Медь', 'Рубин', 'Карп'})
        self.assertEqual(self.store.claim_price_alerts('1', '101', ['Медь']), [])
        self.assertIn('Discord (ЛС)', self.interaction.edit_original_response.call_args.kwargs['content'])
        self.assertNotIn('<#', self.interaction.edit_original_response.call_args.kwargs['content'])
        self.assertTrue(self.view.finished)
        self.assertIsNone(self.interaction.edit_original_response.call_args.kwargs['view'])
        with self.assertRaises(UserError):
            await self.view.save(self.interaction)

    async def test_empty_selection_removes_all_without_requiring_channel_access(self):
        await self.view.choose(self.interaction, [])
        await self.view.save(self.interaction)
        self.assertEqual(self.store.price_subscriptions('1', '123'), [])
        self.service.require_enabled.assert_not_called()
        self.interaction.guild.fetch_channel.assert_not_awaited()

    async def test_cancel_and_expiry_leave_subscriptions_unchanged(self):
        await self.view.choose(self.interaction, ['Карп'])
        await self.view.cancel.callback(self.interaction)
        self.assertEqual(self.store.price_subscriptions('1', '123'), ['Медь'])
        other = PriceSubscriptionView(self.service, self.interaction, ['Медь'])
        self.addCleanup(other.stop)
        await other.choose(self.interaction, ['Карп'])
        await other.on_timeout()
        self.assertTrue(other.finished)
        self.assertEqual(self.store.price_subscriptions('1', '123'), ['Медь'])

    async def test_foreign_user_and_changed_permissions_cannot_save(self):
        self.interaction.user.id = 456
        with self.assertRaises(UserError):
            await self.view.choose(self.interaction, ['Карп'])
        with self.assertRaises(UserError):
            await self.view.save(self.interaction)
        self.interaction.user.id = 123
        await self.view.choose(self.interaction, ['Карп'])
        self.channel.permissions_for = lambda user: SimpleNamespace(view_channel=False)
        with self.assertRaises(UserError):
            await self.view.save(self.interaction)
        self.assertFalse(self.view.finished)
        self.assertEqual(self.store.price_subscriptions('1', '123'), ['Медь'])

    async def test_menu_serializes_as_multi_select_and_confirmation_buttons(self):
        components = self.view.to_components()
        select = components[0]['components'][0]
        self.assertEqual(select['max_values'], 20)
        self.assertEqual(len(select['options']), 20)
        self.assertEqual([item['label'] for item in components[3]['components']], ['Подтвердить', 'Отмена'])

    async def test_global_mode_then_item_override_saved_only_on_confirm(self):
        await self.view.choose(self.interaction,['Медь','Карп'])
        await self.view.choose_mode(self.interaction,'near')
        self.assertEqual(self.store.price_subscription_modes('1','123'),{'Медь':'max'})
        self.view.scope_item='Карп'
        await self.view.choose_mode(self.interaction,'max')
        self.assertEqual(self.view.modes,{'Медь':'near','Карп':'max'})
        await self.view.save(self.interaction)
        saved=self.store.price_subscription_modes('1','123')
        self.assertEqual(saved,{'Медь':'near','Карп':'max'})
        reopened=PriceSubscriptionView(self.service,self.interaction,list(saved),saved)
        self.addCleanup(reopened.stop)
        self.assertEqual(reopened.modes,saved)

    async def test_invalid_scope_mode_and_foreign_user_do_not_save(self):
        with self.assertRaises(UserError):await self.view.choose_mode(self.interaction,'bad')
        self.view.scope_item='Рубин'
        with self.assertRaises(UserError):await self.view.choose_mode(self.interaction,'near')
        self.interaction.user.id=999
        with self.assertRaises(UserError):await self.view.choose_mode(self.interaction,'near')
        self.assertEqual(self.store.price_subscription_modes('1','123'),{'Медь':'max'})

    async def test_full_menu_fits_discord_and_explains_dm_delivery(self):
        from lafamily.modules.prices.parser import LIMITS
        await self.view.choose(self.interaction, list(LIMITS))
        self.assertLessEqual(len(self.view.prompt()), 2000)
        self.assertIn('Discord (ЛС)', self.view.prompt())
