import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.announcements import AnnouncementService, AnnouncementCommands, due_occurrences, parse_days, parse_clock, parse_start
from lafamily.modules.events.time import timestamp

G='111111111111111111'
START=datetime(2026,11,2,17,0,tzinfo=UTC)  # 20:00 Москва


class AnnouncementTimeTests(unittest.TestCase):
    def test_once_window_inclusive_start_exclusive_event(self):
        row={'kind':'once','starts_at':timestamp(START)}
        self.assertEqual(due_occurrences(row,START-timedelta(minutes=15,seconds=1)),[])
        self.assertEqual(due_occurrences(row,START-timedelta(minutes=15)),[START])
        self.assertEqual(due_occurrences(row,START-timedelta(seconds=1)),[START])
        self.assertEqual(due_occurrences(row,START),[])

    def test_weekly_moscow_midnight_and_weekdays(self):
        row={'kind':'weekly','days':'[0]','clock':'00:05'}
        now=datetime(2026,11,1,20,50,tzinfo=UTC)
        self.assertEqual(due_occurrences(row,now),[datetime(2026,11,1,21,5,tzinfo=UTC)])
        self.assertEqual(due_occurrences(row,now+timedelta(days=1)),[])

    def test_schedule_validation(self):
        self.assertEqual(parse_days('пн, ср пт'),[0,2,4])
        self.assertEqual(parse_days('ежедневно'),list(range(7)))
        self.assertEqual(parse_clock('23:59'),'23:59')
        for value in ('','пн,xxx'):
            with self.assertRaises(UserError):parse_days(value)
        for value in ('24:00','9:30','12:60'):
            with self.assertRaises(UserError):parse_clock(value)
        self.assertEqual(parse_start('2026-11-02 20:00',START-timedelta(hours=1)),timestamp(START))
        with self.assertRaises(UserError):parse_start('2026-11-02 20:00',START-timedelta(minutes=14))


