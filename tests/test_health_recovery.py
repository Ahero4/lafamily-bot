import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.time import timestamp
from lafamily.modules.events.ui import report_embed
from lafamily.modules.health import HealthService

G='111111111111111111';U='222222222222222222';C='444444444444444444'


class HealthRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        kind=self.store.set_type(G,U,'Капт',10)
        self.report=self.store.create_report(Draft(G,U,C,kind['id'],'Капт',10,timestamp(),members=[U]))
        self.messages=[]
        async def history(**kwargs):
            for message in self.messages:yield message
        self.channel=SimpleNamespace(id=int(C),send=AsyncMock(),history=history,
            permissions_for=lambda member:SimpleNamespace(read_message_history=True))
        self.guild=SimpleNamespace(id=int(G),me=object())
        self.client=SimpleNamespace(user=SimpleNamespace(id=789),is_ready=lambda:True,
            events=SimpleNamespace(channel=AsyncMock(return_value=self.channel)),fetch_channel=AsyncMock(return_value=self.channel))
        self.service=HealthService(self.client,self.store,int(G));self.addAsyncCleanup(self.service.close)

    async def test_cancelled_report_recovers_original_active_card_without_new_points(self):
        message=SimpleNamespace(id=800,author=self.client.user,embeds=[report_embed(self.report)],edit=AsyncMock())
        self.messages=[message]
        self.store.cancel_report(G,U,self.report['id'],'Тест')
        await self.service.recover_report(self.guild,self.report['id'])
        self.channel.send.assert_not_awaited();message.edit.assert_awaited_once()
        self.assertEqual(self.store.report(G,self.report['id'])['message_id'],'800')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],1)

    async def test_uncertain_republication_is_never_repeated_without_finding_card(self):
        self.channel.send.side_effect=OSError('network')
        with self.assertRaises(OSError):await self.service.recover_report(self.guild,self.report['id'])
        with self.assertRaises(UserError):await self.service.recover_report(self.guild,self.report['id'])
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.participant_profile(G,U,('2000','2100'))['total'],10)

    async def test_monitor_only_announces_changed_actionable_state(self):
        self.store.configure_admin_report_channel(G,U,C)
        self.service.inspect=AsyncMock(return_value={'warnings':['Бэкап просрочен']})
        await self.service.poll();await self.service.poll()
        self.channel.send.assert_awaited_once()
        self.service.inspect.return_value={'warnings':[]}
        await self.service.poll()
        self.channel.send.assert_awaited_once()
        self.service.inspect.return_value={'warnings':['Бэкап просрочен']}
        await self.service.poll()
        self.assertEqual(self.channel.send.await_count,2)


if __name__=='__main__':unittest.main()
