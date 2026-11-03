"""Согласованные архивы, расписание, проверки ZIP и восстановление через новый worker."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

import discord
from discord import app_commands

from lafamily.async_database import DatabaseWorker
from lafamily.config import Config
from lafamily.database import Store
from lafamily.database_archives import (apply_restore, inspect_archive, pack_database, snapshot, FatalRestoreError)
from lafamily.errors import UserError
from lafamily.modules.database_backups import (DatabaseBackups, RestoreView, register_database_backups)
from lafamily.modules.events.models import Draft
from lafamily.release import VERSION

G='111111111111111111'
A='222222222222222222'
C='333333333333333333'
B='444444444444444444'
NOW=datetime(2026,11,3,12,tzinfo=UTC)


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'live.sqlite'
        self.worker=await DatabaseWorker(self.path).__aenter__()
        self.addAsyncCleanup(self.close_worker)
        await self.worker.call('configure',G,A,C,'organizer','manager')
        kind=await self.worker.call('set_type',G,A,'МП',10)
        self.draft=Draft(G,A,C,kind['id'],'МП',10,'2026-11-02T12:00:00.000Z',members=[A])
        await self.worker.call('create_report',self.draft)
        await self.worker.call('set_price_subscription',G,A,'Медь',True)
        self.guild=SimpleNamespace(id=int(G),me=object(),filesize_limit=8*1024*1024)
        self.channel=MagicMock(spec=discord.TextChannel)
        self.channel.id=int(C)
        self.channel.guild=self.guild
        self.channel.permissions_for.return_value=SimpleNamespace(view_channel=True,send_messages=True,
            attach_files=True,read_message_history=True)
        self.client=SimpleNamespace(user=SimpleNamespace(id=int(B)),is_ready=lambda:False,
            fetch_channel=AsyncMock(return_value=self.channel),close=AsyncMock(),restore_request=None)
        self.sent=[]
        self.messages={}
        async def send(**payload):
            file=payload['file']
            data=file.fp.read()
            ident=555555555555555555+len(self.sent)
            attachment=SimpleNamespace(filename=file.filename,size=len(data),read=AsyncMock(return_value=data))
            message=SimpleNamespace(id=ident,jump_url=f'https://discord.com/channels/{G}/{C}/{ident}',
                author=self.client.user,attachments=[attachment])
            self.sent.append((payload['content'],data))
            self.messages[ident]=message
            return message
        self.channel.send=AsyncMock(side_effect=send)
        self.channel.fetch_message=AsyncMock(side_effect=lambda ident:self.messages[ident])
        self.service=DatabaseBackups(self.client,self.worker,int(G))
        self.addAsyncCleanup(self.service.close)
        await self.worker.call('configure_database_backups',G,A,C,NOW)

    async def close_worker(self):
        if not self.worker.closing:
            await self.worker.__aexit__(None,None,None)

    def interaction(self,admin=True):
        return SimpleNamespace(guild_id=int(G),guild=self.guild,user=SimpleNamespace(id=int(A)),
            permissions=discord.Permissions(administrator=admin),
            response=SimpleNamespace(defer=AsyncMock(),edit_message=AsyncMock()),
            edit_original_response=AsyncMock())

    async def make_archive(self):
        message=await self.service.create(force=True)
        return await self.service.prepare(self.interaction(),str(message.id))

    async def test_manual_backup_includes_wal_data_and_admin_metadata(self):
        message=await self.service.create(force=True)
        content,data=self.sent[0]
        for word in ('Дата:','Москва','Размер БД','ZIP','записей','Отчётов','активных','отменённых','Участников','баллов','подписчиков','SHA-256',VERSION):
            self.assertIn(word,content)
        self.assertLessEqual(len(content),2000)
        self.assertEqual(self.channel.send.call_args.kwargs['allowed_mentions'].to_dict(),{'parse':[]})
        archive=Path(self.folder.name)/'download.zip'
        archive.write_bytes(data)
        target=Path(self.folder.name)/'checked.sqlite'
        manifest,summary=inspect_archive(archive,target,G,B,VERSION)
        self.assertEqual((summary['reports'],summary['participants']),(1,1))
        self.assertEqual((manifest['summary']['points'],manifest['summary']['subscribers']),(10,1))
        self.assertGreater(manifest['summary']['records'],1)
        reopened=Store(target)
        try:
            self.assertEqual(reopened.report(G,1)['points'],10)
        finally:
            reopened.close()
        setting=await self.worker.call('database_backup_settings',G)
        self.assertEqual((setting['state'],setting['message_id']),('sent',str(message.id)))
        self.assertIsNone(await self.service.create())
        self.assertEqual(len(self.sent),1)

    async def test_six_hour_schedule_survives_restart_and_failed_upload_is_not_looped(self):
        setting=await self.worker.call('claim_database_backup',G,NOW)
        self.assertIsNotNone(setting)
        await self.worker.call('finish_database_backup',G,setting['last_attempt'],'uncertain')
        self.assertIsNone(await self.worker.call('claim_database_backup',G,NOW+timedelta(hours=5,minutes=59)))
        await self.close_worker()
        self.worker=await DatabaseWorker(self.path).__aenter__()
        self.service.store=self.worker
        self.assertIsNone(await self.worker.call('claim_database_backup',G,NOW+timedelta(hours=5)))
        due=await self.worker.call('claim_database_backup',G,NOW+timedelta(hours=6))
        self.assertIsNotNone(due)
        self.channel.send.side_effect=OSError('uncertain')
        with self.assertRaises(UserError):
            await self.service.create(force=True)
        setting=await self.worker.call('database_backup_settings',G)
        self.assertEqual(setting['state'],'uncertain')
        await self.service.create()
        self.channel.send.assert_awaited_once()

    async def test_configure_first_backup_disable_and_permissions(self):
        await self.service.configure(self.interaction(),self.channel)
        self.assertEqual(len(self.sent),1)
        await self.service.configure(self.interaction(),None)
        self.assertIsNone(await self.service.create())
        with self.assertRaises(UserError):
            await self.service.create(force=True)
        self.channel.permissions_for.return_value.attach_files=False
        with self.assertRaises(UserError):
            await self.service.configure(self.interaction(),self.channel)
        self.channel.permissions_for.return_value.attach_files=True
        self.channel.id = int(C)+1
        await self.worker.call('configure_prices',G,A,str(self.channel.id))
        with self.assertRaises(UserError):
            await self.service.configure(self.interaction(),self.channel)

    async def test_file_limit_does_not_change_live_database(self):
        self.guild.filesize_limit=1
        with self.assertRaises(UserError):
            await self.service.create(force=True)
        self.assertEqual((await self.worker.call('report',G,1))['points'],10)
        self.channel.send.assert_not_awaited()
        self.assertEqual((await self.worker.call('database_backup_settings',G))['state'],'failed')

    async def test_all_commands_require_administrator(self):
        client=discord.Client(intents=discord.Intents.none())
        self.addAsyncCleanup(client.close)
        tree=app_commands.CommandTree(client)
        register_database_backups(tree,self.service,int(G))
        group=tree.get_command('бот',guild=discord.Object(id=int(G))).get_command('бэкапы')
        for name,args in [('настройки',()),('создать',()),('восстановить',('555555555555555555',))]:
            command=group.get_command(name)
            self.assertEqual(command.extras['access'],'admin')
            with self.assertRaises(UserError):
                await command.callback(self.interaction(False),*args)
        self.channel.send.assert_not_awaited()

    async def test_restore_safety_archive_freeze_and_real_database_replacement(self):
        request=await self.make_archive()
        self.addCleanup(request.cleanup)
        kind=await self.worker.call('set_type',G,A,'Новое МП',20)
        await self.worker.call('create_report',Draft(G,A,C,kind['id'],'Новое МП',20,'2026-11-03T12:00:00.000Z',members=[A]))
        await self.service.restore(self.interaction(),request)
        self.assertTrue(self.worker.paused)
        self.client.close.assert_awaited_once()
        self.assertIs(self.client.restore_request,request)
        self.assertEqual(len(self.sent),2)
        safety_zip=Path(request.safety_archive)
        self.assertTrue(safety_zip.is_file())
        with ZipFile(safety_zip) as archive:
            manifest=json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['summary']['points'],30)
        with self.assertRaises(UserError):
            await self.worker.call('set_type',G,A,'Недопустимо',100)
        await self.close_worker()
        apply_restore(request,self.path)
        restored=Store(self.path)
        try:
            self.assertEqual(restored.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],1)
            self.assertEqual(restored.report(G,1)['points'],10)
            self.assertEqual(restored.database_backup_settings(G)['channel_id'],C)
            self.assertEqual(restored.db.execute("SELECT COUNT(*) FROM audit_log WHERE action='database.restored'").fetchone()[0],1)
        finally:
            restored.close()

    async def test_failed_safety_upload_aborts_restore_and_resumes_old_worker(self):
        request=await self.make_archive()
        self.addCleanup(request.cleanup)
        self.channel.send.side_effect=OSError('failed safety upload')
        with self.assertRaises(UserError):
            await self.service.restore(self.interaction(),request)
        self.assertFalse(self.worker.paused)
        self.assertFalse(self.service.restoring)
        self.assertIsNone(self.client.restore_request)
        self.client.close.assert_not_awaited()
        self.assertEqual((await self.worker.call('report',G,1))['points'],10)
        self.assertTrue(request.safety_archive.is_file())

    async def test_message_must_be_in_backup_channel_and_belong_to_this_bot(self):
        message=await self.service.create(force=True)
        for ident in ('bad','123'):
            with self.assertRaises(UserError):
                await self.service.prepare(self.interaction(),ident)
        message.author=SimpleNamespace(id=123)
        with self.assertRaises(UserError):
            await self.service.prepare(self.interaction(),str(message.id))
        message.attachments[0].read.assert_not_awaited()
        self.client.close.assert_not_awaited()

    async def test_confirmation_cancel_timeout_and_role_recheck(self):
        request=await self.make_archive()
        view=RestoreView(self.service,self.interaction(),request)
        self.addCleanup(view.stop)
        stranger=self.interaction()
        stranger.user.id=123
        with self.assertRaises(UserError):
            await view.confirm.callback(stranger)
        with self.assertRaises(UserError):
            await view.confirm.callback(self.interaction(False))
        self.assertFalse(view.finished)
        await view.cancel.callback(self.interaction())
        self.assertFalse(Path(request.directory.name).exists())
        self.client.close.assert_not_awaited()
        request=await self.make_archive()
        other=RestoreView(self.service,self.interaction(),request)
        await other.on_timeout()
        self.assertFalse(Path(request.directory.name).exists())
        with self.assertRaises(UserError):
            await other.confirm.callback(self.interaction())

    async def test_frozen_worker_drains_previous_writes_and_rejects_new_ones(self):
        pending=asyncio.create_task(self.worker.call('set_type',G,A,'До барьера',99))
        await asyncio.sleep(0)
        target=Path(self.folder.name)/'frozen.sqlite'
        await self.worker.freeze_and_export(target)
        await pending
        with self.assertRaises(UserError):
            await self.worker.call('set_type',G,A,'После барьера',999)
        copied=Store(target)
        try:
            self.assertIn('До барьера',[r['name'] for r in copied.types(G)])
            self.assertNotIn('После барьера',[r['name'] for r in copied.types(G)])
        finally:
            copied.close()
        self.worker.resume()
        self.assertEqual(len(await self.worker.call('types',G)),2)

    async def test_restore_copy_error_rolls_back_and_double_failure_stops_startup(self):
        request=await self.make_archive()
        self.addCleanup(request.cleanup)
        await self.service.restore(self.interaction(),request)
        await self.close_worker()
        from lafamily.database_archives import copy_into_existing
        calls=[]
        def fail_first(source,target):
            calls.append(source)
            if len(calls)==1:
                raise OSError('restore failed')
            return copy_into_existing(source,target)
        with patch('lafamily.database_archives.copy_into_existing',side_effect=fail_first):
            with self.assertRaises(OSError):
                apply_restore(request,self.path)
        self.assertEqual(calls,[request.database,request.safety_database])
        reopened=Store(self.path)
        self.assertEqual(reopened.report(G,1)['points'],10)
        reopened.close()
        with patch('lafamily.database_archives.copy_into_existing',side_effect=OSError('disk')):
            with self.assertRaises(FatalRestoreError):
                apply_restore(request,self.path)

    async def test_main_reconnects_with_a_new_worker_after_closing_old_one(self):
        request=await self.make_archive()
        self.addCleanup(request.cleanup)
        kind=await self.worker.call('set_type',G,A,'Позже',20)
        await self.worker.call('create_report',Draft(G,A,C,kind['id'],'Позже',20,'2026-11-03T12:00:00.000Z',members=[A]))
        await self.close_worker()
        original_client=self.client
        interaction=self.interaction()
        created=[]
        outer=self
        class FakeBot:
            def __init__(self,config,store):
                self.store=store
                self.user=original_client.user
                self.fetch_channel=original_client.fetch_channel
                self.restore_request=None
                self.startup_notice=None
                self.backups=DatabaseBackups(self,store,int(G))
                created.append(self)
            async def __aenter__(self):
                return self
            async def __aexit__(self,*args):
                await self.close()
            async def close(self):
                pass
            async def start(self,token):
                if len(created)==1:
                    await self.backups.restore(interaction,request)
                else:
                    outer.assertTrue(created[0].store.closing)
                    rows=await self.store.call('points_summary',G,('2000','2100'))
                    outer.assertEqual(sum(r['total_points'] for r in rows),10)
                    outer.assertIn('БД восстановлена',self.startup_notice[1])
        import main
        config=Config('fake',int(B),int(G),self.path)
        with patch.object(main,'load_config',return_value=config),patch.object(main,'FamilyBot',FakeBot):
            await main.run()
        self.assertEqual(len(created),2)
        self.assertTrue(all(b.store.closing for b in created))
        self.assertFalse(Path(request.directory.name).exists())

    async def test_snapshot_metadata_counts_cancellations_and_excludes_their_points(self):
        kind=await self.worker.call('set_type',G,A,'Тест',25)
        report=await self.worker.call('create_report',Draft(G,A,C,kind['id'],'Тест',25,
            '2026-11-02T13:00:00.000Z',members=[A,C]))
        await self.worker.call('cancel_report',G,A,report['id'],'Проверка')
        request=await self.make_archive()
        self.addCleanup(request.cleanup)
        row=request.manifest['summary']
        self.assertEqual((row['reports'],row['active'],row['cancelled']),(2,1,1))
        self.assertEqual((row['participants'],row['active_participants'],row['points']),(2,1,10))
        checked=Store(request.database)
        try:
            self.assertTrue(checked.report(G,report['id'])['cancelled_at'])
        finally:
            checked.close()

    async def test_changed_channel_invalidates_pending_restore(self):
        request=await self.make_archive()
        self.addCleanup(request.cleanup)
        await self.worker.call('configure_database_backups',G,A,'999999999999999999')
        with self.assertRaises(UserError):
            await self.service.restore(self.interaction(),request)
        self.assertFalse(self.worker.paused)
        self.client.close.assert_not_awaited()

    async def test_freeze_failure_resumes_worker_without_closing_database(self):
        target=Path(self.folder.name)/'missing-directory'/'copy.sqlite'
        with self.assertRaises(sqlite3.OperationalError):
            await self.worker.freeze_and_export(target)
        self.assertFalse(self.worker.paused)
        self.assertEqual((await self.worker.call('report',G,1))['points'],10)

    async def test_scheduler_starts_only_when_ready_and_once_per_connection(self):
        with patch.object(self.service.poll,'start') as start:
            self.service.start()
            start.assert_not_called()
            self.client.is_ready=lambda:True
            with patch.object(self.service.poll,'is_running',side_effect=[False,True]):
                self.service.start()
                self.service.start()
            start.assert_called_once()


class ArchiveValidationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        store=Store(self.root/'live.sqlite')
        store.configure(G,A,C,'organizer','manager')
        snapshot(store.db,self.root/'copy.sqlite')
        store.close()
        self.archive=self.root/'backup.zip'
        pack_database(self.root/'copy.sqlite',self.archive,G,B,VERSION)

    def mutate(self,callback):
        with ZipFile(self.archive) as archive:
            files={n:archive.read(n) for n in archive.namelist()}
        callback(files)
        with ZipFile(self.archive,'w',compression=ZIP_DEFLATED) as archive:
            for name,data in files.items():
                archive.writestr(name,data)

    def reject(self):
        target=self.root/'restored.sqlite'
        if target.exists():
            target.unlink()
        with self.assertRaises(UserError):
            inspect_archive(self.archive,self.root/'restored.sqlite',G,B,VERSION)

    def test_checksum_and_wrong_server_bot_version_are_rejected(self):
        for field,value in [('guild_id','other'),('bot_id','other'),('version','999.0.0'),('sha256','0'*64)]:
            with self.subTest(field=field):
                pack_database(self.root/'copy.sqlite',self.archive,G,B,VERSION)
                def change(files):
                    manifest=json.loads(files['manifest.json'])
                    manifest[field]=value
                    files['manifest.json']=json.dumps(manifest).encode()
                self.mutate(change)
                self.reject()

    def test_traversal_extra_files_and_duplicate_names_are_rejected(self):
        self.mutate(lambda files:files.update({'../outside.sqlite':b'bad'}))
        self.reject()
        self.assertFalse((self.root.parent/'outside.sqlite').exists())
        pack_database(self.root/'copy.sqlite',self.archive,G,B,VERSION)
        with ZipFile(self.archive,'a') as archive:
            with self.assertWarns(UserWarning):
                archive.writestr('manifest.json','{}')
        self.reject()

    def test_corrupt_sqlite_and_oversized_database_are_rejected(self):
        self.mutate(lambda files:files.update({'lafamily.sqlite':b'X'*1024}))
        self.reject()
        pack_database(self.root/'copy.sqlite',self.archive,G,B,VERSION)
        with patch('lafamily.database_archives.MAX_DATABASE_BYTES',1):
            self.reject()

    def test_unexpected_schema_triggers_and_broken_foreign_keys_are_rejected(self):
        c=sqlite3.connect(self.root/'copy.sqlite')
        c.execute('CREATE TRIGGER unexpected AFTER INSERT ON reports BEGIN DELETE FROM reports; END')
        c.close()
        pack_database(self.root/'copy.sqlite',self.archive,G,B,VERSION)
        self.reject()
        c=sqlite3.connect(self.root/'copy.sqlite')
        c.execute('DROP TRIGGER unexpected')
        c.execute('INSERT INTO report_members VALUES (999,?)',(A,))
        c.commit()
        c.close()
        pack_database(self.root/'copy.sqlite',self.archive,G,B,VERSION)
        self.reject()
