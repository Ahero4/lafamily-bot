"""Explicit garden progress correction preserves care and avoids automatic credit."""
from datetime import UTC,datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import tempfile
from pathlib import Path
import unittest
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.garden_store import CROPS
from lafamily.modules.garden import GardenService,GardenView,CorrectPaydayModal


def at(hour,minute=0): return datetime(2026,11,12,hour,minute,tzinfo=UTC)


class GardenCorrectionTests(unittest.TestCase):
    def test_forgot_mark_pineapple_and_preserved_state(self):
        store=Store(':memory:')
        try:
            store.garden_plant('g','u','one',{'pineapple':2},at(12,50))
            store.garden_action('g','u','fertilize',now=at(12,55))
            for h in (13,14,15,16): store.garden_action('g','u','process',now=at(h,30))
            row=store.garden_snapshot('g','u',at(18))[0]
            self.assertEqual(row['progress'],5)
            store.garden_correct_remaining('g','u',row['id'],1,row,'fix',at(18,10))
            corrected=store.garden_snapshot('g','u',at(18,11))[0]
            self.assertEqual(corrected['progress'],6)
            for key in ('id','quantity','fertilized','processed_hour'):
                self.assertEqual(corrected[key],row[key])
            self.assertEqual(store.garden_snapshot('g','u',at(19))[0]['progress'],6)
            self.assertFalse(store.garden_correct_remaining('g','u',row['id'],1,row,'fix',at(19)))
        finally: store.close()

    def test_all_ranges_current_care_and_no_automatic_credit(self):
        for crop,(_,duration) in CROPS.items():
            for remaining in range(duration+1):
                for processed in (False,True):
                    with self.subTest(crop=crop,remaining=remaining,processed=processed):
                        store=Store(':memory:')
                        try:
                            store.garden_plant('g','u','one',{crop:1},at(12,10))
                            row=store.garden_snapshot('g','u',at(12,11))[0]
                            store.garden_correct_remaining('g','u',row['id'],remaining,row,'fix',at(12,12))
                            if processed and remaining: store.garden_action('g','u','process',now=at(12,15))
                            corrected=store.garden_snapshot('g','u',at(13))[0]
                            self.assertEqual(corrected['progress'],duration-remaining+(1 if processed and remaining else 0))
                            self.assertEqual(corrected['ripe_hour'] is not None,remaining==0 or (processed and remaining==1))
                        finally: store.close()

    def test_validation_stale_isolation_and_preserve_concurrent_care(self):
        store=Store(':memory:')
        try:
            store.garden_plant('g','u','one',{'cabbage':1},at(12,50))
            row=store.garden_snapshot('g','u',at(13))[0]
            for remaining in (-1,5,True,1.5,'1'):
                with self.assertRaises(UserError): store.garden_correct_remaining('g','u',row['id'],remaining,row,'bad',at(13))
            with self.assertRaises(UserError): store.garden_correct_remaining('g','other',row['id'],1,row,'bad',at(13))
            store.garden_action('g','u','process',now=at(13,10))
            store.garden_action('g','u','fertilize',now=at(13,11))
            store.garden_correct_remaining('g','u',row['id'],2,row,'fix',at(13,12))
            latest=store.garden_snapshot('g','u',at(13,13))[0]
            self.assertIsNotNone(latest['processed_hour'])
            self.assertTrue(latest['fertilized'])
            with self.assertRaises(UserError): store.garden_correct_remaining('g','u',row['id'],1,row,'stale',at(13,14))
            with self.assertRaises(UserError): store.garden_correct_remaining('g','u',row['id'],1,latest,'newhour',at(14))
            store.garden_correct_remaining('g','u',row['id'],0,latest,'ripe',at(13,15))
            with self.assertRaises(UserError): store.garden_correct_remaining('g','u',row['id'],2,latest,'reopen',at(13,16))
            store.garden_action('g','u','collect',now=at(13,17))
            with self.assertRaises(UserError): store.garden_correct_remaining('g','u',row['id'],1,latest,'collected',at(13,18))
        finally: store.close()

    def test_restart_correction_and_dosadka(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'garden.db'
            store=Store(path)
            store.garden_plant('g','u','one',{'pineapple':1},at(12,50))
            row=store.garden_snapshot('g','u',at(13))[0]
            store.garden_plant('g','u','later',{'cabbage':1},at(13,10))
            store.garden_correct_remaining('g','u',row['id'],1,row,'fix',at(13,11))
            store.close()
            store=Store(path)
            try:
                rows=store.garden_snapshot('g','u',at(13,12))
                self.assertEqual([r['progress'] for r in rows],[6,0])
                self.assertFalse(store.garden_correct_remaining('g','u',row['id'],1,row,'fix',at(13,13)))
            finally: store.close()


class CorrectionModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_modal_selected_only_owner_and_maturity_notification(self):
        store=Store(':memory:')
        try:
            recipient=SimpleNamespace(send=AsyncMock())
            client=SimpleNamespace(fetch_user=AsyncMock(return_value=recipient),backups=SimpleNamespace(restoring=False))
            service=GardenService(client,store,123,clock=lambda:at(13,10))
            store.garden_plant('123','456','one',{'pineapple':1},at(12,50))
            rows=store.garden_snapshot('123','456',at(13,10))
            interaction=SimpleNamespace(guild_id=123,user=SimpleNamespace(id=456),id=999,
                response=SimpleNamespace(send_modal=AsyncMock(),defer=AsyncMock()),edit_original_response=AsyncMock())
            view=GardenView(service,456,rows)
            with self.assertRaises(UserError): await view.correct.callback(interaction)
            view.selected=rows[0]['id']
            await view.correct.callback(interaction)
            modal=interaction.response.send_modal.call_args.args[0]
            self.assertEqual(modal.remaining.default,'6')
            modal.remaining._value='0'
            interaction.user.id=789
            with self.assertRaises(UserError): await modal.on_submit(interaction)
            interaction.user.id=456
            await modal.on_submit(interaction)
            self.assertIn('отметьте отдельно',interaction.edit_original_response.call_args.kwargs['content'])
            await service.tick(at(13,11))
            await service.tick(at(13,12))
            recipient.send.assert_awaited_once()
            self.assertIn('Созрело',recipient.send.call_args.args[0])
            self.assertEqual(len(store.garden_snapshot('123','456',at(13,12))),1)
        finally: store.close()
