"""Несколько ролей: перенос старых настроек, доступ и явное сохранение формы."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.permissions import require_access
from lafamily.settings_ui import SettingsView
from lafamily.modules.events.service import EventService
from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.info import build_info

GUILD='111111111111111111'
ACTOR='222222222222222222'
CHANNEL='333333333333333333'
ORG=['444444444444444444','555555555555555555']
MGR=['666666666666666666','777777777777777777']

def role(value):
    result=MagicMock(spec=discord.Role)
    result.id=int(value)
    result.mention=f'<@&{value}>'
    result.is_default.return_value=value==GUILD
    return result

class RoleStorageTests(unittest.TestCase):
    def test_old_schema_migrates_without_losing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.sqlite'
            db=sqlite3.connect(path)
            db.execute('CREATE TABLE settings (guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, organizer_role_id TEXT NOT NULL, manager_role_id TEXT NOT NULL)')
            db.execute('INSERT INTO settings VALUES (?,?,?,?)',(GUILD,CHANNEL,ORG[0],MGR[0]))
            db.commit();db.close()
            store=Store(path)
            try:
                self.assertEqual(store.settings(GUILD)['organizer_role_ids'],[ORG[0]])
                store.configure(GUILD,ACTOR,CHANNEL,ORG,MGR)
                store.configure(GUILD,ACTOR,CHANNEL,[ORG[1]],[MGR[1]])
            finally: store.close()
            store=Store(path)
            try:
                self.assertEqual(store.settings(GUILD)['organizer_role_ids'],[ORG[1]])
                self.assertEqual(store.settings(GUILD)['manager_role_ids'],[MGR[1]])
            finally: store.close()

    def test_roles_and_audit_are_atomic_and_everyone_is_rejected(self):
        store=Store(':memory:')
        try:
            store.configure(GUILD,ACTOR,CHANNEL,ORG,MGR)
            with patch.object(store,'audit',side_effect=RuntimeError('test')):
                with self.assertRaises(RuntimeError): store.configure(GUILD,ACTOR,CHANNEL,[ORG[1]],[MGR[1]])
            self.assertEqual(store.settings(GUILD)['organizer_role_ids'],ORG)
            for organizers in ([],[GUILD],ORG*26):
                if len(set(organizers))<=25 and organizers not in ([],[GUILD]): continue
                with self.assertRaises(UserError): store.configure(GUILD,ACTOR,CHANNEL,organizers,MGR)
        finally: store.close()

class RoleFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:'); self.addCleanup(self.store.close)
        self.store.configure(GUILD,ACTOR,CHANNEL,ORG[0],MGR[0])
        self.roles={value:role(value) for value in [*ORG,*MGR,GUILD]}
        self.guild=SimpleNamespace(get_role=lambda rid:self.roles.get(str(rid)),fetch_roles=AsyncMock(return_value=list(self.roles.values())))
        self.service=EventService(self.store);self.addCleanup(self.service.close)
        self.service.channel=AsyncMock(return_value=SimpleNamespace(id=int(CHANNEL)))
        self.view=SettingsView(self.service,self.request(),SimpleNamespace(id=int(CHANNEL)),self.store.settings(GUILD))
        self.addCleanup(self.view.finish)

    def request(self,admin=True,actor=ACTOR,roles=()):
        return SimpleNamespace(guild_id=int(GUILD),guild=self.guild,user=SimpleNamespace(id=int(actor),roles=[self.roles[r] for r in roles]),permissions=discord.Permissions(administrator=admin),response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())

    async def test_multiple_roles_save_only_after_button_and_each_grants_access(self):
        await self.view.handle(self.request(),'organizer',[self.roles[r] for r in ORG])
        await self.view.handle(self.request(),'manager',[self.roles[r] for r in MGR])
        self.assertEqual(self.store.settings(GUILD)['organizer_role_ids'],[ORG[0]])
        await self.view.handle(self.request(),'save')
        self.assertEqual(self.store.settings(GUILD)['organizer_role_ids'],ORG)
        for selected in ORG: require_access(self.request(False,roles=[selected]),self.store,'organizer')
        for selected in MGR:
            require_access(self.request(False,roles=[selected]),self.store,'organizer')
            require_access(self.request(False,roles=[selected]),self.store,'manager')
        with self.assertRaises(UserError): require_access(self.request(False,roles=[MGR[1]]),self.store,'admin')

    async def test_menu_defaults_and_serialization(self):
        self.view.payload()
        data=self.view.to_components()
        self.assertEqual(data[0]['components'][0]['max_values'],25)
        self.assertEqual(str(data[0]['components'][0]['default_values'][0]['id']),ORG[0])

    async def test_cancel_and_timeout_preserve_old_settings(self):
        await self.view.handle(self.request(),'organizer',[self.roles[ORG[1]]])
        await self.view.handle(self.request(),'cancel')
        self.assertEqual(self.store.settings(GUILD)['organizer_role_ids'],[ORG[0]])
        self.assertTrue(self.view.finished)

    async def test_other_user_lost_admin_and_everyone_cannot_save(self):
        for request in (self.request(actor=ORG[0]),self.request(admin=False)):
            with self.assertRaises(UserError): await self.view.handle(request,'save')
        with self.assertRaises(UserError): await self.view.handle(self.request(),'organizer',[self.roles[GUILD]])
        self.assertEqual(self.store.settings(GUILD)['organizer_role_ids'],[ORG[0]])

    async def test_deleted_role_and_failed_message_update_do_not_save_unseen_choices(self):
        failed=self.request();failed.edit_original_response.side_effect=RuntimeError('offline')
        with self.assertRaises(RuntimeError): await self.view.handle(failed,'organizer',[self.roles[ORG[1]]])
        self.assertEqual(self.view.organizers,[ORG[0]])
        await self.view.handle(self.request(),'save')
        self.assertFalse(self.view.finished)
        self.guild.fetch_roles.return_value=[]
        with self.assertRaises(UserError): await self.view.handle(self.request(),'save')

    async def test_info_lists_all_roles_within_discord_limits(self):
        organizers=[str(800000000000000000+i) for i in range(25)]
        managers=[str(900000000000000000+i) for i in range(25)]
        self.roles.update({r:role(r) for r in organizers+managers})
        self.store.configure(GUILD,ACTOR,CHANNEL,organizers,managers)
        bot=FamilyBot(Config('fake',123456789012345678,int(GUILD),Path(':memory:')),self.store)
        try:
            embed=build_info(self.request(),bot.tree,self.store)
            self.assertLessEqual(len(embed),6000)
            self.assertTrue(all(len(field.value)<=1024 for field in embed.fields))
            text=' '.join(field.value for field in embed.fields)
            for selected in organizers+managers: self.assertIn(f'<@&{selected}>',text)
        finally: await bot.close()
