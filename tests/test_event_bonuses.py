import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
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
        return SimpleNamespace(guild_id=int(G),user=SimpleNamespace(id=int(A),roles=[SimpleNamespace(id=role)]),guild=SimpleNamespace(),permissions=discord.Permissions.none(),response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())

    async def test_only_manager_configures_bonuses(self):
        with self.assertRaises(UserError):await self.commands.bonuses.callback(self.commands,self.interaction(),str(self.kind),True,15,7)
        await self.commands.bonuses.callback(self.commands,self.interaction('manager'),str(self.kind),True,15,7)
        self.assertEqual(self.store.event_type(G,self.kind)['win_bonus'],15)

    async def test_outcome_required_when_enabled_and_disabled_otherwise(self):
        self.store.configure_event_bonuses(G,A,self.kind,True,15,7)
        with self.assertRaises(UserError):await self.commands.create_report.callback(self.commands,self.interaction(),str(self.kind))
        self.store.configure_event_bonuses(G,A,self.kind,False,15,7)
        with self.assertRaises(UserError):await self.commands.create_report.callback(self.commands,self.interaction(),str(self.kind),outcome='win')

    async def test_organizer_draft_snapshots_rewards_and_checks_mvp_roster(self):
        self.store.configure_event_bonuses(G,A,self.kind,True,15,7)
        interaction=self.interaction()
        await self.commands.create_report.callback(self.commands,interaction,str(self.kind),outcome='win',mvp=SimpleNamespace(id=int(B),bot=False))
        view=self.service.views[(G,A)]
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