class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.guild=SimpleNamespace(id=int(G),me=object())
        self.role=MagicMock(spec=discord.Role)
        self.role.id=123;self.role.guild=self.guild;self.role.mentionable=True
        self.role.is_default.return_value=False
        self.guild.get_role=lambda number:self.role if number==123 else None
        self.channel=MagicMock(spec=discord.TextChannel)
        self.channel.id=456;self.channel.guild=self.guild
        self.channel.permissions_for.return_value=SimpleNamespace(view_channel=True,send_messages=True,embed_links=True,
            read_message_history=True,mention_everyone=False,attach_files=True)
        self.channel.send=AsyncMock(return_value=SimpleNamespace(id=789,attachments=[SimpleNamespace(id=999)]))
        self.client=SimpleNamespace(is_ready=lambda:True,fetch_channel=AsyncMock(return_value=self.channel))
        self.service=AnnouncementService(self.client,self.store,int(G))
        self.addAsyncCleanup(self.service.close)
        self.template=self.store.save_announcement_template(G,'Капт','456','123','Сбор @everyone <@555>')
        self.schedule=self.store.create_announcement_schedule(G,self.template,'once',timestamp(START),None,None)

    async def test_send_only_selected_role_and_no_duplicate_on_restart(self):
        await asyncio.gather(self.service.tick(START-timedelta(minutes=15)),self.service.tick(START-timedelta(minutes=14)))
        await AnnouncementService(self.client,self.store,int(G)).tick(START-timedelta(minutes=10))
        self.channel.send.assert_awaited_once()
        payload=self.channel.send.call_args.kwargs
        self.assertEqual(payload['content'],'<@&123>')
        mentions=payload['allowed_mentions'].to_dict()
        self.assertEqual(mentions['roles'],[123])
        self.assertNotIn('everyone',mentions.get('parse',[]))
        self.assertNotIn('users',mentions.get('parse',[]))
        self.assertIn(str(int(START.timestamp())),str(payload['embed'].to_dict()))
        self.assertEqual(self.store.announcement_attempt(self.schedule,timestamp(START))['state'],'sent')

    async def test_offline_recovery_and_no_late_delivery(self):
        await self.service.tick(START-timedelta(minutes=16))
        self.channel.send.assert_not_awaited()
        await self.service.tick(START-timedelta(minutes=8))
        self.channel.send.assert_awaited_once()
        self.channel.send.reset_mock()
        self.store.create_announcement_schedule(G,self.template,'once',timestamp(START),None,None)
        await self.service.tick(START)
        self.channel.send.assert_not_awaited()

    async def test_failures_and_permissions_do_not_duplicate_tags(self):
        self.channel.permissions_for.return_value.send_messages=False
        with self.assertLogs('lafamily.errors',level='ERROR'):
            await self.service.tick(START-timedelta(minutes=15))
        self.assertIsNone(self.store.announcement_attempt(self.schedule,timestamp(START)))
        self.channel.permissions_for.return_value.send_messages=True
        self.channel.send.side_effect=RuntimeError('unknown result')
        with self.assertLogs('lafamily.errors',level='ERROR'):
            await self.service.tick(START-timedelta(minutes=14))
        await self.service.tick(START-timedelta(minutes=13))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.announcement_attempt(self.schedule,timestamp(START))['state'],'uncertain')

    async def test_disabled_schedule_and_unmentionable_role(self):
        self.role.mentionable=False
        with self.assertLogs('lafamily.errors',level='ERROR'):
            await self.service.tick(START-timedelta(minutes=15))
        self.channel.send.assert_not_awaited()
        self.role.mentionable=True
        self.store.disable_announcement_schedule(G,self.schedule)
        await self.service.tick(START-timedelta(minutes=14))
        self.channel.send.assert_not_awaited()
        with self.assertRaises(UserError):self.store.disable_announcement_schedule('other',self.schedule)

    async def test_saved_image_uploaded_once_refetched_and_missing_image_falls_back(self):
        self.store.configure_announcement_storage(G,'456')
        file=SimpleNamespace(close=MagicMock())
        attachment=SimpleNamespace(content_type='image/png',size=100,to_file=AsyncMock(return_value=file))
        reference=await self.service.upload_image(attachment)
        attachment.to_file.assert_awaited_once();file.close.assert_called_once()
        self.channel.fetch_message=AsyncMock(return_value=SimpleNamespace(attachments=[SimpleNamespace(id=999,url='https://cdn.discordapp.com/attachments/456/789/image.png')]))
        self.store.save_announcement_template(G,'Капт','456','123','Текст',reference)
        self.channel.send.reset_mock()
        await self.service.tick(START-timedelta(minutes=15))
        self.assertIn('image.png',self.channel.send.call_args.kwargs['embed'].image.url)
        self.assertNotIn('file',self.channel.send.call_args.kwargs)
        self.store.create_announcement_schedule(G,self.template,'once',timestamp(START+timedelta(days=1)),None,None)
        self.channel.fetch_message.return_value=SimpleNamespace(attachments=[])
        with self.assertLogs('lafamily.errors',level='ERROR'):
            await self.service.tick(START+timedelta(days=1)-timedelta(minutes=15))
        self.assertIn('Картинку',self.channel.send.call_args.kwargs['embed'].footer.text)
        attachment.to_file.assert_awaited_once()

    async def test_admin_commands_runtime_access_and_invalid_schedule(self):
        commands=AnnouncementCommands(self.service)
        request=SimpleNamespace(guild_id=int(G),user=SimpleNamespace(id=1,roles=[]),guild=self.guild,
            permissions=discord.Permissions.none(),response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())
        for command,args in ((commands.listing,()),(commands.disable,(self.schedule,)),
                             (commands.once,(str(self.template),'2026-12-01 20:00')),
                             (commands.template,('Test',self.channel,self.role,'Text'))):
            with self.assertRaises(UserError):await command.callback(commands,request,*args)
        request.permissions=discord.Permissions(administrator=True)
        await commands.weekly.callback(commands,request,str(self.template),'пн,ср','20:00')
        self.assertEqual(len(self.store.announcement_schedules(G)),2)
        with self.assertRaises(UserError):await commands.weekly.callback(commands,request,str(self.template),'xxx','20:00')
        self.assertEqual(len(self.store.announcement_schedules(G)),2)

    def test_persistence_template_edit_and_guild_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'anons.sqlite'
            store=Store(path)
            number=store.save_announcement_template(G,'МП','456','123','Первый','image-ref')
            self.assertEqual(store.save_announcement_template(G,'МП','456','123','Новый'),number)
            self.assertEqual(store.announcement_template(G,number)['image_ref'],'image-ref')
            schedule=store.create_announcement_schedule(G,number,'once',timestamp(START),None,None)
            store.claim_announcement(schedule,timestamp(START));store.close()
            store=Store(path)
            try:
                self.assertEqual(store.announcement_schedules(G)[0]['body'],'Новый')
                self.assertFalse(store.claim_announcement(schedule,timestamp(START)))
                self.assertEqual(store.announcement_schedules('other'),[])
                with self.assertRaises(UserError):store.create_announcement_schedule('other',number,'once',timestamp(START),None,None)
                store.save_announcement_template(G,'МП','456','123','Текст',None,True)
                self.assertIsNone(store.announcement_template(G,number)['image_ref'])
            finally:store.close()

    async def test_image_validation_autocomplete_and_scheduler_lifecycle(self):
        attachment=SimpleNamespace(content_type='text/plain',size=100,to_file=AsyncMock())
        with self.assertRaises(UserError):await self.service.upload_image(attachment)
        attachment.to_file.assert_not_awaited()
        commands=AnnouncementCommands(self.service)
        request=SimpleNamespace(guild_id=int(G),user=SimpleNamespace(id=1,roles=[]),guild=self.guild,
            permissions=discord.Permissions.none())
        self.assertEqual(await commands.choices(request,''),[])
        request.permissions=discord.Permissions(administrator=True)
        self.assertEqual((await commands.choices(request,'Кап'))[0].value,str(self.template))
        from unittest.mock import patch
        with patch.object(self.service,'poll') as loop:
            loop.is_running.side_effect=[False,True]
            self.service.start();self.service.start()
            loop.start.assert_called_once()
            loop.get_task.return_value=None
            await self.service.close()
            loop.cancel.assert_called_once()
