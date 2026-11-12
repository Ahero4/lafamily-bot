"""Garden rules verified with controlled clocks and isolated databases."""
import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.garden_store import CROPS
from lafamily.modules.garden import GardenService, GardenView, PlantModal


def at(hour,minute=0):
    return datetime(2026,11,12,hour,minute,tzinfo=UTC)


class GardenTests(unittest.TestCase):
    def setUp(self): self.store=Store(':memory:')
    def tearDown(self): self.store.close()
    def plant(self,request='one',crop='cabbage',quantity=1,now=None):
        self.store.garden_plant('g','u',request,{crop:quantity},now or at(12,50))
    def rows(self,now): return self.store.garden_snapshot('g','u',now)

    def test_first_payday_and_processing(self):
        self.plant()
        self.assertEqual(self.rows(at(12,59))[0]['progress'],0)
        with self.assertRaises(UserError): self.store.garden_action('g','u','process',now=at(12,59))
        self.assertEqual(self.rows(at(13))[0]['progress'],1)
        self.assertEqual(self.rows(at(14))[0]['progress'],1)
        self.store.garden_action('g','u','process',now=at(14,59))
        self.assertEqual(self.rows(at(15))[0]['progress'],2)
        self.assertEqual(self.rows(at(18))[0]['progress'],2)

    def test_all_crops_mature_and_yield(self):
        for i,crop in enumerate(CROPS): self.plant(str(i),crop,2)
        self.store.garden_action('g','u','fertilize',now=at(12,55))
        with self.assertRaises(UserError): self.store.garden_action('g','u','fertilize',now=at(12,56))
        for h in range(13,20):
            rows=self.rows(at(h))
            for row in rows:
                self.assertEqual(row['progress'],min(h-12,CROPS[row['crop']][1]))
            if h<19: self.store.garden_action('g','u','process',now=at(h,30))
        collected=self.store.garden_action('g','u','collect',now=at(19))
        self.assertEqual(sum(r['quantity']*16 for r in collected),128)
        self.assertEqual(self.rows(at(19)),[])

    def test_capacity_replay_and_isolation(self):
        self.plant(quantity=9)
        self.plant(quantity=9)
        self.assertEqual(sum(r['quantity'] for r in self.rows(at(13))),9)
        with self.assertRaises(UserError): self.plant('extra')
        self.assertEqual(self.store.garden_snapshot('other','u',at(13)),[])
        with self.assertRaises(UserError): self.store.garden_action('g','other','collect',1,at(20))
        for amount in (-1,0,10,1.5,True):
            with self.assertRaises(UserError): self.store.garden_plant('g','z',str(amount),{'cabbage':amount},at(12))

    def test_separate_batches_and_no_retroactive_credit(self):
        self.plant()
        self.plant('later','pumpkin',now=at(14,40))
        rows=self.rows(at(15))
        self.assertEqual([r['progress'] for r in rows],[1,1])
        self.store.garden_action('g','u','process',rows[1]['id'],at(15,10))
        self.assertEqual([r['progress'] for r in self.rows(at(16))],[1,2])

    def test_reminder_window_dedup_and_ripe(self):
        self.plant()
        from lafamily.garden_store import hour_at
        key=str(hour_at(at(13)))
        self.assertFalse(self.store.garden_claim('g','u','process',key,at(13,49)))
        self.assertTrue(self.store.garden_claim('g','u','process',key,at(13,50)))
        self.assertFalse(self.store.garden_claim('g','u','process',key,at(13,59)))
        for h in (13,14,15): self.store.garden_action('g','u','process',now=at(h,55))
        row=self.rows(at(16))[0]
        self.assertIsNotNone(row['ripe_hour'])
        self.assertFalse(self.store.garden_claim('g','u','process',str(hour_at(at(16))),at(16,50)))
        self.store.garden_action('g','u','collect',now=at(16))
        self.assertFalse(self.store.garden_claim('g','u','ripe',str(row['id']),at(16)))

    def test_restart_and_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'garden.db'
            store=Store(path)
            store.garden_plant('g','u','x',{'cabbage':1},at(12,50))
            store.garden_action('g','u','process',now=at(13,30))
            store.close()
            store=Store(path)
            self.assertEqual(store.garden_snapshot('g','u',at(18))[0]['progress'],2)
            store.close()


class GardenAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:')
        self.recipient=SimpleNamespace(send=AsyncMock())
        self.client=SimpleNamespace(fetch_user=AsyncMock(return_value=self.recipient),backups=SimpleNamespace(restoring=False))
        self.service=GardenService(self.client,self.store,123)
        self.store.garden_plant('123','456','one',{'cabbage':1},at(12,50))
    async def asyncTearDown(self): self.store.close()

    async def test_service_dedup_restart_and_restore(self):
        await self.service.tick(at(12,50))
        self.assertIn('удобрение',self.recipient.send.call_args.args[0])
        self.assertNotIn('Нужна обработка',self.recipient.send.call_args.args[0])
        self.recipient.send.reset_mock()
        await self.service.tick(at(13,50))
        await GardenService(self.client,self.store,123).tick(at(13,51))
        self.recipient.send.assert_awaited_once()
        self.client.backups.restoring=True
        await self.service.tick(at(14,50))
        self.recipient.send.assert_awaited_once()

    async def test_timeout_is_not_retried(self):
        self.recipient.send.side_effect=asyncio.TimeoutError()
        await self.service.tick(at(13,50))
        await self.service.tick(at(13,51))
        self.recipient.send.assert_awaited_once()
        state=self.store.db.execute('SELECT state FROM garden_deliveries').fetchone()[0]
        self.assertEqual(state,'uncertain')

    async def test_owner_checks_and_components(self):
        interaction=SimpleNamespace(guild_id=123,user=SimpleNamespace(id=789))
        view=GardenView(self.service,456,[])
        with self.assertRaises(UserError): await view.interaction_check(interaction)
        interaction.user.id=456
        self.assertTrue(await view.interaction_check(interaction))
        interaction.guild_id=999
        with self.assertRaises(UserError): self.service.check(interaction,456)
        self.assertEqual(len(PlantModal(self.service,456,'cabbage').children),2)
        embed,card=await self.service.card(456)
        self.assertIn('Капуста',embed.description)

    async def test_fetch_crossing_hour_drops_stale_reminder(self):
        current=[at(13,59)]
        async def fetch(user):
            current[0]=at(14)
            return self.recipient
        self.client.fetch_user.side_effect=fetch
        service=GardenService(self.client,self.store,123,clock=lambda:current[0])
        await service.tick()
        self.recipient.send.assert_not_awaited()

    async def test_delayed_claim_crossing_hour_expires_without_send(self):
        from unittest.mock import patch
        from lafamily.async_database import database_call as real_database_call
        current=[at(13,59)]
        async def delayed_call(store, method, *args, **kwargs):
            result=await real_database_call(store,method,*args,**kwargs)
            if method == 'garden_claim':
                current[0]=at(14)
            return result
        service=GardenService(self.client,self.store,123,clock=lambda:current[0])
        with patch('lafamily.modules.garden.database_call',side_effect=delayed_call):
            await service.tick()
        self.recipient.send.assert_not_awaited()
        self.assertEqual(self.store.db.execute('SELECT state FROM garden_deliveries').fetchone()[0],'expired')

    async def test_no_fetch_when_no_pending_notification(self):
        await self.service.tick(at(12,49))
        self.client.fetch_user.assert_not_awaited()
        await self.service.tick(at(13,50))
        self.client.fetch_user.reset_mock()
        await self.service.tick(at(13,51))
        self.client.fetch_user.assert_not_awaited()

    async def test_combined_reminders_independent_states(self):
        for processed,fertilized in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(processed=processed,fertilized=fertilized):
                store=Store(':memory:')
                try:
                    store.garden_plant('123','456','a',{'cabbage':1},at(12,50))
                    if processed: store.garden_action('123','456','process',now=at(13,10))
                    if fertilized: store.garden_action('123','456','fertilize',now=at(13,10))
                    self.recipient.send.reset_mock()
                    await GardenService(self.client,store,123).tick(at(13,50))
                    if processed and fertilized:
                        self.recipient.send.assert_not_awaited()
                    else:
                        self.recipient.send.assert_awaited_once()
                        text=self.recipient.send.call_args.args[0]
                        self.assertEqual('Нужна обработка' in text,not processed)
                        self.assertEqual('Не отмечено удобрение' in text,not fertilized)
                        self.assertNotIn('шт.',text)
                finally: store.close()

    async def test_modal_late_entry_and_no_product_numbers(self):
        modal=PlantModal(self.service,456,'pumpkin')
        modal.quantity._value='2'
        modal.remaining._value='0'
        interaction=SimpleNamespace(guild_id=123,user=SimpleNamespace(id=456),id=999,
            response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())
        await modal.on_submit(interaction)
        rows=self.store.garden_snapshot('123','456')
        self.assertIsNotNone(rows[-1]['ripe_hour'])
        embed=interaction.edit_original_response.call_args.kwargs['embed']
        self.assertNotIn('шт.',str(embed.to_dict()))
        self.assertNotIn('урожай:',embed.description)
        view=GardenView(self.service,456,rows,rows[-1]['id'])
        interaction.response.is_done=lambda:True
        await view.action(interaction,'collect')
        self.assertEqual(interaction.edit_original_response.call_args.kwargs['content'],'Растения собраны. Места освобождены.')

    async def test_modal_rejects_bad_input_without_writes(self):
        for quantity,remaining in (('0',''),('-1',''),('2.5',''),('٢',''),('2','5'),('2','-1'),('2','2 / 3')):
            with self.subTest(quantity=quantity,remaining=remaining):
                modal=PlantModal(self.service,456,'cabbage')
                modal.quantity._value=quantity
                modal.remaining._value=remaining
                interaction=SimpleNamespace(guild_id=123,user=SimpleNamespace(id=456),id=999,
                    response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())
                with self.assertRaises(UserError): await modal.on_submit(interaction)
                interaction.response.defer.assert_not_awaited()
        self.assertEqual(len(self.store.garden_snapshot('123','456')),1)

    async def test_optional_remaining_blank_uses_fresh_defaults(self):
        from lafamily.garden_store import hour_at
        for i,(crop,(_,duration)) in enumerate(CROPS.items()):
            modal=PlantModal(self.service,456,crop)
            self.assertFalse(modal.remaining.required)
            self.assertEqual(modal.remaining.placeholder,f'По умолчанию: {duration}')
            modal.quantity._value='1'
            modal.remaining._value=''
            interaction=SimpleNamespace(guild_id=123,user=SimpleNamespace(id=456),id=1000+i,
                response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())
            await modal.on_submit(interaction)
            row=self.store.garden_snapshot('123','456')[-1]
            self.assertEqual(row['progress'],0)
            self.assertEqual(row['planted_hour'],hour_at())

    async def test_culture_choice_and_access(self):
        from lafamily.modules.garden import CultureView
        view=CultureView(self.service,456)
        select=view.children[0]
        self.assertEqual({option.value for option in select.options},set(CROPS))
        interaction=SimpleNamespace(guild_id=123,user=SimpleNamespace(id=456),response=SimpleNamespace(send_modal=AsyncMock()))
        select._values=['pineapple']
        await select.callback(interaction)
        self.assertEqual(interaction.response.send_modal.call_args.args[0].crop,'pineapple')
        interaction.user.id=789
        with self.assertRaises(UserError): await view.interaction_check(interaction)
        with self.assertRaises(UserError): await select.callback(interaction)



