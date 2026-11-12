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
        self.recipient.send.assert_not_awaited()
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
        self.assertEqual(len(PlantModal(self.service,456).children),4)
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

    async def test_no_fetch_when_no_pending_notification(self):
        await self.service.tick(at(12,50))
        self.client.fetch_user.assert_not_awaited()
        await self.service.tick(at(13,50))
        self.client.fetch_user.reset_mock()
        await self.service.tick(at(13,51))
        self.client.fetch_user.assert_not_awaited()
