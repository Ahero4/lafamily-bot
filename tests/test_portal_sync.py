import asyncio
import unittest
from datetime import UTC, datetime

import discord
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from lafamily.database import Store
from lafamily.portal_sync import PortalSync

G = '111111111111111111'
U = '222222222222222222'
C = '333333333333333333'


class PortalDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.event = self.store.create_planned_event(G, U, 'plan', C, 'Event', '2099-01-01T00:00:00.000Z', 1)
        self.store.bind_planned_message(G, self.event['id'], '444444444444444444')
        self.command = {'id': 'command', 'eventId': str(self.event['id']), 'discordId': U, 'choice': 'going', 'expiresAt': '2099-01-01T00:00:00Z'}

    def test_unchanged_revision_retries_and_stale_ack(self):
        draft = dict(id='draft:1', kind='draft', name='draft', closed=False)
        self.store.portal_scan(G, {C}, [draft])
        old = self.store.portal_batch(G)
        self.assertEqual(old, self.store.portal_batch(G))
        self.assertEqual(len(old['events']), 2)
        self.store.portal_scan(G, {C}, [])
        self.store.portal_ack(old)
        pending = self.store.portal_batch(G)['events']
        self.assertEqual([(e['id'], e['closed']) for e in pending], [('draft:1', True)])
        self.assertGreater(pending[0]['revision'], next(e['revision'] for e in old['events'] if e['id'] == draft['id']))

    def test_removed_profile_delivers_zero_and_signup_receipt_is_replayed(self):
        self.store.portal_remember(G, 'profiles', U, {'discordId': U, 'total': 22})
        self.store.portal_ack(self.store.portal_batch(G))
        self.store.portal_scan(G, {C}, [])
        zero, = self.store.portal_batch(G)['profiles']
        for key in ('total', 'week', 'events', 'wins', 'mvps', 'weekEvents', 'weekWins', 'weekMvps'):
            self.assertEqual(zero[key], 0)
        self.store.portal_signup(G, self.command)
        self.store.portal_ack(self.store.portal_batch(G))
        self.store.portal_signup(G, dict(self.command, choice='no'))
        self.assertEqual(self.store.portal_batch(G)['results'], [{'id': 'command', 'ok': True}])
        self.assertEqual(self.store.planned_event(G, self.event['id'])['going'], [U])

    def test_idle_scan_does_not_read_reports_or_plans(self):
        self.store.portal_scan(G, {C}, [])
        sql = []
        self.store.db.set_trace_callback(sql.append)
        self.store.portal_scan(G, {C}, [])
        self.assertTrue(sql)
        self.assertNotRegex(' '.join(sql).lower(), r'\b(reports|report_members|planned_events|planned_signups)\b')

    async def test_idle_and_busy_polling(self):
        for busy, upper_bound in ((False, 0.5), (True, 0.05)):
            client = Mock(config=SimpleNamespace(guild_id=G), backups=SimpleNamespace(restoring=False))
            sync = PortalSync(client)
            sync.origin = 'https://example.invalid'
            sync.db = AsyncMock()
            sync.exchange = AsyncMock(return_value=busy)
            async def stop(awaitable, timeout):
                awaitable.close()
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, upper_bound)
                raise asyncio.CancelledError

            wait = AsyncMock(side_effect=stop)
            with patch('lafamily.portal_sync.aiohttp.ClientSession', return_value=MagicMock()), \
                    patch('lafamily.portal_sync.asyncio.wait_for', wait):
                with self.assertRaises(asyncio.CancelledError):
                    await sync.run()
            wait.assert_awaited_once()

    async def test_removed_card_closes_snapshot_and_rejects_signup(self):
        self.store.portal_scan(G, {C}, [])
        self.store.portal_ack(self.store.portal_batch(G))
        guild = Mock(id=int(G), fetch_member=AsyncMock(return_value=Mock(bot=False)))
        channel = guild.get_channel_or_thread.return_value
        channel.guild = guild
        channel.permissions_for.return_value.view_channel = True
        missing = discord.NotFound(Mock(status=404, reason='Missing'), 'Unknown Message')
        channel.fetch_message = AsyncMock(side_effect=missing)
        client = Mock(config=SimpleNamespace(guild_id=G), events=SimpleNamespace(views={}))
        client.get_guild.return_value = guild
        sync = PortalSync(client)
        sync.channels = {C}

        async def invoke(method, *args):
            return getattr(self.store, method)(*args)

        sync.db = invoke
        await sync.command(self.command)
        self.store.portal_scan(G, {C}, [])
        batch = self.store.portal_batch(G)
        self.assertFalse(batch['results'][0]['ok'])
        self.assertTrue(batch['events'][0]['closed'])
        self.assertEqual(self.store.planned_event(G, self.event['id'])['going'], [])

    async def test_member_without_channel_visibility_cannot_join(self):
        guild = Mock(id=int(G), fetch_member=AsyncMock(return_value=Mock(bot=False)))
        channel = guild.get_channel_or_thread.return_value
        channel.guild = guild
        channel.permissions_for.return_value.view_channel = False
        client = Mock(config=SimpleNamespace(guild_id=G), events=SimpleNamespace(views={}))
        client.get_guild.return_value = guild
        sync = PortalSync(client)
        sync.channels = {C}

        async def invoke(method, *args):
            return getattr(self.store, method)(*args)

        sync.db = invoke
        await sync.command(self.command)
        self.assertFalse(self.store.portal_batch(G)['results'][0]['ok'])
        self.assertEqual(self.store.planned_event(G, self.event['id'])['going'], [])

    async def test_commands_arriving_while_discord_busy_are_processed_in_order(self):
        sync = PortalSync(Mock(config=SimpleNamespace(guild_id=G), backups=SimpleNamespace(restoring=False),
                               events=SimpleNamespace(views={})))

        async def invoke(method, *args):
            return getattr(self.store, method)(*args)

        sync.db = invoke
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def command(cmd):
            seen.append(cmd['id'])
            if cmd['id'] == 'one':
                started.set()
                await release.wait()

        sync.command = command
        session = MagicMock()
        response = session.post.return_value.__aenter__.return_value
        response.status = 200
        response.json = AsyncMock(side_effect=[
            {'commands': [{'id': 'one'}]},
            {'commands': [{'id': 'one'}, {'id': 'two'}]},
        ])
        try:
            await sync.exchange(session)
            await asyncio.wait_for(started.wait(), 1)
            await sync.exchange(session)
            release.set()
            await asyncio.wait_for(sync.command_task, 1)
            self.assertEqual(seen, ['one', 'two'])
        finally:
            release.set()
            await sync.close()

    def sync_for_cards(self):
        guild = Mock(id=int(G), fetch_member=AsyncMock(return_value=Mock(bot=False)))
        channel = guild.get_channel_or_thread.return_value
        channel.guild = guild
        channel.permissions_for.return_value.view_channel = True
        client = Mock(config=SimpleNamespace(guild_id=G), events=SimpleNamespace(views={}),
                      store=self.store, backups=SimpleNamespace(restoring=False))
        client.planning_card_lock = asyncio.Lock()
        client.get_guild.return_value = guild
        sync = PortalSync(client)
        sync.channels = {C}
        message = Mock(author=client.user, embeds=[discord.Embed(title='test')], edit=AsyncMock())
        channel.fetch_message = AsyncMock(return_value=message)
        return sync, channel, message

    async def test_request_expires_while_waiting_for_discord(self):
        sync, channel, message = self.sync_for_cards()
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(_):
            entered.set()
            await release.wait()
            return message

        channel.fetch_message.side_effect = fetch
        with patch('lafamily.portal_store.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2098, 12, 31, tzinfo=UTC)
            task = asyncio.create_task(sync.command(self.command))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                clock.now.return_value = datetime(2099, 1, 1, tzinfo=UTC)
            finally:
                release.set()
                await asyncio.wait_for(task, 1)
        self.assertFalse(self.store.portal_batch(G)['results'][0]['ok'])
        self.assertEqual(self.store.planned_event(G, self.event['id'])['going'], [])

    async def test_signup_during_discord_edit_keeps_new_card_refresh(self):
        sync, _, message = self.sync_for_cards()
        self.store.portal_signup(G, self.command)
        number = self.event['id']
        entered, released = asyncio.Event(), asyncio.Event()

        async def edit(**kwargs):
            entered.set()
            await released.wait()

        message.edit.side_effect = edit
        task = asyncio.create_task(sync.refresh_card(number))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.store.portal_signup(G, dict(self.command, id='second', choice='no'))
        finally:
            released.set()
            await asyncio.wait_for(task, 1)
        self.assertIn(number, self.store.portal_cards(G))
        await sync.refresh_card(number)
        self.assertNotIn(number, self.store.portal_cards(G))
        self.assertIn('0 / 1', message.edit.await_args.kwargs['embed'].fields[-1].value)

    async def test_uneditable_card_does_not_block_other_cards(self):
        sync, _, _ = self.sync_for_cards()
        self.store.portal_signup(G, self.command)
        second = self.store.create_planned_event(G, U, 'second', C, 'Second', '2099-01-01T00:00:00.000Z', 1)
        self.store.bind_planned_message(G, second['id'], '555555555555555555')
        self.store.portal_signup(G, dict(self.command, id='another', eventId=str(second['id'])))
        first = self.event['id']
        real_refresh = sync.refresh_card

        async def refresh(number):
            if number == first:
                raise discord.Forbidden(Mock(status=403, reason='Forbidden'), 'Forbidden')
            await real_refresh(number)

        sync.refresh_card = refresh
        with patch('lafamily.portal_sync.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await sync.maintain_cards()
        self.assertEqual(self.store.portal_cards(G), [first])

    async def test_request_expires_while_waiting_for_draft_lock(self):
        sync, channel, _ = self.sync_for_cards()
        entered = asyncio.Event()
        lock = asyncio.Lock()
        await lock.acquire()

        class ObservedLock:
            async def __aenter__(self):
                entered.set()
                await lock.acquire()

            async def __aexit__(self, *exc):
                lock.release()

        view = Mock(message=Mock(channel=channel), lock=ObservedLock(), update_members_with=AsyncMock())
        sync.drafts = lambda: {'draft:555555555555555555': view}
        with patch('lafamily.portal_store.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2098, 12, 31, tzinfo=UTC)
            cmd = dict(self.command, eventId='draft:555555555555555555')
            task = asyncio.create_task(sync.command(cmd))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                clock.now.return_value = datetime(2099, 1, 1, tzinfo=UTC)
            finally:
                lock.release()
                await asyncio.wait_for(task, 1)
        self.assertFalse(self.store.portal_batch(G)['results'][0]['ok'])
        view.update_members_with.assert_not_awaited()

    def draft_for_portal(self):
        from lafamily.modules.events.models import Draft, new_direct_report_request_id
        from lafamily.modules.events.ui import ReportView

        sync, channel, _ = self.sync_for_cards()
        channel.id = int(C)
        channel.guild.fetch_member.return_value.voice = None
        other = '666666666666666666'
        draft = Draft(G, U, C, 1, 'Test', 5, '2026-11-24T10:00:00Z',
                      request_id=new_direct_report_request_id(), members=[U, other],
                      voice_channel_id=C, mvp_id=U, mvp_bonus=3)
        service = SimpleNamespace(views={}, store=self.store)
        view = ReportView(service, draft)
        self.addCleanup(view.finish)
        view.message = Mock(id=444444444444444444, channel=channel, edit=AsyncMock())
        service.views[(G,U)] = view
        sync.client.events = service
        cmd = dict(self.command, eventId='draft:' + str(view.message.id), choice='no')
        return sync, view, cmd, other

    async def test_draft_organizer_can_leave_without_voice_and_retry(self):
        sync, view, cmd, other = self.draft_for_portal()
        pending = '777777777777777777'
        view.pending = [pending]
        await sync.command(cmd)
        self.assertEqual(view.draft.members, [other])
        self.assertEqual(view.pending, [pending])
        self.assertEqual((view.draft.mvp_id, view.draft.mvp_bonus), (None, 0))
        self.assertEqual(sync.draft_snapshots()[0]['going'], [other])
        for retry in (cmd, dict(cmd, id='repeat')):
            await sync.command(retry)
        self.assertTrue(all(row['ok'] for row in self.store.portal_batch(G)['results']))
        view.message.edit.assert_awaited_once()

    async def test_draft_pending_user_can_leave_while_other_mvp_remains(self):
        sync, view, cmd, other = self.draft_for_portal()
        view.draft.members = [other]
        view.draft.mvp_id = other
        view.pending = [U, '777777777777777777']
        await sync.command(cmd)
        self.assertEqual(view.pending, ['777777777777777777'])
        self.assertEqual(view.draft.members, [other])
        self.assertEqual((view.draft.mvp_id, view.draft.mvp_bonus), (other, 3))
        self.assertTrue(self.store.portal_batch(G)['results'][0]['ok'])
        view.message.edit.assert_awaited_once()

    async def test_draft_withdrawal_rolls_back_failed_edit_then_retries(self):
        sync, view, cmd, other = self.draft_for_portal()
        view.pending = [U, '777777777777777777']
        view.message.edit.side_effect = RuntimeError('temporary transport failure')
        with self.assertRaises(RuntimeError):
            await sync.command(cmd)
        self.assertEqual(view.draft.members, [U, other])
        self.assertEqual(view.pending, [U, '777777777777777777'])
        self.assertEqual((view.draft.mvp_id, view.draft.mvp_bonus), (U, 3))
        self.assertFalse(self.store.portal_batch(G)['results'])
        view.message.edit.side_effect = None
        await sync.command(cmd)
        self.assertEqual(view.pending, ['777777777777777777'])
        self.assertEqual(view.draft.members, [other])

    async def test_closed_expired_or_inaccessible_draft_refuses_withdrawal(self):
        sync, view, cmd, other = self.draft_for_portal()
        channel = view.message.channel
        for condition in ('finished', 'expired', 'hidden'):
            with self.subTest(condition=condition):
                view.finished = condition == 'finished'
                view.expires_at = 0 if condition == 'expired' else float('inf')
                channel.permissions_for.return_value.view_channel = condition != 'hidden'
                await sync.command(dict(cmd, id=condition))
                result = next(r for r in self.store.portal_batch(G)['results'] if r['id'] == condition)
                self.assertFalse(result['ok'])
                self.assertEqual(view.draft.members, [U, other])
                view.message.edit.assert_not_awaited()
