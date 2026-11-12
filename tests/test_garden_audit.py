"""Real executor queue and transaction-entry clock regressions for garden care."""
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from lafamily.async_database import DatabaseWorker
from lafamily.database import Store
from lafamily.garden_store import hour_at
from lafamily.modules.garden import GardenReminderButton, GardenService


def at(hour, minute=0):
    return datetime(2026,11,12,hour,minute,tzinfo=UTC)


class GardenQueueClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_button_cannot_mark_previous_hour(self):
        async with DatabaseWorker(':memory:') as worker:
            key=str(hour_at(at(13)))
            await worker.call('garden_plant','123','456','one',{'cabbage':1},at(12,50))
            await worker.call('garden_claim','123','456','process',key,at(13,50))
            current=[at(13,59)]
            client=SimpleNamespace(user=SimpleNamespace(id=999),backups=SimpleNamespace(restoring=False))
            client.garden=GardenService(client,worker,123,clock=lambda:current[0])
            interaction=SimpleNamespace(client=client,guild_id=None,user=SimpleNamespace(id=456),
                message=SimpleNamespace(author=SimpleNamespace(id=999)),
                response=SimpleNamespace(type=None,defer=AsyncMock(),is_done=lambda:False,send_message=AsyncMock()),
                edit_original_response=AsyncMock())
            release=threading.Event()
            blocked=threading.Event()
            def occupy_executor():
                blocked.set()
                if not release.wait(10): raise RuntimeError('Test executor was not released')
            block=worker.executor.submit(occupy_executor)
            self.assertTrue(await asyncio.to_thread(blocked.wait,3))
            queued=asyncio.Event()
            original_call=worker.call
            async def observed_call(method,*args,**kwargs):
                if method=='garden_reminder_action': queued.set()
                return await original_call(method,*args,**kwargs)
            worker.call=observed_call
            task=asyncio.create_task(GardenReminderButton('process','123','456',key).callback(interaction))
            try:
                await asyncio.wait_for(queued.wait(),3)
                current[0]=at(14)
            finally:
                release.set()
            await task
            block.result(timeout=3)
            row=(await worker.call('garden_snapshot','123','456',at(14)))[0]
            self.assertEqual(row['progress'],1,'queued previous-hour click must not grant a second PayDay')
            self.assertIsNone(row['processed_hour'])
            self.assertIn('прошёл',interaction.response.send_message.call_args.args[0])


class GardenTransactionClockTests(unittest.TestCase):
    def test_clock_read_after_transaction_acquisition(self):
        for action in ('plant','process','claim','reminder'):
            with self.subTest(action=action):
                store=Store(':memory:')
                try:
                    key=str(hour_at(at(13)))
                    store.garden_plant('g','u','one',{'cabbage':1},at(12,50))
                    store.garden_claim('g','u','process',key,at(13,50))
                    current=[at(13,59)]
                    original=store.transaction
                    @contextmanager
                    def delayed_begin():
                        with original():
                            current[0]=at(14)
                            yield
                    store.transaction=delayed_begin
                    if action=='plant':
                        store.garden_plant('g','u','later',{'pumpkin':1},lambda:current[0])
                        self.assertEqual(store.garden_snapshot('g','u',at(14))[-1]['planted_hour'],hour_at(at(14)))
                    elif action=='process':
                        store.garden_action('g','u','process',now=lambda:current[0])
                        row=store.garden_snapshot('g','u',at(14))[0]
                        self.assertEqual(row['processed_hour'],hour_at(at(14)))
                        self.assertEqual(row['progress'],1)
                    elif action=='claim':
                        store.db.execute('DELETE FROM garden_deliveries')
                        self.assertEqual(store.garden_claim('g','u','process',key,lambda:current[0]),[])
                    else:
                        from lafamily.errors import UserError
                        with self.assertRaises(UserError):
                            store.garden_reminder_action('g','u',key,'process',lambda:current[0])
                finally: store.close()
