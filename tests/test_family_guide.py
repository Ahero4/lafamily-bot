import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.family_guide import FamilyGuide, guide_embed


def missing():
    return discord.NotFound(SimpleNamespace(status=404, reason='missing'), {'code': 10008})


class FamilyGuideTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'family.sqlite'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.client = SimpleNamespace(config=SimpleNamespace(guild_id=2), backups=SimpleNamespace(restoring=False),
            store=self.store, user=SimpleNamespace(id=8), fetch_channel=AsyncMock())
        self.guide = FamilyGuide(self.client)
        self.channels = {}
        self.client.fetch_channel.side_effect = self.channels.__getitem__

    def channel(self, ident):
        channel = Mock(spec=discord.TextChannel)
        channel.id, channel.guild = ident, SimpleNamespace(id=2, me=object())
        channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True,
            send_messages=True, embed_links=True)
        posts = {}
        channel.posts = posts
        async def fetch(ident):
            if ident not in posts:
                raise missing()
            return posts[ident]
        async def history(*, limit, after=None):
            selected = sorted((n for n in posts if after is None or n > after.id), reverse=True)
            for n in selected[:limit]:
                yield posts[n]
        async def send(*, embed, **kwargs):
            number = max([channel.id * 100, *posts]) + 1
            message = SimpleNamespace(id=number, author=self.client.user, embeds=[discord.Embed.from_dict(embed.to_dict())],
                jump_url=f'https://discord.com/channels/2/{ident}/{number}', edit=AsyncMock())
            async def edit(*, embed, **kwargs):
                message.embeds = [discord.Embed.from_dict(embed.to_dict())]
            message.edit.side_effect = edit
            posts[number] = message
            return message
        channel.fetch_message = AsyncMock(side_effect=fetch)
        channel.history.side_effect = history
        channel.send = AsyncMock(side_effect=send)
        self.channels[ident] = channel
        return channel

    async def select(self, channel):
        async with self.guide.lock:
            return await self.guide.select(channel.id)

    async def test_repeated_publication_reuses_survives_restart_and_updates_release(self):
        channel = self.channel(12)
        await asyncio.gather(self.select(channel), self.select(channel))
        message, = channel.posts.values()
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.call_args.kwargs['allowed_mentions'].to_dict(), {'parse': []})
        message.edit.assert_not_awaited()
        second_store = Store(self.path)
        self.addCleanup(second_store.close)
        self.client.store = second_store
        restarted = FamilyGuide(self.client)
        message.embeds[0].set_footer(text='LaFamily · Памятка семьи · версия 0.1')
        await restarted.sync()
        await restarted.sync()
        message.edit.assert_awaited_once()
        self.assertEqual(message.embeds[0], guide_embed())
        self.assertEqual(len(channel.posts), 1)
        channel.posts.clear()
        await restarted.sync()
        self.assertEqual(channel.send.await_count, 2)
        self.assertEqual(len(channel.posts), 1)

    async def test_move_recovers_lost_reply_without_duplicate_or_losing_old_binding(self):
        old, new = self.channel(12), self.channel(13)
        await self.select(old)
        original, = old.posts.values()
        send = new.send.side_effect
        async def lost_reply(**kwargs):
            await send(**kwargs)
            raise OSError('response lost')
        new.send.side_effect = lost_reply
        with self.assertRaises(OSError):
            await self.select(new)
        rows = {r['channel_id']: r for r in self.store.family_guide_posts('2')}
        self.assertEqual((rows['12']['status'], rows['13']['status']), ('active', 'target'))
        original.edit.assert_not_awaited()
        # Даже если после памятки уже появилось другое сообщение, ищем от сохранённой границы.
        new.posts[1500] = SimpleNamespace(id=1500, embeds=[guide_embed()], author=SimpleNamespace(id=999))
        await self.guide.sync()
        new.send.assert_awaited_once()
        new_post = new.posts[1301]
        self.assertIn(new_post.jump_url, original.embeds[0].description)
        self.assertEqual([(r['channel_id'], r['message_id'], r['status']) for r in self.store.family_guide_posts('2')],
                         [('13', str(new_post.id), 'active')])
        with self.assertRaises(UserError):
            self.store.configure_prices('2', '1', '13')

    async def test_return_to_channel_reactivates_redirect_returned_by_nonce(self):
        first, second = self.channel(12), self.channel(13)
        await self.select(first)
        original, = first.posts.values()
        await self.select(second)
        self.assertIn('Читать памятку', original.embeds[0].description)
        first.posts[1500] = SimpleNamespace(id=1500, author=SimpleNamespace(id=999), embeds=[])
        first.send.side_effect = None
        first.send.return_value = original
        url, moved = await self.select(first)
        self.assertTrue(moved)
        self.assertEqual(url, original.jump_url)
        self.assertEqual(original.embeds, [guide_embed()])
        self.assertEqual(first.send.await_count, 2)
        self.assertIn(original.jump_url, next(iter(second.posts.values())).embeds[0].description)

    async def test_old_channel_is_reserved_until_redirect_is_confirmed(self):
        old, new = self.channel(12), self.channel(13)
        await self.select(old)
        permissions = old.permissions_for.return_value
        old.permissions_for.return_value = discord.Permissions.none()
        with self.assertLogs(level='ERROR'):
            url, moved = await self.select(new)
        self.assertFalse(moved)
        self.assertIn('/13/', url)
        with self.assertRaises(UserError):
            self.store.configure_prices('2', '1', '12')
        old.permissions_for.return_value = permissions
        await self.guide.sync()
        self.store.configure_prices('2', '1', '12')
        self.assertEqual(self.store.price_settings('2')['channel_id'], '12')

    async def test_read_error_or_bad_permissions_never_publish_another_guide(self):
        channel = self.channel(12)
        await self.select(channel)
        channel.fetch_message.side_effect = discord.Forbidden(SimpleNamespace(status=403,reason='denied'), {'code':50001})
        with self.assertRaises(discord.Forbidden):
            await self.select(channel)
        denied = self.channel(13)
        denied.permissions_for.return_value = discord.Permissions.none()
        with self.assertRaises(UserError):
            await self.select(denied)
        self.store.configure_prices('2', '1', '14')
        with self.assertRaises(UserError):
            await self.select(self.channel(14))
        channel.send.assert_awaited_once()
        denied.send.assert_not_awaited()
        self.assertEqual(self.store.family_guide_posts('2')[0]['channel_id'], '12')
