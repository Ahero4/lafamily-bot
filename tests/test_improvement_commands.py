import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import DIRECT_REPORT_REQUEST_PREFIX, Draft, new_direct_report_request_id
from lafamily.modules.events.time import timestamp, week_range

G='111111111111111111';U='222222222222222222';V='333333333333333333';C='444444444444444444'


class ImprovementCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.bot=FamilyBot(Config('fake',int(V),int(G),Path(':memory:')),self.store)
        self.addAsyncCleanup(self.bot.close)

    def command(self,path):
        parts=path.split();command=self.bot.tree.get_command(parts[0],guild=discord.Object(id=int(G)))
        for name in parts[1:]:command=command.get_command(name)
        return command

    def interaction(self):
        return SimpleNamespace(id=123,guild_id=int(G),guild=SimpleNamespace(),user=SimpleNamespace(id=int(U),roles=[]),
            permissions=discord.Permissions.none(),response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())

    async def test_admin_and_organizer_commands_recheck_access(self):
        cases=[('баллы ведомости создать',(100,)),('баллы ведомости список',()),('баллы ведомости посмотреть',(1,)),
            ('баллы ведомости статус',(1,'paid')),('бот диагностика',()),('бот операции',()),
            ('бот восстановить-операцию',('report',1)),('мп запись создать',('Капт','2099-01-01 12:00')),
            ('мп запись закрыть',(1,)),('мп запись отчёт',(1,'1'))]
        for path,args in cases:
            with self.subTest(path=path),self.assertRaises(UserError):
                await self.command(path).callback(self.interaction(),*args)

    async def test_create_planned_event_enables_reminder_unless_disabled(self):
        self.bot.events.channel = AsyncMock()
        command = self.command('мп запись создать')
        for request_id, options, expected in ((123, {}, 'pending'), (124, {'reminder': False}, None)):
            interaction = self.interaction()
            interaction.id = request_id
            interaction.channel_id = int(C)
            interaction.permissions = discord.Permissions(administrator=True)
            interaction.channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(
                id=request_id + 1000, jump_url='https://discord.com/channels/1/2/3')))
            await command.callback(interaction, 'Капт', '2099-01-01 12:00', **options)
            event = self.store.planned_by_message(G, str(request_id + 1000))
            self.assertEqual(event['reminder_state'], expected)

    async def test_profile_hides_other_participants_subscriptions(self):
        self.store.set_price_subscription(G,V,'Медь',True)
        interaction=self.interaction()
        await self.command('участник профиль').callback(interaction,SimpleNamespace(id=int(V)))
        payload = interaction.edit_original_response.call_args.kwargs
        foreign = payload['embed']
        self.assertNotIn('Медь', str(foreign.to_dict()))
        self.assertEqual([field.name for field in foreign.fields], [
            'Баллы', 'Участие в отчётах за неделю', 'Участие в отчётах за всё время',
        ])
        self.assertEqual(foreign.fields[1].value, 'Участий — **0**\nПобед — **0**\nMVP — **0**')
        self.assertIn('включая сборы коровника', foreign.footer.text)
        self.assertIn('/мп создать', foreign.footer.text)
        self.assertEqual(payload['allowed_mentions'].to_dict(), {'parse': []})
        await self.command('участник профиль').callback(interaction)
        empty = interaction.edit_original_response.call_args.kwargs['embed']
        self.assertIn('Пока нет подписок', empty.fields[-1].value)
        from lafamily.modules.prices.parser import LIMITS
        for item in LIMITS:
            self.store.set_price_subscription(G,U,item,True)
        await self.command('участник профиль').callback(interaction)
        full = interaction.edit_original_response.call_args.kwargs['embed']
        subscriptions = full.fields[-1]
        self.assertFalse(subscriptions.inline)
        self.assertIn(str(len(LIMITS)), subscriptions.name)
        for item in LIMITS:
            self.assertIn(item, subscriptions.value)
        self.assertIn('**Охота**', subscriptions.value)
        self.assertIn('**Продукты**', subscriptions.value)
        self.assertLessEqual(len(subscriptions.value), 1024)
        self.assertLessEqual(len(full), 6000)

    async def test_weekly_profile_has_same_values_in_menu_and_slash(self):
        start, end = week_range()
        kind = self.store.set_type(G, U, 'Встреча', 5)
        self.store.create_report(Draft(G, U, C, kind['id'], kind['name'], 5, start, members=[U],
                                       request_id=new_direct_report_request_id(),
                                       outcome='win', win_bonus=2, mvp_id=U, mvp_bonus=1))
        self.store.create_report(Draft(G, U, C, kind['id'], kind['name'], 5, end, members=[U],
                                       request_id=new_direct_report_request_id()))
        slash = self.interaction()
        menu = self.interaction()
        with patch('lafamily.modules.insights.week_range', return_value=(start, end)), \
             patch('lafamily.modules.menu.week_range', return_value=(start, end)):
            await self.command('участник профиль').callback(slash)
            await self.bot.menu.show_profile(menu)
        profile = slash.edit_original_response.call_args.kwargs['embed']
        self.assertEqual(profile, menu.edit_original_response.call_args.kwargs['embed'])
        self.assertEqual(profile.fields[1].value, 'Участий — **1**\nПобед — **1**\nMVP — **1**')
        self.assertEqual(profile.fields[2].value, 'Участий — **2**\nПобед — **1**\nMVP — **1**')

    async def test_slash_report_source_differs_from_report_created_from_signup(self):
        self.store.configure(G, U, C, '10', '11')
        direct_kind = self.store.set_type(G, U, 'Капт', 10)
        planned_kind = self.store.set_type(G, U, 'Заезд', 20)
        week = week_range()
        before_start = datetime.fromisoformat(week[0]) - timedelta(minutes=1)
        planned = self.store.create_planned_event(G, U, 'signup', C, 'Заезд', week[0], 10, now=before_start)
        with patch('lafamily.planning_store.timestamp', return_value=timestamp(before_start)):
            self.store.signup_planned(G, planned['id'], U, 'going')

        channel = SimpleNamespace(permissions_for=lambda _: discord.Permissions(view_channel=True),
                                  get_partial_message=MagicMock(),
                                  send=AsyncMock(side_effect=[SimpleNamespace(id=700), SimpleNamespace(id=701)]))
        self.bot.events.channel = AsyncMock(return_value=channel)

        def organizer():
            request = self.interaction()
            request.channel_id = int(C)
            request.channel = channel
            request.guild.fetch_channel = AsyncMock(return_value=channel)
            request.guild.fetch_member = AsyncMock(return_value=SimpleNamespace(bot=False))
            request.permissions = discord.Permissions(administrator=True)
            request.edit_original_response.return_value = SimpleNamespace(id=600)
            return request

        direct = organizer()
        await self.command('мп создать')._invoke_with_namespace(direct, SimpleNamespace(вид=str(direct_kind['id'])))
        direct_view = direct.edit_original_response.call_args.kwargs['view']
        direct_view.draft.members = [U]
        self.assertTrue(direct_view.draft.request_id.startswith(DIRECT_REPORT_REQUEST_PREFIX))
        await self.bot.events.confirm(direct, direct_view)

        from_signup = organizer()
        await self.command('мп запись отчёт')._invoke_with_namespace(
            from_signup, SimpleNamespace(номер=planned['id'], вид=str(planned_kind['id'])),
        )
        planned_view = from_signup.edit_original_response.call_args.kwargs['view']
        self.assertFalse(planned_view.draft.request_id.startswith(DIRECT_REPORT_REQUEST_PREFIX))
        await self.bot.events.confirm(from_signup, planned_view)

        self.assertEqual(self.store.report(G, 1)['request_id'], direct_view.draft.request_id)
        self.assertEqual(self.store.report(G, 2)['request_id'], planned_view.draft.request_id)
        profile = self.store.participant_profile(G, U, week)
        self.assertEqual((profile['total'], profile['events'], profile['week_events']), (30, 1, 1))

    async def test_history_highlights_award_and_shows_only_relevant_details(self):
        kind = self.store.set_type(G, U, 'Встреча', 5)
        def add(name, **changes):
            draft = Draft(G, U, C, kind['id'], name, 5, '2026-11-20T10:30:00+00:00', members=[U], **changes)
            return self.store.create_report(draft)['id']
        standard = add('Встреча')
        win = add('Захват', outcome='win', win_bonus=3)
        cancelled = add('Турнир', mvp_id=U, mvp_bonus=2)
        self.store.cancel_report(G, V, cancelled, '*повтор*\nотчёта')
        interaction = self.interaction()
        await self.command('баллы история').callback(interaction)
        header, removed, bonus, normal = interaction.edit_original_response.call_args.kwargs['content'].split('\n\n')
        self.assertIn('страница 1', header)
        self.assertEqual(normal.splitlines(), ['**+5** · Встреча', f'-# Отчёт #{standard} · 20.11.2026 13:30 МСК'])
        self.assertIn(f'#{win}', bonus)
        self.assertIn('**+8** · Захват', bonus)
        self.assertIn('База 5 · победа +3', bonus)
        self.assertNotIn('MVP', bonus)
        self.assertIn('**0 · отменён**', removed)
        self.assertIn('До отмены: база 5 · MVP +2', removed)
        self.assertIn(r'\*повтор\* отчёта', removed)
        self.assertIn(f'<@{V}>', removed)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        await self.command('баллы история').callback(interaction, 2)
        self.assertIn('страница 2\n\nЗаписей нет.', interaction.edit_original_response.call_args.kwargs['content'])

    async def test_history_handles_maximum_names_and_cancellation_reasons(self):
        kind=self.store.set_type(G,U,'*'*80,10)
        for index in range(5):
            saved=self.store.create_report(Draft(G,U,C,kind['id'],kind['name'],10,timestamp(),members=[U],request_id=str(index)))
            self.store.cancel_report(G,U,saved['id'],'*'*300)
        interaction=self.interaction()
        await self.command('баллы история').callback(interaction)
        payload=interaction.edit_original_response.call_args.kwargs
        self.assertLessEqual(len(payload['content']),2000)
        self.assertEqual(payload['content'].count('**0 · отменён**'), 5)
        self.assertEqual(payload['content'].count('До отмены: 10'), 5)
        self.assertEqual(payload['content'].count('Причина:'), 5)
        self.assertEqual(payload['allowed_mentions'].to_dict(),{'parse':[]})

    async def test_private_check_is_user_initiated_rate_limited_and_preserves_subscriptions(self):
        interaction=self.interaction();interaction.user.send=AsyncMock()
        self.store.set_price_subscription(G,U,'Медь',True)
        await self.command('скупщик проверить-лс').callback(interaction)
        interaction.user.send.assert_awaited_once()
        with self.assertRaises(UserError):await self.command('скупщик проверить-лс').callback(interaction)
        self.assertEqual(self.store.price_subscriptions(G,U),['Медь'])

    async def test_price_history_requires_viewing_the_price_channel(self):
        self.store.configure_prices(G, U, C)
        interaction = self.interaction()
        channel = SimpleNamespace(permissions_for=lambda member: SimpleNamespace(view_channel=False))
        interaction.guild.fetch_channel = AsyncMock(return_value=channel)
        with patch.object(self.store, 'price_history_data', return_value=[]) as history:
            with self.assertRaises(UserError):
                await self.command('скупщик история').callback(interaction, 'Медь')
            history.assert_not_called()
            channel.permissions_for = lambda member: SimpleNamespace(view_channel=True)
            await self.command('скупщик история').callback(interaction, 'Медь')
            history.assert_called_once()

    def planned_report(self):
        self.store.configure(G, U, C, 'organizer', 'manager')
        kind = self.store.set_type(G, U, 'Капт', 10)
        event = self.store.create_planned_event(G, U, 'plan', C, 'Капт',
            timestamp(datetime.now(UTC) + timedelta(hours=1)), 10)
        self.store.db.execute('UPDATE planned_events SET starts_at=? WHERE id=?',
            (timestamp(datetime.now(UTC) - timedelta(minutes=1)), event['id']))
        interaction = self.interaction()
        interaction.permissions = discord.Permissions(administrator=True)
        interaction.channel_id = int(C)
        message = SimpleNamespace(edit=AsyncMock())
        interaction.channel = SimpleNamespace(get_partial_message=MagicMock(return_value=message))
        interaction.edit_original_response.return_value = SimpleNamespace(id=777)
        source = SimpleNamespace(permissions_for=lambda member: SimpleNamespace(view_channel=True))
        interaction.guild.fetch_channel = AsyncMock(return_value=source)
        return interaction, event, kind, message

    async def test_planned_report_cannot_be_created_in_price_channel(self):
        interaction, event, kind, _ = self.planned_report()
        self.store.configure_prices(G, U, '555555555555555555')
        interaction.channel_id = 555555555555555555
        with self.assertRaises(UserError):
            await self.command('мп запись отчёт').callback(interaction, event['id'], str(kind['id']))
        self.assertEqual(self.bot.events.views, {})

    async def test_planned_report_expiry_uses_message_after_token_expires(self):
        interaction, event, kind, message = self.planned_report()
        await self.command('мп запись отчёт').callback(interaction, event['id'], str(kind['id']))
        view = self.bot.events.views[(G, U)]
        interaction.edit_original_response.reset_mock()
        interaction.edit_original_response.side_effect = RuntimeError('token expired')
        await view.on_timeout()
        message.edit.assert_awaited_once()
        interaction.edit_original_response.assert_not_awaited()


if __name__=='__main__':unittest.main()
