import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.announcements import AnnouncementService, AnnouncementCommands, due_occurrences, parse_days, parse_clock, parse_start
from lafamily.modules.events.time import timestamp
from lafamily.modules.planning import SignupView

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
        self.assertEqual(parse_start('2026-11-02 20:00',START-timedelta(minutes=14)), timestamp(START))
        self.assertEqual(parse_start('2026-11-02 20:00',START+timedelta(days=1)), timestamp(START))

    def test_time_without_date_uses_today_in_moscow(self):
        self.assertEqual(parse_start('20:00', START - timedelta(minutes=1)), timestamp(START))
        midnight = datetime(2026, 11, 1, 21, 0, tzinfo=UTC)
        self.assertEqual(parse_start('00:05', midnight), timestamp(midnight + timedelta(minutes=5)))
        for invalid in ('24:00', '12:60', '2026-02-30 12:00'):
            with self.subTest(value=invalid), self.assertRaises(UserError):
                parse_start(invalid, START)


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

    async def test_once_command_sends_nearby_and_past_start_immediately(self):
        self.store.disable_announcement_schedule(G, self.schedule)
        commands = AnnouncementCommands(self.service)
        for now, has_signup in ((START - timedelta(minutes=5), True), (START + timedelta(minutes=1), False)):
            self.channel.send.reset_mock()
            with patch('lafamily.modules.announcements.datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                await commands.once.callback(commands, self.notice_interaction(), str(self.template), '2026-11-02 20:00')
                await self.service.tick(now)
            self.channel.send.assert_awaited_once()
            payload = self.channel.send.call_args.kwargs
            self.assertEqual(payload['view'] is not None, has_signup)
            self.assertEqual(payload['content'], '<@&123>')

    async def test_signup_recovers_when_announcement_message_binding_fails(self):
        with patch.object(self.store, 'bind_planned_message', side_effect=RuntimeError('database busy')):
            with self.assertLogs('lafamily.errors', level='ERROR'):
                await self.service.tick(START - timedelta(minutes=15))
        payload = self.channel.send.call_args.kwargs
        bot_user = SimpleNamespace(id=999)
        message = SimpleNamespace(id=789, author=bot_user, embeds=[payload['embed']], edit=AsyncMock())
        interaction = SimpleNamespace(guild=self.guild, guild_id=int(G), channel=self.channel, channel_id=456,
            user=SimpleNamespace(id=222222222222222222, bot=False), message=message,
            client=SimpleNamespace(user=bot_user), response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock())
        view = SignupView(self.store)
        self.addCleanup(view.stop)
        with patch('lafamily.planning_store.timestamp', return_value=timestamp(START - timedelta(minutes=10))):
            await view.choose(interaction, 'going')
        event = self.store.planned_by_message(G, '789')
        self.assertEqual(event['going'], ['222222222222222222'])
        await self.service.tick(START - timedelta(minutes=9))
        self.channel.send.assert_awaited_once()

    def planned(self, request_id, *, reminder=True, publish=True):
        event = self.store.create_planned_event(G, '1', request_id, '456', 'Тренировка', timestamp(START), 10,
                                                now=START - timedelta(hours=1), reminder=reminder)
        if publish:
            self.store.bind_planned_message(G, event['id'], '987')
        return event['id']

    async def test_planned_reminder_is_sent_once_with_link_and_no_mentions(self):
        self.store.disable_announcement_schedule(G, self.schedule)
        event_id = self.planned('manual')
        await self.service.tick(START - timedelta(minutes=16))
        self.channel.send.assert_not_awaited()

        second_service = AnnouncementService(self.client, self.store, int(G))
        now = START - timedelta(minutes=15)
        await asyncio.gather(self.service.tick(now), second_service.tick(now))
        await second_service.tick(START - timedelta(minutes=2))
        self.channel.send.assert_awaited_once()
        payload = self.channel.send.call_args.kwargs
        self.assertEqual(payload['embed'].title, 'Скоро МП: Тренировка')
        self.assertIn(f'https://discord.com/channels/{G}/456/987', payload['embed'].description)
        self.assertEqual(payload['allowed_mentions'].to_dict(), {'parse': []})
        self.assertEqual(self.store.planned_event(G, event_id)['reminder_state'], 'sent')

    async def test_planned_reminder_skips_disabled_closed_unpublished_and_started_events(self):
        self.store.disable_announcement_schedule(G, self.schedule)
        self.planned('disabled', reminder=False)
        self.planned('unpublished', publish=False)
        closed = self.planned('closed')
        self.store.close_planned(G, '1', closed)
        await self.service.tick(START - timedelta(minutes=10))
        self.planned('started')
        await self.service.tick(START)
        self.channel.send.assert_not_awaited()

    async def test_planned_reminder_does_not_repeat_an_uncertain_send(self):
        self.store.disable_announcement_schedule(G, self.schedule)
        event_id = self.planned('manual')
        self.channel.send.side_effect = OSError('unknown result')
        with self.assertLogs('lafamily.errors', level='ERROR'):
            await self.service.tick(START - timedelta(minutes=5))
        await AnnouncementService(self.client, self.store, int(G)).tick(START - timedelta(minutes=4))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.planned_event(G, event_id)['reminder_state'], 'uncertain')

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
        embed = self.channel.send.call_args.kwargs['embed']
        self.assertIn('Картинку', next(field.value for field in embed.fields if field.name == 'Картинка'))
        self.assertTrue(embed.footer.text.startswith('lafamily:planned:'))
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

    def notice_interaction(self, request_id=1000):
        return SimpleNamespace(id=request_id, guild_id=int(G), guild=self.guild, channel=self.channel,
            channel_id=self.channel.id, user=SimpleNamespace(id=42), permissions=discord.Permissions(administrator=True),
            response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())

    async def test_notice_card_saves_only_after_description_and_confirmation_then_edits_same_id(self):
        commands = AnnouncementCommands(self.service)
        interaction = self.notice_interaction()
        await commands.notice.callback(commands, interaction, 'Капт', '20:00', self.role)
        card = interaction.edit_original_response.call_args.kwargs['view']
        self.addCleanup(card.stop)
        self.assertEqual(self.store.recurring_notices(G), [])
        with self.assertRaises(UserError):
            await card.confirm.callback(interaction)
        await card.description.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        modal.body._value = 'Сбор в голосовом канале'
        await modal.on_submit(interaction)
        self.assertEqual(self.store.recurring_notices(G), [])
        await card.confirm.callback(interaction)
        saved, = self.store.recurring_notices(G)
        self.assertEqual(json.loads(saved['days']), list(range(7)))
        self.assertEqual(saved['body'], 'Сбор в голосовом канале')
        self.assertEqual(saved['name'], 'Капт')

        new_role = MagicMock(spec=discord.Role)
        new_role.id, new_role.guild, new_role.mentionable = 321, self.guild, True
        new_role.is_default.return_value = False
        self.guild.get_role = {self.role.id: self.role, new_role.id: new_role}.get
        edit = self.notice_interaction(1001)
        choices = await commands.notice_choices(edit, 'кап')
        self.assertEqual([choice.value for choice in choices], [str(saved['id'])])
        self.assertIn('Капт', choices[0].name)
        await commands.edit_notice.callback(commands, edit, choices[0].value, '21:00', role=new_role)
        editor = edit.edit_original_response.call_args.kwargs['view']
        self.addCleanup(editor.stop)
        self.assertEqual(len(editor.select_days.options), 7)
        editor.select_days._values = ['2', '0']
        await editor.select_days.callback(edit)
        self.assertEqual(json.loads(self.store.recurring_notice(G, saved['id'])['days']), list(range(7)))
        await editor.description.callback(edit)
        modal = edit.response.send_modal.call_args.args[0]
        modal.body._value = 'Сбор на тренировку'
        await modal.on_submit(edit)
        await editor.confirm.callback(edit)
        changed, = self.store.recurring_notices(G)
        self.assertEqual((changed['id'], changed['clock'], changed['body']),
                         (saved['id'], '21:00', 'Сбор на тренировку'))
        self.assertEqual(json.loads(changed['days']), [0, 2])
        self.assertEqual(changed['role_id'], '321')

        disable = self.notice_interaction(1002)
        await commands.edit_notice.callback(commands, disable, str(saved['id']), enabled=False)
        card = disable.edit_original_response.call_args.kwargs['view']
        self.addCleanup(card.stop)
        self.assertEqual([option.value for option in card.select_days.options if option.default], ['0', '2'])
        card.select_days._values = []
        await card.select_days.callback(disable)
        await card.confirm.callback(disable)
        self.assertEqual(self.store.recurring_notices(G, enabled_only=True), [])
        self.assertEqual(json.loads(self.store.recurring_notice(G, saved['id'])['days']), list(range(7)))

    async def test_recurring_notice_sends_at_chosen_time_on_selected_days_once_per_day(self):
        self.store.disable_announcement_schedule(G, self.schedule)
        self.role.mention = '<@&123>'
        self.store.save_recurring_notice(G, 'notice', {'channel_id': '456', 'role_id': '123',
            'clock': '20:00', 'days': '[0, 2]', 'body': 'Тренировка @everyone <@555>', 'enabled': True})
        await self.service.tick(START - timedelta(minutes=1))
        await self.service.tick(START + timedelta(days=1))
        await self.service.tick(START + timedelta(minutes=5))
        self.channel.send.assert_not_awaited()

        await self.service.tick(START)
        await AnnouncementService(self.client, self.store, int(G)).tick(START + timedelta(minutes=1))
        self.channel.send.assert_awaited_once()
        payload = self.channel.send.call_args.kwargs
        self.assertEqual(payload['content'], '<@&123>')
        self.assertEqual(payload['allowed_mentions'].to_dict(), {'roles': [123], 'parse': []})
        self.assertIn('Тренировка', payload['embed'].description)
        await self.service.tick(START + timedelta(days=2))
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_notice_edit_is_rejected_after_another_card_was_saved(self):
        draft = {'channel_id': '456', 'role_id': '123', 'clock': '20:00', 'days': '[0]',
                 'body': 'Сбор', 'enabled': True}
        number = self.store.save_recurring_notice(G, 'notice', draft)
        older = self.store.recurring_notice(G, number)
        self.store.save_recurring_notice(G, 'first-edit', {**older, 'body': 'Первое изменение'})
        with self.assertRaises(UserError):
            self.store.save_recurring_notice(G, 'stale-edit', {**older, 'body': 'Устаревшая карточка'})
        self.assertEqual(self.store.recurring_notice(G, number)['body'], 'Первое изменение')

    def test_old_recurring_notice_keeps_schedule_when_name_column_is_added(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'notices.sqlite'
            store = Store(path)
            try:
                number = store.save_recurring_notice(G, 'legacy', {'channel_id': '456', 'role_id': '123',
                    'clock': '20:00', 'days': '[0, 2]', 'body': 'Сбор', 'enabled': True})
                store.db.execute('ALTER TABLE recurring_notices DROP COLUMN name')
            finally:
                store.close()
            store = Store(path)
            try:
                notice = store.recurring_notice(G, number)
                self.assertIsNone(notice['name'])
                self.assertEqual((notice['clock'], notice['days'], notice['body']), ('20:00', '[0, 2]', 'Сбор'))
            finally:
                store.close()

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
