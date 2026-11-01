import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.payouts import allocate_bank, send_payout
from lafamily.modules.events.time import week_range

G='111111111111111111'
A='222222222222222222'
B='333333333333333333'


class AllocationTests(unittest.TestCase):
    def test_proportional_and_entire_bank(self):
        rows=allocate_bank(100,[{'user_id':A,'points':1},{'user_id':B,'points':2}])
        self.assertEqual({r['user_id']:r['amount'] for r in rows},{A:33,B:67})
        self.assertEqual(sum(r['amount'] for r in rows),100)

    def test_tie_is_stable_small_bank_and_zero_points(self):
        rows=[{'user_id':B,'points':1},{'user_id':A,'points':1},{'user_id':'4','points':0}]
        result=allocate_bank(1,rows)
        self.assertEqual([(r['user_id'],r['amount']) for r in result],[(A,1),(B,0)])
        self.assertEqual(result,allocate_bank(1,list(reversed(rows))))
        self.assertNotIn('amount',rows[0])

    def test_invalid_and_empty(self):
        for bank in (0,-1,True,1.5,1000000001):
            with self.assertRaises(UserError):allocate_bank(bank,[{'user_id':A,'points':1}])
        for rows in ([],[{'user_id':A,'points':0}]):
            with self.assertRaises(UserError):allocate_bank(100,rows)

    def test_large_integer_precision(self):
        rows=[{'user_id':str(i+1),'points':10**15+i} for i in range(101)]
        self.assertEqual(sum(r['amount'] for r in allocate_bank(1000000000,rows)),1000000000)


class PayoutCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.store.set_type(G,A,'МП',10)
        self.kind=self.store.types(G)[0]['id']

    def interaction(self, admin=True):
        return SimpleNamespace(guild_id=int(G),guild=SimpleNamespace(),
            user=SimpleNamespace(id=int(A),roles=[]),
            permissions=discord.Permissions(administrator=admin),
            response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())

    async def test_admin_access_and_no_points(self):
        with self.assertRaises(UserError):await send_payout(self.interaction(False),self.store,100,0)
        with self.assertRaises(UserError):await send_payout(self.interaction(),self.store,100,0)

    async def test_all_members_attachment_and_week_parameter(self):
        interaction=self.interaction()
        rows=[{'user_id':str(100+i),'points':1} for i in range(23)]
        with patch('lafamily.modules.events.payouts.database_call',new=AsyncMock(return_value=rows)) as query:
            await send_payout(interaction,self.store,100,-1)
        self.assertEqual(query.call_args.kwargs['limit'],-1)
        self.assertEqual(query.call_args.args[3],week_range(-1))
        payload=interaction.edit_original_response.call_args.kwargs
        file=payload['attachments'][0]
        self.addCleanup(file.close)
        lines=file.fp.read().decode('utf-8-sig').splitlines()
        self.assertEqual(len(lines),24)
        self.assertEqual(sum(int(line.split(';')[-1]) for line in lines[1:]),100)
        self.assertFalse(payload['allowed_mentions'].everyone)
        self.assertLess(len(payload['embed'].description),4096)

    async def test_week_bonuses_cancel_and_guild_isolation_without_mutation(self):
        start=week_range(-1)[0]
        draft=Draft(G,A,A,self.kind,'МП',10,start,members=[A,B],outcome='win',win_bonus=5,mvp_id=B,mvp_bonus=5)
        kept=self.store.create_report(draft)
        cancelled=self.store.create_report(Draft(G,A,A,self.kind,'МП',100,start,members=[A]))
        self.store.cancel_report(G,A,cancelled['id'],'тест')
        self.store.set_type('other',A,'МП',100)
        other=self.store.types('other')[0]['id']
        self.store.create_report(Draft('other',A,A,other,'МП',100,start,members=[A]))
        before=self.store.db.total_changes
        interaction=self.interaction()
        await send_payout(interaction,self.store,70,-1)
        text=interaction.edit_original_response.call_args.kwargs['embed'].description
        self.assertIn('**35**',text)
        self.assertIn('**$30**',text)
        self.assertIn('**$40**',text)
        self.assertEqual(self.store.db.total_changes,before)
        self.assertEqual(self.store.report(G,kept['id'])['points'],15)
