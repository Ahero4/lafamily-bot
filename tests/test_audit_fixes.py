"""Сценарии сбоев и отзыва доступа, воспроизведённые при обзоре 03.11.2026."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.async_database import DatabaseWorker
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.barn import register_barn_commands
from lafamily.modules.events.models import Draft
from lafamily.modules.events.service import EventService
from lafamily.modules.events.time import timestamp, week_range
from lafamily.modules.events.ui import report_embed
from lafamily.modules.onboarding import IdentityModal, OnboardingService, RecoveryView, ReviewView, RejectModal
from lafamily.modules.prices.notifications import notify_maximum_prices
from lafamily.modules.prices.parser import parse_prices
from lafamily.modules.prices.service import PriceService
from lafamily.modules.prices.subscriptions import PriceSubscriptionView

G = '111111111111111111'
A = '222222222222222222'
U = '333333333333333333'
V = '444444444444444444'
C = '555555555555555555'
M = '666666666666666666'
R = '777777777777777777'
EXAMPLE = Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')


def admin(member=None):
    return SimpleNamespace(guild_id=int(G), channel_id=int(C), id=888888888888888888,
        user=SimpleNamespace(id=int(A), display_name='Admin'), permissions=discord.Permissions(administrator=True),
        guild=SimpleNamespace(id=int(G), name='Test', owner_id=0, me=SimpleNamespace(id=999, top_role=10),
            fetch_member=AsyncMock(return_value=member), get_role=lambda number: object()),
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock()),
        edit_original_response=AsyncMock())


class ChannelTests(unittest.TestCase):
    def setters(self, store):
        return [
            lambda: store.configure(G,A,C,'org','manager'),
            lambda: store.configure_onboarding(G,C,'other',R),
            lambda: store.configure_onboarding(G,'other',C,R),
            lambda: store.configure_announcement_storage(G,C),
            lambda: store.save_announcement_template(G,'Test',C,R,'Body'),
            lambda: store.configure_release_channel(G,A,C),
            lambda: store.configure_database_backups(G,A,C),
            lambda: store.configure_admin_report_channel(G,A,C),
        ]

    def test_all_channel_assignments_reject_prices_in_both_orders_atomically(self):
        for index in range(8):
            for prices_first in (True,False):
                with self.subTest(setter=index, prices_first=prices_first):
                    store = Store(':memory:')
                    try:
                        setter = self.setters(store)[index]
                        if prices_first:
                            store.configure_prices(G,A,C)
                            with self.assertRaises(UserError): setter()
                            self.assertEqual(store.price_settings(G)['channel_id'],C)
                        else:
                            setter()
                            store.configure_prices(G,A,'separate')
                            with self.assertRaises(UserError): store.configure_prices(G,A,C)
                            self.assertEqual(store.price_settings(G)['channel_id'],'separate')
                        self.assertFalse(store.db.in_transaction)
                    finally:
                        store.close()

    def test_old_image_storage_and_pending_review_channels_are_protected(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.save_announcement_template(G,'Image','destination',R,'Body',json.dumps({'channel':C}))
        store.configure_announcement_storage(G,'new-storage')
        with self.assertRaises(UserError): store.configure_prices(G,A,C)
        store.save_onboarding_request(M,G,U,R,review_channel_id='old-review')
        with self.assertRaises(UserError): store.configure_prices(G,A,'old-review')

    def test_barn_cannot_credit_in_a_channel_that_will_be_cleaned(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.configure_prices(G,A,C)
        store.configure_barn(G,A,10)
        with self.assertRaises(UserError): store.create_barn_collection(G,A,U,C,'barn:test',100)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],0)


class AuditAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)

    async def test_concurrent_channel_configuration_cannot_create_collision(self):
        async with DatabaseWorker(':memory:') as worker:
            outcomes = await asyncio.gather(worker.call('configure_prices',G,A,C),
                worker.call('configure_announcement_storage',G,C), return_exceptions=True)
            self.assertEqual(sum(isinstance(r,UserError) for r in outcomes),1)

    async def test_cleanup_skips_existing_conflict_without_reading_or_deleting_history(self):
        self.store.configure_prices(G,A,C)
        # Имитируем настройку, сохранённую до исправления.
        self.store.db.execute('INSERT INTO announcement_settings VALUES (?,?)',(G,C))
        channel = SimpleNamespace(id=int(C), history=MagicMock())
        service = PriceService(None,self.store,int(G))
        await service.cleanup_channel(SimpleNamespace(channel=channel))
        channel.history.assert_not_called()

    async def test_cleanup_preserves_old_bot_cards_but_removes_old_price_messages(self):
        self.store.configure_prices(G,A,C)
        for source,result in (('10','11'),('20','21')):
            self.store.claim_price_publication(G,C,source,'x')
            self.store.save_price_publication(G,source,result,'x')
        messages = [SimpleNamespace(id=i,pinned=False,author=SimpleNamespace(id=999),
            content='old card',embeds=[],message_snapshots=[],delete=AsyncMock()) for i in (10,11,15,20,21)]
        async def history(**kwargs):
            for message in messages: yield message
        channel = SimpleNamespace(id=int(C),history=history,
            permissions_for=lambda member:SimpleNamespace(manage_messages=True,read_message_history=True),
            fetch_message=AsyncMock(side_effect=lambda i:next(m for m in messages if m.id==i)))
        service = PriceService(SimpleNamespace(user=SimpleNamespace(id=999)),self.store,int(G))
        await service.cleanup_channel(SimpleNamespace(id=20,channel=channel,guild=SimpleNamespace(me=object())))
        for message in messages:
            self.assertEqual(message.delete.await_count,1 if message.id in (10,11) else 0)

    async def test_ambiguous_signup_requires_refresh_before_credit(self):
        self.store.configure(G,A,C,'org','manager')
        kind = self.store.set_type(G,A,'Test',10)
        service = EventService(self.store)
        self.addCleanup(service.close)
        draft = Draft(G,A,C,kind['id'],'Test',10,timestamp(),members=[U])
        view = service.new_view(draft)
        shown = []
        async def lost_reply(**payload):
            shown.append(payload['embeds'][0].fields[0].value)
            raise OSError('reply lost after applying update')
        click = SimpleNamespace(guild_id=int(G),user=SimpleNamespace(id=int(V),bot=False),
            response=SimpleNamespace(edit_message=AsyncMock(side_effect=lost_reply)))
        with self.assertRaises(OSError): await view.join.callback(click)
        self.assertIn(V,shown[0])
        self.assertEqual(draft.members,[U])
        self.assertTrue(view.needs_refresh)
        service.channel = AsyncMock(return_value=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=int(M)))))
        interaction = admin(SimpleNamespace(bot=False))
        await view.dispatch('confirm',interaction)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],0)
        await view.dispatch('confirm',interaction)
        self.assertEqual(self.store.report(G,1)['members'],[U])

    def subscription(self):
        self.store.configure_prices(G,A,C)
        self.store.set_price_subscription(G,A,'Медь',True)
        interaction = admin()
        interaction.guild.fetch_channel = AsyncMock(return_value=SimpleNamespace(
            permissions_for=lambda user:SimpleNamespace(view_channel=True)))
        service = SimpleNamespace(store=self.store,require_enabled=lambda:None,require_channel=lambda *args:None)
        view = PriceSubscriptionView(service,interaction,['Медь'])
        self.addCleanup(view.stop)
        return interaction,view

    async def test_failed_product_selection_cannot_save_unseen_choices(self):
        interaction,view = self.subscription()
        interaction.response.edit_message.side_effect = OSError('lost')
        with self.assertRaises(OSError): await view.choose(interaction,['Карп'])
        self.assertEqual(view.selected,{'Медь'})
        self.assertTrue(next(o for o in view.selector.options if o.value=='Медь').default)
        await view.save(interaction)
        self.assertFalse(view.finished)
        self.assertEqual(self.store.price_subscriptions(G,A),['Медь'])
        await view.save(interaction)
        self.assertTrue(view.finished)
        self.assertEqual(self.store.price_subscriptions(G,A),['Медь'])

    async def test_failed_threshold_and_scope_changes_restore_the_full_menu(self):
        interaction,view = self.subscription()
        interaction.response.edit_message.side_effect = OSError('lost')
        with self.assertRaises(OSError): await view.choose_mode(interaction,'near')
        self.assertEqual(view.default_mode,'max')
        self.assertEqual(view.modes,{'Медь':'max'})
        view.scope_selector._values = ['Медь']
        with self.assertRaises(OSError): await view.scope_selector.callback(interaction)
        self.assertEqual(view.scope_item,'*')
        self.assertIn('все выбранные товары',view.mode_selector.placeholder)
        interaction.edit_original_response.side_effect = OSError('still offline')
        with self.assertRaises(OSError): await view.save(interaction)
        self.assertTrue(view.needs_refresh)

    def prices(self):
        self.store.configure_prices(G,A,C)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = int(C)
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True,send_messages=True,embed_links=True)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=int(M)))
        source = SimpleNamespace(id=int(R),guild=SimpleNamespace(id=int(G),me=object()),channel=channel,
            author=SimpleNamespace(id=int(A)),content=EXAMPLE,embeds=[],message_snapshots=[])
        service = PriceService(SimpleNamespace(user=SimpleNamespace(id=999),intents=SimpleNamespace(message_content=True)),self.store,int(G))
        return channel,source,service

    async def test_definite_http_refusal_can_retry_but_ambiguous_failures_cannot(self):
        for status in (400,403,429,408,500):
            with self.subTest(status=status):
                channel,source,service = self.prices()
                source.id += status
                channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=status,reason='test'),'test')
                with self.assertRaises(UserError): await service.process(source)
                channel.send.side_effect = None
                if status in (408,500):
                    with self.assertRaises(UserError): await service.process(source)
                    self.assertEqual(channel.send.await_count,1)
                else:
                    self.assertEqual(await service.process(source),'published')
                    self.assertEqual(channel.send.await_count,2)

    async def test_dm_checks_fresh_roles_and_preserves_subscription_on_denial(self):
        channel,source,_ = self.prices()
        self.store.set_price_subscription(G,U,'Медь',True)
        cached = SimpleNamespace(send=AsyncMock())
        fresh = SimpleNamespace(send=AsyncMock())
        source.guild.get_member = lambda uid:cached
        source.guild.fetch_member = AsyncMock(return_value=fresh)
        channel.permissions_for.side_effect = lambda member:SimpleNamespace(view_channel=member is cached)
        high = parse_prices(EXAMPLE.replace('- Медь: $36 ($43)','- Медь: $60 ($72)'))
        await notify_maximum_prices(self.store,source,high)
        cached.send.assert_not_awaited()
        fresh.send.assert_not_awaited()
        source.guild.fetch_member.assert_awaited_once_with(int(U))
        row = self.store.db.execute('SELECT state,error_type FROM price_delivery_attempts').fetchone()
        self.assertEqual(tuple(row),('forbidden','ChannelAccessDenied'))
        self.assertEqual(self.store.price_subscriptions(G,U),['Медь'])

    def onboarding(self):
        service = OnboardingService(self.store)
        service.check_role = lambda guild,role:None
        member = SimpleNamespace(id=int(U),top_role=1,edit=AsyncMock(),add_roles=AsyncMock(),send=AsyncMock())
        interaction = admin(member)
        message = SimpleNamespace(id=int(M),embeds=[service.embed('Test Person','123',int(U))],edit=AsyncMock())
        interaction.message = message
        self.store.save_onboarding_request(M,G,U,R,'Test Person','123',C)
        modal = IdentityModal(service,self.store.onboarding_request(M),message)
        modal.name._value,modal.static._value = 'Test Person','123'
        return service,member,interaction,message,modal

    async def test_onboarding_card_failure_does_not_block_dm_and_repair_never_reissues_role(self):
        service,member,interaction,message,modal = self.onboarding()
        message.edit.side_effect = OSError('lost card reply')
        await modal.on_submit(interaction)
        self.assertEqual(self.store.onboarding_request(M)['approved'],1)
        member.send.assert_awaited_once()
        self.assertEqual(message.edit.call_args.kwargs['embed'].footer.text,'Утвердил: Admin')
        message.edit.side_effect = None
        restarted = OnboardingService(self.store)
        view = ReviewView(restarted)
        self.addCleanup(view.stop)
        await view.review.callback(interaction)
        member.add_roles.assert_awaited_once()
        member.edit.assert_awaited_once()
        member.send.assert_awaited_once()
        self.assertIsNone(message.edit.call_args.kwargs['view'])

    async def test_onboarding_explicit_dm_refusal_has_a_manual_retry_without_second_decision(self):
        service,member,interaction,message,modal = self.onboarding()
        member.send.side_effect = discord.Forbidden(SimpleNamespace(status=403,reason='closed'),'closed')
        await modal.on_submit(interaction)
        self.assertEqual(self.store.onboarding_request(M)['notification_state'],'failed')
        repair = message.edit.call_args.kwargs['view']
        self.addCleanup(repair.stop)
        self.assertIsInstance(repair,RecoveryView)
        member.send.side_effect = None
        await repair.repair.callback(interaction)
        self.assertEqual(self.store.onboarding_request(M)['notification_state'],'sent')
        self.assertEqual(member.send.await_count,2)
        member.add_roles.assert_awaited_once()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM audit_log WHERE action='onboarding.approved'").fetchone()[0],1)

    async def test_onboarding_ambiguous_dm_or_interrupted_attempt_is_not_repeated(self):
        service,member,interaction,message,modal = self.onboarding()
        member.send.side_effect = OSError('uncertain delivery')
        await modal.on_submit(interaction)
        await service.complete(interaction,message)
        member.send.assert_awaited_once()
        self.assertEqual(self.store.onboarding_request(M)['notification_state'],'uncertain')
        self.store.db.execute("UPDATE onboarding_requests SET notification_state='sending' WHERE message_id=?",(M,))
        await OnboardingService(self.store).complete(interaction,message)
        member.send.assert_awaited_once()

    async def test_rejected_decision_can_repair_card_without_another_notification(self):
        service,member,interaction,message,_ = self.onboarding()
        modal = RejectModal(service,message)
        modal.reason._value = 'Incorrect details'
        message.edit.side_effect = OSError('offline')
        await modal.on_submit(interaction)
        self.assertEqual(self.store.onboarding_request(M)['approved'],-1)
        member.send.assert_awaited_once()
        message.edit.side_effect = None
        view = ReviewView(OnboardingService(self.store))
        self.addCleanup(view.stop)
        await view.review.callback(interaction)
        member.send.assert_awaited_once()
        member.add_roles.assert_not_awaited()
        self.assertIn('отклонена',message.edit.call_args.kwargs['embed'].title)
        self.assertEqual(message.edit.call_args.kwargs['embed'].footer.text,'Отклонил: Admin')

    async def test_repair_uses_original_administrator_name_from_cache_or_api(self):
        service,member,interaction,message,modal = self.onboarding()
        await modal.on_submit(interaction)
        interaction.user = SimpleNamespace(id=int(V),display_name='Other admin')
        original = SimpleNamespace(id=int(A),display_name='Original admin')
        for cached in (original,None):
            with self.subTest(cached=cached is not None):
                interaction.guild.get_member = lambda uid: cached
                interaction.guild.fetch_member = AsyncMock(return_value=original)
                await OnboardingService(self.store).complete(interaction,message)
                self.assertEqual(message.edit.call_args.kwargs['embed'].footer.text,'Утвердил: Original admin')
                if cached is None:
                    interaction.guild.fetch_member.assert_awaited_once_with(int(A))
                else:
                    interaction.guild.fetch_member.assert_not_awaited()
        self.assertEqual(self.store.onboarding_request(M)['actor_id'],A)
        member.send.assert_awaited_once()
        member.add_roles.assert_awaited_once()

    async def test_unavailable_original_administrator_does_not_break_card_or_expose_id(self):
        service,member,interaction,message,modal = self.onboarding()
        await modal.on_submit(interaction)
        interaction.user = SimpleNamespace(id=int(V),display_name='Other admin')
        interaction.guild.get_member = lambda uid:None
        interaction.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404,reason='Not found'),'left server'))
        await OnboardingService(self.store).complete(interaction,message)
        footer = message.edit.call_args.kwargs['embed'].footer.text
        self.assertEqual(footer,'Утверждено администратором')
        self.assertNotIn(A,footer)
        self.assertIsNone(message.edit.call_args.kwargs['view'])
        member.send.assert_awaited_once()
        member.add_roles.assert_awaited_once()

    async def test_legacy_decision_without_actor_keeps_neutral_footer_and_no_new_dm(self):
        service,member,interaction,message,_ = self.onboarding()
        self.store.db.execute("UPDATE onboarding_requests SET approved=-1,actor_id=NULL,notification_state='legacy' WHERE message_id=?",(M,))
        await service.complete(interaction,message)
        self.assertEqual(message.edit.call_args.kwargs['embed'].footer.text,'Отклонено администратором')
        member.send.assert_not_awaited()
        member.add_roles.assert_not_awaited()

    async def test_submission_is_saved_before_sending_and_buttons_recover_failed_binding(self):
        service = OnboardingService(self.store)
        self.store.configure_onboarding(G,C,V,R)
        interaction = admin()
        interaction.client = SimpleNamespace(user=SimpleNamespace(id=999))
        captured = []
        async def send(**kwargs):
            self.assertIsNotNone(self.store.onboarding_request(f'pending:{interaction.id}'))
            captured.append(kwargs)
            return SimpleNamespace(id=int(M))
        interaction.guild.fetch_channel = AsyncMock(return_value=SimpleNamespace(send=AsyncMock(side_effect=send)))
        modal = IdentityModal(service)
        modal.name._value,modal.static._value = 'Test Person','123'
        with patch.object(self.store,'attach_onboarding_message',side_effect=sqlite3.OperationalError('busy')):
            await modal.on_submit(interaction)
        self.assertIsInstance(captured[0]['view'],ReviewView)
        captured[0]['view'].stop()
        interaction.channel_id = int(V)
        interaction.message = SimpleNamespace(id=int(M),author=SimpleNamespace(id=999),embeds=[captured[0]['embed']])
        row = await OnboardingService(self.store).request_for_message(interaction)
        self.assertEqual((row['message_id'],row['user_id']),(M,A))
        self.assertIsNone(self.store.onboarding_request(f'pending:{interaction.id}'))

    async def test_submission_database_failure_never_sends_an_orphan_card(self):
        service = OnboardingService(self.store)
        self.store.configure_onboarding(G,C,V,R)
        interaction = admin()
        channel = SimpleNamespace(send=AsyncMock())
        interaction.guild.fetch_channel = AsyncMock(return_value=channel)
        modal = IdentityModal(service)
        modal.name._value,modal.static._value = 'Test Person','123'
        with patch.object(self.store,'save_onboarding_request',side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError): await modal.on_submit(interaction)
        channel.send.assert_not_awaited()

    async def test_lost_submission_response_keeps_data_for_the_delivered_card(self):
        service = OnboardingService(self.store)
        self.store.configure_onboarding(G,C,V,R)
        interaction = admin()
        sent = []
        async def lose_reply(**payload):
            sent.append(payload)
            raise OSError('reply lost after delivery')
        channel = SimpleNamespace(send=AsyncMock(side_effect=lose_reply))
        interaction.guild.fetch_channel = AsyncMock(return_value=channel)
        modal = IdentityModal(service)
        modal.name._value,modal.static._value = 'Test Person','123'
        with self.assertRaises(UserError): await modal.on_submit(interaction)
        self.assertIsNotNone(self.store.onboarding_request(f'pending:{interaction.id}'))
        sent[0]['view'].stop()
        interaction.client = SimpleNamespace(user=SimpleNamespace(id=999))
        interaction.channel_id = int(V)
        interaction.message = SimpleNamespace(id=int(M),author=SimpleNamespace(id=999),embeds=[sent[0]['embed']])
        self.assertEqual((await service.request_for_message(interaction))['message_id'],M)
        channel.send.assert_awaited_once()

    async def test_definite_submission_refusal_does_not_leave_a_pending_request(self):
        service = OnboardingService(self.store)
        self.store.configure_onboarding(G,C,V,R)
        interaction = admin()
        channel = SimpleNamespace(send=AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403,reason='forbidden'),'test')))
        interaction.guild.fetch_channel = AsyncMock(return_value=channel)
        modal = IdentityModal(service)
        modal.name._value,modal.static._value = 'Test Person','123'
        with self.assertRaises(UserError): await modal.on_submit(interaction)
        self.assertIsNone(self.store.onboarding_request(f'pending:{interaction.id}'))

    async def test_pending_binding_rejects_other_author_guild_or_channel(self):
        service = OnboardingService(self.store)
        interaction = admin()
        interaction.client = SimpleNamespace(user=SimpleNamespace(id=999))
        embed = service.embed('Test Person','123',int(U))
        embed.set_footer(text=f'Заявка: {interaction.id}')
        self.store.save_onboarding_request(f'pending:{interaction.id}',G,U,R,review_channel_id=C)
        interaction.message = SimpleNamespace(id=int(M),author=SimpleNamespace(id=998),embeds=[embed])
        with self.assertRaises(UserError): await service.request_for_message(interaction)
        interaction.message.author.id = 999
        interaction.channel_id = 'other'
        with self.assertRaises(UserError): await service.request_for_message(interaction)
        interaction.channel_id = int(C)
        interaction.guild_id = 123
        with self.assertRaises(UserError): await service.request_for_message(interaction)

    async def test_barn_stores_message_and_cancel_updates_same_card_including_legacy(self):
        self.store.configure_barn(G,A,10)
        client = discord.Client(intents=discord.Intents.none())
        self.addAsyncCleanup(client.close)
        tree = discord.app_commands.CommandTree(client)
        register_barn_commands(tree,self.store,int(G))
        command = tree.get_command('коровник',guild=discord.Object(id=int(G))).get_command('сбор')
        interaction = admin(SimpleNamespace(id=int(A),bot=False))
        interaction.edit_original_response.return_value = SimpleNamespace(id=int(M))
        await command.callback(interaction,'100')
        self.assertEqual(self.store.report(G,1)['message_id'],M)
        report = self.store.cancel_report(G,A,1,'Test')
        self.assertEqual(self.store.statistics(G,week_range()),[])
        message = SimpleNamespace(id=int(M),author=SimpleNamespace(id=999),embeds=[report_embed({**report,'cancelled_at':None})])
        channel = MagicMock(spec=discord.TextChannel)
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True,send_messages=True,embed_links=True)
        partial = SimpleNamespace(edit=AsyncMock())
        channel.get_partial_message.return_value = partial
        async def history(**kwargs): yield message
        channel.history = history
        interaction.guild.fetch_channel = AsyncMock(return_value=channel)
        service = EventService(self.store)
        for legacy in (False,True):
            if legacy:
                self.store.db.execute('UPDATE reports SET message_id=NULL WHERE id=1')
                report = self.store.report(G,1)
            self.assertTrue(await service.update_cancelled(interaction.guild,report))
            self.assertIn('отменён',partial.edit.call_args.kwargs['embed'].title)
            self.assertEqual(self.store.report(G,1)['message_id'],M)


class OnboardingMigrationTests(unittest.TestCase):
    def test_legacy_pending_and_approved_requests_survive_repeated_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'legacy.sqlite'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE onboarding_requests(message_id TEXT PRIMARY KEY,guild_id TEXT,user_id TEXT,role_id TEXT,approved INTEGER DEFAULT 0)')
            db.executemany('INSERT INTO onboarding_requests VALUES (?,?,?,?,?)',[(M,G,U,R,1),('pending',G,V,R,0)])
            db.commit()
            db.close()
            for _ in range(2):
                store = Store(path)
                try:
                    self.assertEqual(store.onboarding_request(M)['approved'],1)
                    self.assertFalse(store.claim_onboarding_notification(M))
                    self.assertEqual(store.onboarding_request('pending')['approved'],0)
                finally:
                    store.close()
