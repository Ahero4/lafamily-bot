import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.time import timestamp
from lafamily.modules.database_backups import RestoreView
from lafamily.modules.menu import LAUNCHER_TEXT, MenuLauncher, OPEN_ID


class FamilyMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        self.bot = FamilyBot(Config('fake', 1, 2, Path(':memory:')), store)
        self.addAsyncCleanup(self.bot.close)
        self.request = SimpleNamespace(guild_id=2, guild=SimpleNamespace(id=2), user=SimpleNamespace(id=3),
            id=123, channel_id=12, type=discord.InteractionType.component,
            permissions=discord.Permissions.none(),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(),
                                     send_modal=AsyncMock(), is_done=Mock(return_value=False)),
            edit_original_response=AsyncMock())

    async def click(self, view, label):
        button = next(item for item in view.children if getattr(item, 'label', None) == label)
        await button.callback(self.request)

    def section(self):
        return self.request.response.edit_message.await_args.kwargs['view']

    async def open_home(self):
        await self.bot.menu.open(self.request)
        return self.request.response.send_message.await_args.kwargs['view']

    def assert_card(self, payload, name, *, opening=False):
        files = payload['files' if opening else 'attachments']
        filename = f'{name}.jpg'
        self.assertEqual([file.filename for file in files], [filename])
        self.assertEqual(payload['embed'].image.url, f'attachment://{filename}')

    def add_points(self):
        user_id = '300000000000000000'
        self.request.user.id = int(user_id)
        kind = self.bot.store.set_type('2', user_id, 'Заезд', 20)
        return self.bot.store.create_report(Draft('2', user_id, '12', kind['id'], kind['name'], 20,
                                                  timestamp(), members=[user_id]))

    async def test_public_button_opens_private_view_and_real_personal_garden(self):
        launcher = MenuLauncher(self.bot.menu)
        self.assertTrue(launcher.is_persistent())
        self.assertEqual(launcher.children[0].custom_id, OPEN_ID)
        await launcher.open.callback(self.request)
        payload = self.request.response.send_message.await_args.kwargs
        self.assertTrue(payload['ephemeral'])
        self.assert_card(payload, 'home', opening=True)
        view = payload['view']
        self.assertEqual([(b.label, b.row) for b in view.children], [
            ('Профиль', 0), ('Мероприятия', 0), ('Сад', 0),
            ('Скупщик', 1), ('Коровник', 1), ('Помощь', 1),
        ])
        garden = next(b for b in view.children if b.label == 'Сад')
        await garden.callback(self.request)
        self.request.response.defer.assert_awaited_once_with()
        card = self.request.edit_original_response.await_args.kwargs
        self.assertEqual(card['view'].owner, 3)
        self.assertTrue(card['embed'].fields)
        self.assert_card(card, 'garden')
        await self.click(card['view'], 'Обновить')
        refreshed = self.request.edit_original_response.await_args.kwargs
        self.assert_card(refreshed, 'garden')
        await self.click(refreshed['view'], 'Назад')
        returned = self.request.response.edit_message.await_args.kwargs
        self.assertEqual(returned['content'], '**LaFamily**')
        self.assert_card(returned, 'home')
        self.request.user.id = 4
        with self.assertRaises(UserError):
            await view.interaction_check(self.request)

    async def test_card_navigation_uploads_only_the_current_image(self):
        command = self.bot.tree.get_command('меню', guild=discord.Object(id=2))
        await command.callback(self.request)
        payload = self.request.response.send_message.await_args.kwargs
        self.assertTrue(payload['ephemeral'])
        self.assert_card(payload, 'home', opening=True)
        home = payload['view']

        for label, name in [('Мероприятия', 'events'), ('Скупщик', 'prices'), ('Коровник', 'barn')]:
            with self.subTest(section=label):
                await self.click(home, label)
                section = self.request.response.edit_message.await_args.kwargs
                self.assert_card(section, name)
                await self.click(section['view'], 'Назад')
                returned = self.request.response.edit_message.await_args.kwargs
                self.assert_card(returned, 'home')
                home = returned['view']

        await self.click(home, 'Профиль')
        profile = self.request.edit_original_response.await_args.kwargs
        self.assertEqual(profile['attachments'], [])
        self.assertIsNone(profile['embed'].image.url)

    async def test_missing_picture_keeps_menu_usable(self):
        with TemporaryDirectory() as folder, patch('lafamily.modules.menu_cards.CARD_DIR', Path(folder)), \
                patch('lafamily.modules.menu_cards.log_error') as log_error:
            home = await self.open_home()
            self.assertEqual(self.request.response.send_message.await_args.kwargs['files'], [])
            await self.click(home, 'Скупщик')
            section = self.request.response.edit_message.await_args.kwargs
            self.assertEqual(section['attachments'], [])
            self.assertEqual(section['content'], '**Скупщик**')
            self.assertIn('Подписки', [getattr(item, 'label', None) for item in section['view'].children])
            self.assertEqual(log_error.call_count, 2)

    async def test_private_admin_navigation_rechecks_rights_and_restore(self):
        self.request.permissions = discord.Permissions(administrator=True)
        cmd = self.bot.tree.get_command('меню', guild=discord.Object(id=2))
        await cmd.callback(self.request)
        home = self.request.response.send_message.await_args.kwargs['view']
        await next(b for b in home.children if b.label == 'Администрирование').callback(self.request)
        payload = self.request.response.edit_message.await_args.kwargs
        self.assertEqual(payload['attachments'], [])
        admin = payload['view']
        self.request.permissions = discord.Permissions.none()
        with self.assertRaises(UserError):
            await admin.interaction_check(self.request)
        self.bot.backups.restoring = True
        with self.assertRaises(UserError):
            await self.bot.menu.open(self.request)

    async def test_guide_target_picker_requires_current_admin_and_owner(self):
        self.request.permissions = discord.Permissions(administrator=True)
        await self.bot.menu.open(self.request)
        home = self.request.response.send_message.await_args.kwargs['view']
        await next(i for i in home.children if i.label == 'Администрирование').callback(self.request)
        admin = self.request.response.edit_message.await_args.kwargs['view']
        await self.click(admin, 'Каналы и публикации')
        await self.click(self.section(), 'Разместить памятку семьи')
        picker = self.section()
        self.assertEqual(picker.children[0].channel_types, [discord.ChannelType.text])
        self.request.user.id = 5
        with self.assertRaises(UserError):
            await picker.interaction_check(self.request)
        self.request.user.id = 3
        self.request.permissions = discord.Permissions.none()
        with self.assertRaises(UserError):
            await picker.interaction_check(self.request)
        with self.assertRaises(UserError):
            await self.bot.menu.publish_guide(self.request, 12)

    async def test_help_has_topic_buttons_guide_catalog_and_back(self):
        writes = self.bot.store.db.total_changes
        await self.click(await self.open_home(), 'Помощь')
        help_screen = self.request.edit_original_response.await_args.kwargs
        self.assertIn('Памятка', help_screen['embed'].title)
        self.assert_card(help_screen, 'help')
        self.assertEqual([(button.label, button.row) for button in help_screen['view'].children], [
            ('Баллы', 0), ('Мероприятия', 0), ('Сад', 0),
            ('Скупщик', 1), ('Коровник', 1), ('Вступление и роли', 1),
            ('Каталог команд', 2), ('Назад', 2),
        ])
        await self.click(help_screen['view'], 'Сад')
        garden = self.request.edit_original_response.await_args.kwargs
        self.assertIn('PayDay', garden['embed'].description)
        self.assertEqual(garden['attachments'], [])
        self.assertIsNone(garden['embed'].image.url)
        self.assertEqual([field.name for field in garden['embed'].fields], ['/сад'])
        self.assertEqual(garden['allowed_mentions'].to_dict(), {'parse': []})
        self.request.user.id = 4
        with self.assertRaises(UserError):
            await self.click(garden['view'], 'Назад')
        self.request.user.id = 3
        await self.click(garden['view'], 'Назад')
        self.assertEqual(self.request.edit_original_response.await_args.kwargs['embed'].title, help_screen['embed'].title)
        await self.click(help_screen['view'], 'Каталог команд')
        self.assertIn('список команд', self.request.edit_original_response.await_args.kwargs['embed'].title)
        self.assertEqual(self.bot.store.db.total_changes, writes)

    async def test_help_topics_explain_rules_for_points_events_and_price_alerts(self):
        await self.click(await self.open_home(), 'Помощь')
        help_view = self.request.edit_original_response.await_args.kwargs['view']
        checks = {
            'Баллы': ('подтверждённого отчёта МП', 'ставке вида', 'MVP получает отдельный бонус',
                       'фиксированная награда', 'по дате события'),
            'Мероприятия': ('«Участвую»', 'Если мест в основном составе нет', 'в резерв', 'нажмите «Возможно»'),
            'Скупщик': ('по обычной цене', '95–100% от справочного максимума включительно',
                        'при достижении справочного максимума и выше'),
        }
        for topic, expected in checks.items():
            with self.subTest(topic=topic):
                await self.click(help_view, topic)
                embed = self.request.edit_original_response.await_args.kwargs['embed']
                for detail in expected:
                    self.assertIn(detail, embed.description)
                self.assertTrue(all('Доступ — ' in field.value for field in embed.fields))

    async def test_help_filters_commands_by_registered_role_and_hidden_metadata(self):
        event_group = self.bot.tree.get_command('мп', guild=discord.Object(id=2))

        @event_group.command(name='скрытая', description='Скрытая команда', extras={'hidden': True})
        async def hidden(interaction: discord.Interaction):
            raise AssertionError('Справка не вызывает команды')

        self.bot.store.configure('2', '3', '12', '40', '41')
        await self.click(await self.open_home(), 'Помощь')
        help_view = self.request.edit_original_response.await_args.kwargs['view']
        await self.click(help_view, 'Мероприятия')
        member = self.request.edit_original_response.await_args.kwargs['embed']
        member_paths = {field.name for field in member.fields}
        self.assertEqual(member_paths, {'/мп посмотреть', '/мп виды список', '/мп запись состав'})

        self.request.user.roles = [SimpleNamespace(id=40)]
        await self.click(help_view, 'Мероприятия')
        organizer = self.request.edit_original_response.await_args.kwargs['embed']
        organizer_paths = {field.name for field in organizer.fields}
        self.assertTrue(member_paths | {'/мп создать', '/мп запись отчёт'} <= organizer_paths)
        self.assertNotIn('/мп отменить', organizer_paths)
        self.assertNotIn('/мп редактировать', organizer_paths)
        self.assertNotIn('/мп скрытая', organizer_paths)

    async def test_service_help_rechecks_role_and_protects_admin_topics(self):
        self.bot.store.configure('2', '3', '12', '40', '41')
        self.request.user.roles = [SimpleNamespace(id=41)]
        await self.click(await self.open_home(), 'Помощь')
        help_view = self.request.edit_original_response.await_args.kwargs['view']
        await self.click(help_view, 'Служебная помощь')
        service = self.request.edit_original_response.await_args.kwargs['view']
        self.assertEqual([button.label for button in service.children], ['Каналы и настройки', 'Назад'])
        await self.click(service, 'Каналы и настройки')
        manager = self.request.edit_original_response.await_args.kwargs['embed']
        self.assertEqual({field.name for field in manager.fields}, {
            '/скупщик настройки', '/скупщик обработать', '/заявки настройки',
        })

        self.request.permissions = discord.Permissions(administrator=True)
        await self.click(help_view, 'Служебная помощь')
        admin_service = self.request.edit_original_response.await_args.kwargs['view']
        self.assertIn('Анонсы и оповещения', [button.label for button in admin_service.children])
        await self.click(admin_service, 'Резервные копии')
        backup = self.request.edit_original_response.await_args.kwargs
        self.assertEqual(len(backup['embed'].fields), 3)
        self.assertTrue(all('только администратор Discord' in field.value for field in backup['embed'].fields))
        await self.click(backup['view'], 'Назад')
        self.assertEqual(self.request.edit_original_response.await_args.kwargs['content'], '**Служебная помощь**')

        self.request.permissions = discord.Permissions.none()
        with self.assertRaises(UserError):
            await self.click(admin_service, 'Резервные копии')
        self.request.user.roles = []
        with self.assertRaises(UserError):
            await self.click(help_view, 'Служебная помощь')

        self.request.response.defer.reset_mock()
        await self.click(backup['view'], 'Назад')
        self.request.response.defer.assert_awaited_once_with()
        fallback = self.request.edit_original_response.await_args.kwargs
        self.assertIn('Памятка', fallback['embed'].title)
        self.assertNotIn('Служебная помощь', [button.label for button in fallback['view'].children])

    async def test_help_topic_pagination_covers_admin_and_current_access(self):
        self.request.permissions = discord.Permissions(administrator=True)
        await self.click(await self.open_home(), 'Помощь')
        help_view = self.request.edit_original_response.await_args.kwargs['view']
        await self.click(help_view, 'Мероприятия')
        page_one = self.request.edit_original_response.await_args.kwargs
        await self.click(page_one['view'], 'Следующая страница')
        page_two = self.request.edit_original_response.await_args.kwargs
        paths = {field.name for page in (page_one, page_two) for field in page['embed'].fields}
        self.assertEqual(page_two['embed'].footer.text, 'Страница 2/2')
        self.assertTrue({'/мп редактировать', '/мп отменить', '/мп запись отчёт'} <= paths)
        self.request.permissions = discord.Permissions.none()
        await self.click(page_one['view'], 'Следующая страница')
        without_admin = self.request.edit_original_response.await_args.kwargs['embed']
        self.assertNotIn('/мп редактировать', [field.name for field in without_admin.fields])
        self.assertLessEqual(len(without_admin), 6000)

    async def test_profile_history_opens_saved_report_and_returns_to_history(self):
        report = self.add_points()
        await self.click(await self.open_home(), 'Профиль')
        profile = self.request.edit_original_response.await_args.kwargs
        self.assertIn('**20** за текущую неделю', profile['embed'].fields[0].value)
        self.assertIn('**20** за всё время', profile['embed'].fields[0].value)
        await self.click(profile['view'], 'История баллов')
        history = self.request.edit_original_response.await_args.kwargs
        self.assertIn('Заезд', history['content'])
        select = next(item for item in history['view'].children if isinstance(item, discord.ui.Select))
        select._values = [str(report['id'])]
        await select.callback(self.request)
        opened = self.request.response.edit_message.await_args.kwargs
        self.assertIn(f"МП #{report['id']}", opened['embed'].title)
        await self.click(opened['view'], 'Назад')
        self.assertIn('История баллов', self.request.edit_original_response.await_args.kwargs['content'])

    async def test_barn_submits_public_report_with_optional_member(self):
        self.bot.store.configure_barn('2', '3', 4)
        self.request.user.id = 300000000000000000
        receiver = SimpleNamespace(id=400000000000000000, bot=False)
        self.request.guild.fetch_member = AsyncMock(return_value=receiver)
        await self.click(await self.open_home(), 'Коровник')
        self.request.response.send_modal.assert_not_awaited()
        await self.click(self.section(), 'Записать сбор')
        modal = self.request.response.send_modal.await_args.args[0]
        modal.children[0].component._value = '+157к'
        user_select = modal.children[1].component
        self.assertIsInstance(user_select, discord.ui.UserSelect)
        self.assertFalse(user_select.required)
        user_select._values = [receiver]
        self.request.edit_original_response.return_value = SimpleNamespace(id=600000000000000000)
        await modal.on_submit(self.request)
        self.request.response.defer.assert_awaited_once_with(thinking=True)
        self.request.guild.fetch_member.assert_awaited_once_with(receiver.id)
        report = self.bot.store.report('2', 1)
        self.assertEqual((report['barn_amount'], report['points'], report['members']), (157000, 4, [str(receiver.id)]))
        self.assertIn('Коровник', self.request.edit_original_response.await_args.kwargs['embed'].title)

    async def test_payout_modal_uses_registered_group_binding_and_saved_sheet_flow(self):
        self.add_points()
        self.request.permissions = discord.Permissions(administrator=True)
        await self.click(await self.open_home(), 'Профиль')
        profile = self.request.edit_original_response.await_args.kwargs['view']
        await self.click(profile, 'Премии и ведомости')
        payouts = self.section()

        for label in ('Рассчитать премии', 'Создать ведомость'):
            await self.click(payouts, label)
            modal = self.request.response.send_modal.await_args.args[0]
            modal.children[0].component._value = '10000'
            modal.children[1].component._values = ['0']
            await modal.on_submit(self.request)
            result = self.request.edit_original_response.await_args.kwargs
            if label == 'Рассчитать премии':
                self.assertIn('расчёт премий', result['embed'].title)
            else:
                self.assertIn('Ведомость #1', result['content'])
        sheet = self.bot.store.payout_sheet('2', 1)
        self.assertEqual((sheet['state'], sheet['bank']), ('prepared', 10000))

        await self.click(payouts, 'Статус ведомости')
        modal = self.request.response.send_modal.await_args.args[0]
        modal.children[0].component._value = '1'
        modal.children[1].component._values = ['approved']
        self.request.permissions = discord.Permissions.none()
        with self.assertRaises(UserError):
            await modal.on_submit(self.request)
        self.request.permissions = discord.Permissions(administrator=True)
        await modal.on_submit(self.request)
        confirm = self.request.edit_original_response.await_args.kwargs['view']
        await confirm.confirm.callback(self.request)
        self.assertEqual(self.bot.store.payout_sheet('2', 1)['state'], 'approved')

    async def test_nested_manager_command_acks_before_database_access_check(self):
        def settings(guild_id):
            self.assertEqual(guild_id, '2')
            self.request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            return None

        self.request.user.roles = []
        with patch.object(self.bot.store, 'settings', side_effect=settings), self.assertRaisesRegex(UserError, 'роль'):
            await self.bot.menu.invoke(self.request, 'мп запись закрыть', number=1)
        self.request.edit_original_response.assert_not_awaited()

    async def test_event_voice_form_acks_before_role_and_channel_lookup(self):
        self.add_points()
        self.bot.store.configure('2', '3', '12', '40', '41')
        self.request.user.roles = [SimpleNamespace(id=40)]
        await self.click(await self.open_home(), 'Мероприятия')
        await self.click(self.section(), 'Создать отчёт')
        picker = self.section().children[0]
        picker._values = [picker.options[0].value]
        await picker.callback(self.request)
        modal = self.request.response.send_modal.await_args.args[0]

        submit = SimpleNamespace(**vars(self.request))
        submit.type = discord.InteractionType.modal_submit
        submit.response = SimpleNamespace(defer=AsyncMock())
        submit.edit_original_response = AsyncMock(return_value=SimpleNamespace(id=55))
        submit.channel = SimpleNamespace(get_partial_message=Mock())
        selected = Mock(spec=discord.app_commands.AppCommandChannel)
        selected.id = 95
        modal.fields['voice'].component._values = [selected]
        voice = Mock(spec=discord.VoiceChannel)
        voice.id = 95
        voice.guild = submit.guild
        voice.permissions_for.return_value = discord.Permissions(view_channel=True)
        voice.members = [SimpleNamespace(id=500000000000000000, bot=False),
                         SimpleNamespace(id=600000000000000000, bot=True)]

        async def fetch_voice(channel_id):
            self.assertEqual(channel_id, selected.id)
            submit.response.defer.assert_awaited_once_with(thinking=True)
            return voice

        submit.guild.get_channel = Mock(return_value=None)
        submit.guild.fetch_channel = AsyncMock(side_effect=fetch_voice)
        original_settings = self.bot.store.settings

        def settings(guild_id):
            submit.response.defer.assert_awaited_once_with(thinking=True)
            return original_settings(guild_id)

        with patch.object(self.bot.store, 'settings', side_effect=settings):
            await modal.on_submit(submit)
        submit.guild.fetch_channel.assert_awaited_once_with(selected.id)
        view = submit.edit_original_response.await_args.kwargs['view']
        self.assertEqual((view.draft.voice_channel_id, view.draft.members), ('95', ['500000000000000000']))

    async def test_report_recovery_has_a_separate_private_response(self):
        self.request.permissions = discord.Permissions(administrator=True)
        self.bot.health.recover_report = AsyncMock(return_value='Карточка обновлена.')
        await self.click(await self.open_home(), 'Администрирование')
        await self.click(self.section(), 'Диагностика')
        await self.click(self.section(), 'Восстановить карточку отчёта')
        modal = self.request.response.send_modal.await_args.args[0]
        modal.children[0].component._value = '44'
        await modal.on_submit(self.request)
        self.request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.bot.health.recover_report.assert_awaited_once_with(self.request.guild, 44)
        self.assertEqual(self.request.edit_original_response.await_args.kwargs['content'], 'Карточка обновлена.')

    async def test_backup_restore_menu_opens_existing_confirmation(self):
        self.request.permissions = discord.Permissions(administrator=True)
        prepared = SimpleNamespace(manifest={'created_at': '2026-01-01', 'version': '0.6.20'},
                                   summary=dict(reports=1, active=1, cancelled=0, participants=1))
        self.bot.backups.prepare = AsyncMock(return_value=prepared)
        await self.click(await self.open_home(), 'Администрирование')
        await self.click(self.section(), 'Резервные копии')
        await self.click(self.section(), 'Восстановить из бэкапа')
        modal = self.request.response.send_modal.await_args.args[0]
        modal.children[0].component._value = 'not a snowflake'
        with self.assertRaises(UserError):
            await modal.on_submit(self.request)
        self.bot.backups.prepare.assert_not_awaited()
        modal.children[0].component._value = '123000000000000000'
        self.bot.backups.restoring = True
        with self.assertRaises(UserError):
            await modal.on_submit(self.request)
        self.bot.backups.restoring = False
        await modal.on_submit(self.request)
        self.bot.backups.prepare.assert_awaited_once_with(self.request, '123000000000000000')
        self.assertIsInstance(self.request.edit_original_response.await_args.kwargs['view'], RestoreView)

    async def test_publication_reuses_pinned_card(self):
        self.request.permissions = discord.Permissions(administrator=True)
        channel = Mock(spec=discord.TextChannel)
        channel.id = 12
        channel.guild = self.request.guild
        self.request.guild.me = object()
        self.request.guild.fetch_channel = AsyncMock(return_value=channel)
        channel.permissions_for.return_value = discord.Permissions.all()
        self.bot._connection.user = SimpleNamespace(id=10)
        pinned = []
        async def pins(**kwargs):
            for message in pinned:
                yield message
        channel.pins.side_effect = pins
        posted = SimpleNamespace(author=self.bot.user, jump_url='https://discord.com/channels/2/12/4',
                                 components=[SimpleNamespace(children=[SimpleNamespace(custom_id=OPEN_ID)])])
        posted.content = '**LaFamily**'
        posted.edit = AsyncMock(side_effect=lambda **kwargs: setattr(posted, 'content', kwargs['content']))
        posted.pin = AsyncMock(side_effect=lambda **kwargs: pinned.append(posted))
        channel.send = AsyncMock(return_value=posted)
        for _ in range(3):
            await self.bot.menu.publish(self.request, 12)
        channel.send.assert_awaited_once()
        posted.pin.assert_awaited_once()
        posted.edit.assert_awaited_once()
        updated = posted.edit.await_args.kwargs
        self.assertEqual(set(updated), {'content', 'allowed_mentions'})
        self.assertEqual(updated['content'], LAUNCHER_TEXT)
        self.assertEqual(updated['allowed_mentions'].to_dict(), {'parse': []})
        self.assertIn(posted.jump_url, self.request.edit_original_response.await_args.kwargs['content'])
        self.assertIn('уже закреплена', self.request.edit_original_response.await_args.kwargs['content'])

        pinned.clear()
        posted.pin.side_effect = OSError('connection lost')
        with self.assertRaisesRegex(UserError, 'Закрепление не подтверждено') as failure:
            await self.bot.menu.publish(self.request, 12)
        self.assertIn(posted.jump_url, str(failure.exception))
        self.assertEqual(channel.send.await_count, 2)