class LateGardenTests(unittest.TestCase):
    def test_remaining_semantics_and_validation(self):
        for crop,(_,duration) in CROPS.items():
            for remaining in range(duration+1):
                with self.subTest(crop=crop,remaining=remaining):
                    store=Store(':memory:')
                    try:
                        store.garden_plant('g','u','a',{crop:1},at(12,50),remaining={crop:remaining})
                        row=store.garden_snapshot('g','u',at(12,51))[0]
                        self.assertEqual(duration-row['progress'],remaining)
                        self.assertEqual(row['ripe_hour'] is not None,remaining==0)
                        self.assertEqual(store.garden_snapshot('g','u',at(13))[0]['progress'],duration-remaining)
                        if remaining:
                            store.garden_action('g','u','process',now=at(13,10))
                            self.assertEqual(store.garden_snapshot('g','u',at(14))[0]['progress'],duration-remaining+1)
                    finally: store.close()
        store=Store(':memory:')
        try:
            for value in (-1,5,True,1.5,'2'):
                with self.assertRaises(UserError):
                    store.garden_plant('g','u','a',{'cabbage':1},at(12),remaining={'cabbage':value})
            for rem in ({'unknown':1},{'pumpkin':1}):
                with self.assertRaises(UserError): store.garden_plant('g','u','a',{'cabbage':1},at(12),remaining=rem)
            self.assertEqual(store.garden_snapshot('g','u',at(13)),[])
            with self.assertRaises(UserError): store.garden_claim('g','u','unknown','1',at(13))
        finally: store.close()

class ReminderButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from lafamily.garden_store import hour_at
        self.store=Store(':memory:')
        self.key=str(hour_at(at(13,50)))
        self.store.garden_plant('123','456','one',{'cabbage':1},at(12,50))
        self.store.garden_claim('123','456','process',self.key,at(13,50))
        self.client=SimpleNamespace(user=SimpleNamespace(id=999),backups=SimpleNamespace(restoring=False))
        self.service=GardenService(self.client,self.store,123,clock=lambda:at(13,55))
        self.client.garden=self.service
        self.interaction=SimpleNamespace(client=self.client,guild_id=None,user=SimpleNamespace(id=456),
            message=SimpleNamespace(author=SimpleNamespace(id=999)),
            response=SimpleNamespace(type=None,defer=AsyncMock(),is_done=lambda:False,send_message=AsyncMock()),
            edit_original_response=AsyncMock())
    async def asyncTearDown(self): self.store.close()

    async def button(self,action):
        import re
        from lafamily.modules.garden import GardenReminderButton
        item=GardenReminderButton(action,'123','456',self.key)
        match=re.fullmatch(GardenReminderButton.__discord_ui_compiled_template__,item.custom_id)
        return await GardenReminderButton.from_custom_id(self.interaction,item.item,match)

    async def test_reconstructed_callback_updates_and_excludes_later_plants(self):
        self.store.garden_plant('123','456','later',{'pumpkin':1},at(13,51))
        for action in ('process','fertilize'):
            button=await self.button(action)
            await button.callback(self.interaction)
            self.interaction.response.defer.assert_awaited_with(ephemeral=True,thinking=True)
            self.assertEqual(self.interaction.edit_original_response.call_args.kwargs.keys(),{'content'})
            self.assertIn('отмечен',self.interaction.edit_original_response.call_args.kwargs['content'])
            await button.callback(self.interaction)
            self.assertIn('Уже отмечено',self.interaction.edit_original_response.call_args.kwargs['content'])
        rows=self.store.garden_snapshot('123','456',at(13,59))
        self.assertIsNotNone(rows[0]['processed_hour'])
        self.assertTrue(rows[0]['fertilized'])
        self.assertIsNone(rows[1]['processed_hour'])
        self.assertFalse(rows[1]['fertilized'])

    async def test_old_processing_rejected_fertilizer_allowed(self):
        self.service.clock=lambda:at(14,10)
        await (await self.button('process')).callback(self.interaction)
        self.assertIn('прошёл',self.interaction.response.send_message.call_args.args[0])
        self.assertIsNone(self.store.garden_snapshot('123','456',at(14,10))[0]['processed_hour'])
        await (await self.button('fertilize')).callback(self.interaction)
        self.assertTrue(self.store.garden_snapshot('123','456',at(14,10))[0]['fertilized'])

    async def test_foreign_context_and_restore_rejected(self):
        button=await self.button('process')
        for field,value in (('user',SimpleNamespace(id=777)),('guild_id',123),
                            ('message',SimpleNamespace(author=SimpleNamespace(id=777)))):
            old=getattr(self.interaction,field)
            setattr(self.interaction,field,value)
            await button.callback(self.interaction)
            self.interaction.response.defer.assert_not_awaited()
            setattr(self.interaction,field,old)
        self.service.guild_id=777
        await button.callback(self.interaction)
        self.interaction.response.defer.assert_not_awaited()
        self.service.guild_id=123
        self.client.backups.restoring=True
        await button.callback(self.interaction)
        self.interaction.response.defer.assert_not_awaited()

    async def test_delivery_states_targets_and_ripe(self):
        for state in ('sending','sent','uncertain'):
            self.store.garden_delivery_result('123','456','process',self.key,state)
            self.store.garden_reminder_action('123','456',self.key,'process',at(13,55))
        for state in ('expired','forbidden'):
            self.store.garden_delivery_result('123','456','process',self.key,state)
            with self.assertRaises(UserError): self.store.garden_reminder_action('123','456',self.key,'fertilize',at(13,55))
        self.store.garden_delivery_result('123','456','process',self.key,'sent')
        self.store.db.execute('UPDATE garden_batches SET ripe_hour=checked_hour')
        self.assertEqual(self.store.garden_reminder_action('123','456',self.key,'fertilize',at(13,55)),0)
        self.store.db.execute('DELETE FROM garden_reminder_targets')
        with self.assertRaises(UserError): self.store.garden_reminder_action('123','456',self.key,'fertilize',at(13,55))

    async def test_restart_and_old_schema_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.db'
            store=Store(path)
            store.db.execute('DROP TABLE garden_reminder_targets')
            store.garden_plant('123','456','one',{'cabbage':1},at(12,50))
            store.close()
            store=Store(path)
            store.garden_claim('123','456','process',self.key,at(13,50))
            store.close()
            store=Store(path)
            try:
                self.assertEqual(store.garden_reminder_action('123','456',self.key,'process',at(13,55)),1)
                self.assertEqual(store.garden_reminder_action('123','456',self.key,'process',at(13,56)),0)
            finally: store.close()

    async def test_buttons_match_notification_reasons(self):
        from lafamily.modules.garden import reminder_view
        for processing,fertilizer in ((True,True),(True,False),(False,True)):
            view=reminder_view('123','456',self.key,processing,fertilizer)
            self.assertIsNone(view.timeout)
            self.assertEqual([item.item.label for item in view.children],
                (['Обработано'] if processing else [])+(['Удобрено'] if fertilizer else []))
