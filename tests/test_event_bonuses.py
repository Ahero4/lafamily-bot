import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import discord
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.commands import EventCommands
from lafamily.modules.events.models import Draft
from lafamily.modules.events.service import EventService
from lafamily.modules.events.time import parse_event_date, week_range
from lafamily.modules.events.ui import report_embed

G='111111111111111111'
A='222222222222222222'
B='333333333333333333'
C='444444444444444444'

class BonusStorageTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.set_type(G,A,'МП',10)
        self.kind=self.store.types(G)[0]['id']

    def draft(self, **kwargs):
        return Draft(G,A,C,self.kind,'МП',10,parse_event_date(None),members=[A,B],**kwargs)

    def test_victory_mvp_statistics_duplicate_and_cancel(self):
        draft=self.draft(outcome='win',win_bonus=15,mvp_id=B,mvp_bonus=7)
        report=self.store.create_report(draft)
        self.assertEqual(report['points'],25)
        self.assertEqual(self.store.create_report(draft)['id'],report['id'])
        rows={row['user_id']:row for row in self.store.points_summary(G,week_range())}
        self.assertEqual((rows[A]['total_points'],rows[B]['total_points'],rows[B]['week_points']),(25,32,32))
        self.assertEqual(self.store.statistics(G,week_range(),B)[0]['points'],32)
        self.assertEqual(self.store.statistics('other',week_range()),[])
        self.assertIn('32',str(report_embed(report).to_dict()))
        self.store.cancel_report(G,A,report['id'],'ошибка')
        self.assertEqual(self.store.points_summary(G,week_range()),[])

    def test_without_victory_mvp_and_snapshot_survives_rate_change(self):
        self.store.configure_event_bonuses(G,A,self.kind,True,15,7)
        draft=self.draft(outcome='no_win',mvp_id=B,mvp_bonus=7)
        self.store.configure_event_bonuses(G,A,self.kind,False,99,100)
        report=self.store.create_report(draft)
        self.assertEqual(report['points'],10)
        self.assertEqual(report['mvp_bonus'],7)
        old=self.store.create_report(self.draft())
        self.assertEqual((old['points'],old['mvp_bonus']),(10,0))

    def test_invalid_mvp_bonus_and_guild_do_not_create_reports(self):
        for kwargs in ({'mvp_id':'555555555555555555','mvp_bonus':5},{'mvp_id':B,'mvp_bonus':0},{'win_bonus':15},{'outcome':'bad'},{'mvp_bonus':-1}):
            with self.assertRaises(UserError):self.store.create_report(self.draft(**kwargs))
        with self.assertRaises(UserError):self.store.configure_event_bonuses('other',A,self.kind,True,15,7)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],0)

    def test_edit_rates_preserves_identity_history_and_omitted_values(self):
        self.store.set_type(G,A,'МП',10,15,7)
        draft=self.draft(outcome='win',win_bonus=15,mvp_id=B,mvp_bonus=7)
        old=self.store.create_report(draft)
        self.store.set_type(G,A,'МП',points=30)
        kind=self.store.event_type(G,self.kind)
        self.assertEqual((kind['points'],kind['win_bonus'],kind['mvp_bonus'],kind['win_enabled']),(30,15,7,1))
        self.assertEqual(len(self.store.types(G)),1)
        self.assertEqual(self.store.report(G,old['id'])['points'],25)
        self.assertEqual((draft.points,draft.win_bonus,draft.mvp_bonus),(10,15,7))
        self.store.set_type(G,A,'МП',win_bonus=0,mvp_bonus=0)
        kind=self.store.event_type(G,self.kind)
        self.assertEqual((kind['points'],kind['win_bonus'],kind['mvp_bonus'],kind['win_enabled']),(30,0,0,0))

    def test_new_type_requires_base_and_invalid_update_is_atomic(self):
        with self.assertRaises(UserError):self.store.set_type(G,A,'Новый',win_bonus=5)
        for kwargs in ({'win_bonus':-1},{'mvp_bonus':1000001},{'points':True}):
            with self.assertRaises(UserError):self.store.set_type(G,A,'МП',**kwargs)
        self.assertEqual(self.store.event_type(G,self.kind)['points'],10)
        self.assertEqual(len(self.store.types(G)),1)
        kind=self.store.set_type(G,A,'Новый',0)
        self.assertEqual((kind['win_bonus'],kind['mvp_bonus'],kind['win_enabled']),(0,0,0))
        self.store.disable_type(G,A,self.kind)
        self.assertEqual(self.store.set_type(G,A,'МП',25)['id'],self.kind)

    def test_migration_preserves_legacy_report_and_reopens(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'legacy.sqlite'
            db=sqlite3.connect(path)
            db.executescript('''CREATE TABLE event_types(id INTEGER PRIMARY KEY,guild_id TEXT NOT NULL,name TEXT NOT NULL,points INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,UNIQUE(guild_id,name));
            CREATE TABLE reports(id INTEGER PRIMARY KEY,guild_id TEXT NOT NULL,request_id TEXT NOT NULL UNIQUE,organizer_id TEXT NOT NULL,type_name TEXT NOT NULL,points INTEGER NOT NULL,occurred_at TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL,cancelled_at TEXT,cancel_reason TEXT,channel_id TEXT NOT NULL,message_id TEXT);''')
            db.execute('INSERT INTO reports(id,guild_id,request_id,organizer_id,type_name,points,occurred_at,note,created_at,channel_id) VALUES(1,?,?,?,?,?,?,?,?,?)',(G,'legacy',A,'МП',10,parse_event_date(None),'',parse_event_date(None),C))
            db.commit();db.close()
            for _ in range(2):
                store=Store(path)
                try:self.assertEqual((store.report(G,1)['points'],store.report(G,1)['mvp_bonus']),(10,0))
                finally:store.close()

class BonusCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.store.configure(G,A,C,'organizer','manager')
        self.store.set_type(G,A,'МП',10);self.kind=self.store.types(G)[0]['id']
        self.service=EventService(self.store);self.addCleanup(self.service.close)
        self.commands=EventCommands(self.service)

    def interaction(self,role='organizer'):
        return SimpleNamespace(guild_id=int(G),user=SimpleNamespace(id=int(A),roles=[SimpleNamespace(id=role)]),guild=SimpleNamespace(),channel=SimpleNamespace(get_partial_message=MagicMock(return_value=SimpleNamespace(edit=AsyncMock()))),permissions=discord.Permissions.none(),response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())

    async def test_only_manager_updates_type_and_preserves_id(self):
        with self.assertRaises(UserError):
            await self.commands.add_type.callback(self.commands,self.interaction(),'МП',10,15,7)
        await self.commands.add_type.callback(self.commands,self.interaction('manager'),'МП',10,15,7)
        await self.commands.add_type.callback(self.commands,self.interaction('manager'),'МП',win=20)
        kind=self.store.event_type(G,self.kind)
        self.assertEqual((kind['points'],kind['win_bonus'],kind['mvp_bonus']),(10,20,7))
        self.assertEqual(len(self.store.types(G)),1)
        choices=await self.commands.type_name_autocomplete(self.interaction('manager'),'М')
        self.assertEqual(choices[0].value,'МП')
        self.assertNotIn('бонусы',[c.name for c in self.commands.commands])

    async def test_outcome_required_in_view_only_when_enabled(self):
        self.store.configure_event_bonuses(G,A,self.kind,True,15,7)
        interaction=self.interaction()
        await self.commands.create_report.callback(self.commands,interaction,str(self.kind))
        view=self.service.views[(G,A)]
        view.draft.members=[A,B]
        self.assertTrue(view.payload()['view'].confirm.disabled)
        with self.assertRaises(UserError):await self.service.confirm(interaction,view)
        await view.dispatch('outcome',interaction,'win')
        self.assertEqual(view.draft.win_bonus,15)
        self.assertFalse(view.confirm.disabled)
        await view.dispatch('outcome',interaction,'no_win')
        self.assertEqual(view.draft.win_bonus,0)
        self.store.configure_event_bonuses(G,A,self.kind,False,0,0)
        await self.commands.create_report.callback(self.commands,interaction,str(self.kind))
        view=self.service.views[(G,A)]
        self.assertNotIn(view.select_outcome,view.children)
        self.assertNotIn(view.select_mvp,view.children)

    async def test_organizer_draft_snapshots_rewards_and_checks_mvp_roster(self):
        self.store.configure_event_bonuses(G,A,self.kind,True,15,7)
        interaction=self.interaction()
        await self.commands.create_report.callback(self.commands,interaction,str(self.kind))
        view=self.service.views[(G,A)]
        view.draft.members=[A,B]
        await view.dispatch('outcome',interaction,'win')
        await view.dispatch('mvp',interaction,[SimpleNamespace(id=int(B),bot=False)])
        self.assertEqual((view.draft.win_bonus,view.draft.mvp_bonus),(15,7))
        self.assertIn('25 очк.',view.payload()['content'])
        view.draft.members=[A]
        with self.assertRaises(UserError):await self.service.confirm(interaction,view)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],0)
        self.store.configure_event_bonuses(G,A,self.kind,False,30,40)
        view.draft.members=[A,B]
        self.service.channel=AsyncMock(return_value=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123))))
        interaction.guild.fetch_member=AsyncMock(return_value=SimpleNamespace(bot=False))
        await self.service.confirm(interaction,view)
        self.assertEqual(self.store.statistics(G,week_range(),B)[0]['points'],32)

    async def test_selection_mvp_validation_clear_permissions_and_snapshot(self):
        self.store.configure_event_bonuses(G,A,self.kind,True,15,7)
        interaction=self.interaction()
        await self.commands.create_report.callback(self.commands,interaction,str(self.kind))
        view=self.service.views[(G,A)]
        view.draft.members=[A,B]
        self.assertEqual(len({item.row for item in view.children}),5)
        for member in (SimpleNamespace(id=555555555555555555,bot=False),SimpleNamespace(id=int(B),bot=True)):
            with self.assertRaises(UserError):await view.dispatch('mvp',interaction,[member])
        foreign=self.interaction('organizer');foreign.user.id=int(B)
        with self.assertRaises(UserError):await view.dispatch('outcome',foreign,'win')
        self.store.configure_event_bonuses(G,A,self.kind,True,99,100)
        await view.dispatch('outcome',interaction,'win')
        await view.dispatch('mvp',interaction,[SimpleNamespace(id=int(B),bot=False)])
        self.assertEqual((view.draft.win_bonus,view.draft.mvp_bonus),(15,7))
        await view.dispatch('mvp',interaction,[])
        self.assertEqual((view.draft.mvp_id,view.draft.mvp_bonus),(None,0))
        await view.dispatch('mvp',interaction,[SimpleNamespace(id=int(A),bot=False)])
        self.assertEqual(view.draft.mvp_id,A)

    async def test_failed_selection_does_not_apply_unseen_rewards(self):
        self.store.configure_event_bonuses(G,A,self.kind,True,15,7)
        interaction=self.interaction()
        await self.commands.create_report.callback(self.commands,interaction,str(self.kind))
        view=self.service.views[(G,A)]
        view.draft.members=[A,B]
        interaction.edit_original_response.side_effect=RuntimeError('network')
        with self.assertRaises(RuntimeError):await view.dispatch('outcome',interaction,'win')
        self.assertEqual((view.draft.outcome,view.draft.win_bonus),('',0))
        with self.assertRaises(RuntimeError):await view.dispatch('mvp',interaction,[SimpleNamespace(id=int(B),bot=False)])
        self.assertEqual((view.draft.mvp_id,view.draft.mvp_bonus),(None,0))
        self.assertTrue(view.needs_refresh)

    async def test_manager_changes_foreign_draft_and_confirms_without_changing_author(self):
        self.store.configure_event_bonuses(G,A,self.kind,True,15,7)
        owner=self.interaction()
        await self.commands.create_report.callback(self.commands,owner,str(self.kind))
        view=self.service.views[(G,A)]
        view.draft.members=[A,B]
        manager=self.interaction('manager');manager.user.id=int(B)
        await view.dispatch('outcome',manager,'win')
        await view.dispatch('mvp',manager,[SimpleNamespace(id=int(B),bot=False)])
        self.service.channel=AsyncMock(return_value=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123))))
        manager.guild.fetch_member=AsyncMock(return_value=SimpleNamespace(bot=False))
        await view.dispatch('confirm',manager)
        self.assertTrue(view.finished)
        report=self.store.report(G,1)
        self.assertEqual((report['organizer_id'],report['points'],report['mvp_id']),(A,25,B))
        self.assertEqual(self.store.statistics(G,week_range(),B)[0]['points'],32)
        with self.assertRaises(UserError):await view.dispatch('clear',manager)

    async def test_draft_controls_check_current_role_owner_server_and_admin(self):
        owner=self.interaction()
        await self.commands.create_report.callback(self.commands,owner,str(self.kind))
        view=self.service.views[(G,A)];view.draft.members=[A,B]
        await view.validate(owner)
        for role in ('organizer','member'):
            other=self.interaction(role);other.user.id=int(B)
            with self.assertRaises(UserError):await view.dispatch('clear',other)
            self.assertEqual(view.draft.members,[A,B])
        manager=self.interaction('manager');manager.user.id=int(B)
        await view.validate(manager)
        manager.user.roles=[]
        with self.assertRaises(UserError):await view.dispatch('clear',manager)
        admin=self.interaction('member');admin.user.id=int(B)
        admin.permissions=discord.Permissions(administrator=True)
        await view.dispatch('clear',admin)
        self.assertEqual(view.draft.members,[])
        admin.guild_id=999
        with self.assertRaises(UserError):await view.validate(admin)
        owner.user.roles=[]
        with self.assertRaises(UserError):await view.validate(owner)
