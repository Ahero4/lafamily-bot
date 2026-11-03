import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from lafamily.database import Store
from lafamily.modules.prices.parser import parse_prices
from lafamily.modules.prices.notifications import bonus_price_text, notify_maximum_prices

EXAMPLE=Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')

class NotificationBonusTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_modes_include_source_bonus_without_changing_repeat_protection(self):
        store=Store(':memory:'); self.addCleanup(store.close)
        store.replace_price_subscriptions('1','123',['Медь'],{'Медь':'max'})
        store.replace_price_subscriptions('1','456',['Медь'],{'Медь':'near'})
        report=parse_prices(EXAMPLE.replace('- Медь: $36 ($43)','- Медь: $60 ($72)'))
        message=SimpleNamespace(id=100,guild=SimpleNamespace(id=1),channel=SimpleNamespace(id=2,send=AsyncMock()))
        dm=SimpleNamespace(send=AsyncMock())
        message.guild.get_member=lambda user_id: dm
        message.guild.fetch_member=AsyncMock(return_value=dm)
        message.channel.permissions_for=lambda member:SimpleNamespace(view_channel=True)
        await notify_maximum_prices(store,message,report)
        self.assertEqual(dm.send.await_count,2)
        for call in dm.send.call_args_list:
            self.assertIn('Медь: $60 ($72)',call.kwargs['content'])
            self.assertNotIn('+20%',call.kwargs['content'])
            self.assertIn('Скупщик: **№',call.kwargs['content'])
        message.id=101
        await notify_maximum_prices(store,message,report)
        self.assertEqual(dm.send.await_count,2)

    def test_source_rounding_not_recalculated(self):
        report=parse_prices(EXAMPLE.replace('- Алмаз: $2903 ($3483)','- Алмаз: $3997 ($4796)'))
        self.assertEqual(bonus_price_text(report,'Алмаз',3997),' ($4796)')

    def test_tied_buyers_keep_their_own_bonuses(self):
        report=parse_prices(EXAMPLE.replace('- Медь: $36 ($43)','- Медь: $60 ($72)').replace('- Медь: $23 ($27)','- Медь: $60 ($71)'))
        text=bonus_price_text(report,'Медь',60)
        self.assertIn('№1: $72',text)
        self.assertIn('№2: $71',text)

    def test_missing_bonus_is_explicitly_calculated(self):
        report=parse_prices(EXAMPLE)
        self.assertEqual(bonus_price_text(report,'Капуста',720),' (≈$864 (расчёт))')
