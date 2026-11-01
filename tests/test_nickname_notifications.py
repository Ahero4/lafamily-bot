"""Уведомления: согласие, повторная проверка получателей и защита от повторной отправки."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import discord
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.nickname_notifications import NicknameNoticeView

GUILD=111111111111111111
ACTOR=222222222222222222
CHANNEL=333333333333333333
ROLE=444444444444444444
BASE=500000000000000000

async def stream(values):
    for value in values: yield value

def member(index,name='Wrong',roles=(ROLE,),bot=False):
    return SimpleNamespace(id=BASE+index,display_name=name,roles=[SimpleNamespace(id=r) for r in roles],bot=bot)

class NoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.current=[member(1)]
        self.permissions=SimpleNamespace(view_channel=True,send_messages=True)
        self.channel=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=999)),permissions_for=lambda me:self.permissions)
        self.guild=SimpleNamespace(me=SimpleNamespace(id=999),fetch_channel=AsyncMock(return_value=self.channel),
            fetch_members=MagicMock(side_effect=lambda **kwargs:stream(self.current)))
        self.views=[]
        self.view=self.new_view([BASE+1])

    def request(self,admin=True,actor=ACTOR):
        return SimpleNamespace(user=SimpleNamespace(id=actor),guild_id=GUILD,guild=self.guild,channel_id=CHANNEL,
            permissions=discord.Permissions(administrator=admin),response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),followup=SimpleNamespace(send=AsyncMock()))

    def new_view(self,recipients,role_id=ROLE):
        view=NicknameNoticeView(self.store,self.request(),recipients,role_id)
        self.views.append(view);self.addCleanup(view.finish)
        return view

    async def test_no_public_message_until_confirmation_and_cancel_sends_nothing(self):
        self.channel.send.assert_not_awaited()
        await self.view.handle(self.request(),'cancel')
        self.channel.send.assert_not_awaited()
        self.guild.fetch_members.assert_not_called()
        self.assertTrue(self.view.finished)

    async def test_confirmation_rechecks_name_role_bot_and_departed_members(self):
        self.view.finish()
        self.view=self.new_view([BASE+i for i in (1,2,3,5,6)])
        self.current=[member(1),member(2,'Sin Bertoni | 15096'),member(3,roles=()),member(4),member(6,bot=True)]
        callback=self.request()
        await self.view.handle(callback,'confirm')
        self.guild.fetch_channel.assert_awaited_once_with(CHANNEL)
        self.channel.send.assert_awaited_once()
        payload=self.channel.send.call_args.kwargs
        self.assertIn(f'<@{BASE+1}>',payload['content'])
        for index in (2,3,4,5,6): self.assertNotIn(f'<@{BASE+index}>',payload['content'])
        mentions=payload['allowed_mentions'].to_dict()
        self.assertEqual(mentions['users'],[BASE+1])
        self.assertNotIn('everyone',mentions['parse']);self.assertNotIn('roles',mentions['parse'])
        self.assertNotIn('@everyone',payload['content'])
        self.assertTrue(self.view.finished)
        with self.assertRaises(UserError): await self.view.handle(self.request(),'confirm')
        self.channel.send.assert_awaited_once()

    async def test_other_user_and_revoked_administrator_cannot_confirm(self):
        for request in (self.request(actor=ACTOR+1),self.request(admin=False)):
            with self.assertRaises(UserError): await self.view.handle(request,'confirm')
        self.channel.send.assert_not_awaited()

    async def test_expiry_never_sends_and_removes_buttons(self):
        await self.view.on_timeout()
        self.assertTrue(self.view.finished)
        self.assertIsNone(self.view.original.edit_original_response.call_args.kwargs['view'])
        self.channel.send.assert_not_awaited()

    async def test_all_fixed_means_no_notification(self):
        self.current=[member(1,'Зам. | Sin Bertoni | 15096')]
        request=self.request()
        await self.view.handle(request,'confirm')
        self.channel.send.assert_not_awaited()
        self.assertIn('Уведомлять некого',request.edit_original_response.call_args.kwargs['content'])

    async def test_channel_permissions_are_checked_before_sending(self):
        self.permissions.send_messages=False
        with self.assertRaises(UserError): await self.view.handle(self.request(),'confirm')
        self.assertFalse(self.view.finished)
        self.channel.send.assert_not_awaited()

    async def test_failed_revalidation_produces_no_partial_notifications(self):
        async def broken(**kwargs):
            yield member(1)
            raise discord.HTTPException(SimpleNamespace(status=500,reason='Error'),'offline')
        self.guild.fetch_members.side_effect=broken
        with self.assertRaises(UserError): await self.view.handle(self.request(),'confirm')
        self.assertFalse(self.view.finished)
        self.channel.send.assert_not_awaited()

    async def test_partial_failure_stops_and_does_not_retry_ambiguous_batch(self):
        self.view.finish()
        self.current=[member(i) for i in range(45)]
        self.view=self.new_view([m.id for m in self.current],None)
        self.channel.send.side_effect=[SimpleNamespace(id=1),RuntimeError('offline'),SimpleNamespace(id=2)]
        request=self.request()
        with self.assertLogs('lafamily.errors',level='ERROR'):
            await self.view.handle(request,'confirm')
        self.assertEqual(self.channel.send.await_count,2)
        text=request.edit_original_response.call_args.kwargs['content']
        self.assertIn('для **20**',text);self.assertIn('оставшихся **5**',text)
        with self.assertRaises(UserError): await self.view.handle(self.request(),'confirm')
        self.assertEqual(self.channel.send.await_count,2)

    async def test_double_click_during_send_does_not_duplicate_notification(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow_send(**kwargs):
            entered.set();await release.wait();return SimpleNamespace(id=1)
        self.channel.send.side_effect=slow_send
        first=asyncio.create_task(self.view.handle(self.request(),'confirm'))
        await entered.wait()
        try:
            with self.assertRaises(UserError): await self.view.handle(self.request(),'confirm')
        finally:
            release.set();await first
        self.channel.send.assert_awaited_once()

    async def test_failure_of_private_status_cannot_resend_public_message(self):
        request=self.request();request.edit_original_response.side_effect=RuntimeError('offline')
        with self.assertLogs('lafamily.errors',level='ERROR'):
            await self.view.handle(request,'confirm')
        self.channel.send.assert_awaited_once()
        request.followup.send.assert_awaited_once()
        with self.assertRaises(UserError): await self.view.handle(self.request(),'confirm')
        self.channel.send.assert_awaited_once()

    async def test_thread_uses_thread_send_permission(self):
        thread=MagicMock(spec=discord.Thread)
        thread.permissions_for.return_value=SimpleNamespace(view_channel=True,send_messages=False,send_messages_in_threads=True)
        thread.send=AsyncMock(return_value=SimpleNamespace(id=1))
        self.guild.fetch_channel.return_value=thread
        await self.view.handle(self.request(),'confirm')
        thread.send.assert_awaited_once()
