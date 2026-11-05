import tempfile
import unittest
from datetime import UTC,datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.time import timestamp
from lafamily.modules.prices.parser import parse_prices
from lafamily.modules.prices.queue import PriceQueue,enqueue_notifications
from lafamily.modules.prices.parser import price_dm_revision
from lafamily.modules.health import HealthService
from lafamily.confirmations import ConfirmAction

G='111111111111111111';U='222222222222222222';V='333333333333333333';C='444444444444444444'
EXAMPLE=Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.future=timestamp(datetime.now(UTC)+timedelta(hours=1))
        self.event=self.store.create_planned_event(G,U,'1',C,'Капт',self.future,1)

    def test_reserve_promotion_and_maybe_order(self):
        self.store.signup_planned(G,self.event['id'],U,'going')
        event=self.store.signup_planned(G,self.event['id'],V,'going')
        self.assertEqual((event['going'],event['reserve']),([U],[V]))
        self.store.signup_planned(G,self.event['id'],U,'maybe')
        event=self.store.planned_event(G,self.event['id'])
        self.assertEqual((event['going'],event['reserve'],event['maybe']),([V],[],[U]))
        self.store.signup_planned(G,self.event['id'],U,'going')
        self.store.signup_planned(G,self.event['id'],U,'going')
        event=self.store.signup_planned(G,self.event['id'],V,'no')
        self.assertEqual(event['going'],[U])

    def test_isolation_closure_and_start_time(self):
        with self.assertRaises(UserError):self.store.signup_planned('other',self.event['id'],U,'going')
        self.store.close_planned(G,U,self.event['id'])
        with self.assertRaises(UserError):self.store.signup_planned(G,self.event['id'],U,'going')
        self.store.db.execute('UPDATE planned_events SET closed=0,starts_at=?',(timestamp(datetime.now(UTC)-timedelta(seconds=1)),))
        with self.assertRaises(UserError):self.store.signup_planned(G,self.event['id'],U,'going')

    def test_reopen_database_keeps_signup_and_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.sqlite'
            with_store=Store(path)
            event=with_store.create_planned_event(G,U,'1',C,'Капт',self.future,1)
            with_store.signup_planned(G,event['id'],U,'going')
            with_store.bind_planned_message(G,event['id'],'900')
            with_store.close()
            with_store=Store(path)
            try:self.assertEqual(with_store.planned_by_message(G,'900')['going'],[U])
            finally:with_store.close()

    def test_prices_channel_separation_in_both_directions(self):
        with self.assertRaises(UserError):self.store.configure_prices(G,U,C)
        self.store.configure_prices(G,U,'5')
        with self.assertRaises(UserError):self.store.create_planned_event(G,U,'2','5','Другой',self.future,10)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.store.configure_prices(G,U,C)
        for item in ('Медь','Капуста'):self.store.set_price_subscription(G,U,item,True)
        self.report=parse_prices(EXAMPLE.replace('- Медь: $36 ($43)','- Медь: $60 ($72)').replace('- Капуста: $720','- Капуста: $748'))
        self.revision=price_dm_revision(self.report)
        self.store.activate_price_dm_revision(G,'100',self.revision)
        self.member=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=800,author=SimpleNamespace(id=700),channel=SimpleNamespace(id=600))))
        self.guild=SimpleNamespace(id=int(G),fetch_member=AsyncMock(return_value=self.member))
        self.channel=SimpleNamespace(id=int(C),guild=self.guild,permissions_for=lambda member:SimpleNamespace(view_channel=True))
        self.client=SimpleNamespace(user=SimpleNamespace(id=700),fetch_channel=AsyncMock(return_value=self.channel))
        self.message=SimpleNamespace(id=100,guild=self.guild,channel=self.channel)
        self.queue=PriceQueue(self.client,self.store,int(G));self.addAsyncCleanup(self.queue.close)

    async def enqueue(self):await enqueue_notifications(self.store,self.message,self.report)

    async def test_multiple_products_share_one_message_and_delivery_statistics(self):
        await self.enqueue();await self.queue.drain()
        self.member.send.assert_awaited_once()
        content=self.member.send.call_args.kwargs['content']
        self.assertIn('Медь',content);self.assertIn('Капуста',content);self.assertIn('МСК',content)
        self.assertLessEqual(len(content),2000)
        summary=self.store.price_delivery_summary(G,1)
        self.assertEqual((summary['messages'],summary['sent'],summary['recipients']),(1,1,1))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM price_delivery_attempts').fetchone()[0],2)
        self.assertEqual(self.store.price_delivery_data(G,'2000','2100')['daily'][0]['messages'],1)

    async def test_restart_resumes_queued_but_never_repeats_sending(self):
        await self.enqueue()
        await PriceQueue(self.client,self.store,int(G)).drain()
        self.member.send.assert_awaited_once()
        self.store.claim_price_alerts(G,'101',[])
        self.message.id=102;self.store.activate_price_dm_revision(G,'102',self.revision)
        await self.enqueue()
        self.store.claim_price_job(G)  # прерывание после начала задачи
        await PriceQueue(self.client,self.store,int(G)).drain()
        self.assertEqual(self.member.send.await_count,1)
        self.assertEqual(self.store.price_delivery_summary(G,2)['uncertain'],1)

    async def test_stale_job_and_revoked_access_never_send(self):
        await self.enqueue();self.store.activate_price_dm_revision(G,'101','different')
        await self.queue.drain();self.member.send.assert_not_awaited()
        self.assertEqual(self.store.price_delivery_summary(G,1)['failed'],1)

    async def test_ambiguous_send_keeps_uncertain_without_automatic_retry(self):
        await self.enqueue();self.member.send.side_effect=OSError('network')
        await self.queue.drain();await self.queue.drain()
        self.member.send.assert_awaited_once()
        self.assertEqual(self.store.price_delivery_summary(G,1)['uncertain'],1)

    async def test_before_send_network_error_is_deferred_and_closed_ls_is_terminal(self):
        await self.enqueue();self.client.fetch_channel.side_effect=OSError('offline')
        await self.queue.drain();self.member.send.assert_not_awaited()
        self.assertEqual(self.store.db.execute('SELECT state FROM delivery_jobs').fetchone()[0],'queued')
        self.store.db.execute("UPDATE delivery_jobs SET ready_at='2000'")
        self.client.fetch_channel.side_effect=None
        self.member.send.side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='closed'),'closed')
        await self.queue.drain();await self.queue.drain()
        self.member.send.assert_awaited_once()
        self.assertEqual(self.store.price_delivery_summary(G,1)['failed'],1)

    async def test_fresh_permission_denial_keeps_subscriptions(self):
        await self.enqueue();self.channel.permissions_for=lambda member:SimpleNamespace(view_channel=False)
        await self.queue.drain();self.member.send.assert_not_awaited()
        self.assertEqual(len(self.store.price_subscriptions(G,U)),2)


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_user_and_revoked_permissions_cannot_confirm(self):
        store=Store(':memory:');self.addCleanup(store.close)
        original=SimpleNamespace(user=SimpleNamespace(id=int(U)),guild_id=int(G))
        action=AsyncMock(return_value='OK')
        view=ConfirmAction(store,original,'admin',action)
        foreign=SimpleNamespace(user=SimpleNamespace(id=int(V)),guild_id=int(G),response=SimpleNamespace(type=None,send_message=AsyncMock(),is_done=lambda:False))
        self.assertFalse(await view.interaction_check(foreign));action.assert_not_awaited()
        current=SimpleNamespace(user=original.user,guild_id=int(G),guild=object(),permissions=discord.Permissions.none(),response=SimpleNamespace(defer=AsyncMock()))
        with self.assertRaises(UserError):await view.confirm.callback(current)
        action.assert_not_awaited()

    async def test_diagnostic_reports_unconfigured_backup_and_empty_database(self):
        store=Store(':memory:');self.addCleanup(store.close)
        service=HealthService(SimpleNamespace(),store,int(G))
        data=await service.inspect()
        self.assertTrue(any('бэкапы' in w for w in data['warnings']))
        self.assertTrue(any('пуста' in w for w in data['warnings']))


if __name__=='__main__':unittest.main()
