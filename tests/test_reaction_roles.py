import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError

GUILD = 111111111111111111
CHANNEL = 222222222222222222
MESSAGE = 333333333333333333
ROLE = 444444444444444444
MEMBER = 555555555555555555
BOT = 666666666666666666
GRANT, REVOKE = '✅', '❌'


class ReactionRoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.bot = FamilyBot(Config('fake', BOT, GUILD, Path(':memory:')), self.store)
        self.addAsyncCleanup(self.bot.close)
        self.member = SimpleNamespace(id=MEMBER, bot=False, add_roles=AsyncMock(), remove_roles=AsyncMock())
        self.message = SimpleNamespace(id=MESSAGE, jump_url='https://discord.com/channels/1/2/3', reactions=[],
                                       add_reaction=AsyncMock(), remove_reaction=AsyncMock(), delete=AsyncMock())
        self.guild = SimpleNamespace(id=GUILD, me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
                                     fetch_member=AsyncMock(return_value=self.member))
        self.channel = SimpleNamespace(id=CHANNEL, guild=self.guild,
            permissions_for=MagicMock(return_value=discord.Permissions(view_channel=True, read_message_history=True,
                add_reactions=True, send_messages=True)),
            send=AsyncMock(return_value=self.message), fetch_message=AsyncMock(return_value=self.message))
        self.role = SimpleNamespace(id=ROLE, guild=self.guild, mention=f'<@&{ROLE}>',
                                    is_assignable=lambda: True, permissions=discord.Permissions.none())
        self.guild.get_role = MagicMock(return_value=self.role)
        self.interaction = SimpleNamespace(guild_id=GUILD, guild=self.guild, user=SimpleNamespace(id=MEMBER),
            permissions=discord.Permissions(administrator=True), response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock())
        self.command = self.bot.tree.get_command('роль', guild=discord.Object(id=GUILD)).get_command('выдача')

    def reaction(self, emoji_name, **changes):
        event = dict(guild_id=GUILD, channel_id=CHANNEL, message_id=MESSAGE, user_id=MEMBER,
                     member=self.member, emoji=discord.PartialEmoji(name=emoji_name))
        return SimpleNamespace(**(event | changes))

    async def test_configured_card_grants_and_removes_role_without_removing_reactions(self):
        await self.command.callback(self.interaction, self.channel, self.role, GRANT)
        self.assertTrue(self.bot.intents.guild_reactions)
        self.assertEqual([str(call.args[0]) for call in self.message.add_reaction.await_args_list], [GRANT])
        self.assertEqual(self.channel.send.call_args.kwargs['allowed_mentions'].to_dict(), {'parse': []})
        with patch.object(FamilyBot, 'user', SimpleNamespace(id=BOT)), patch.object(self.bot, 'get_guild', return_value=self.guild):
            await self.bot.on_raw_reaction_add(self.reaction(GRANT))
            await self.bot.on_raw_reaction_add(self.reaction(REVOKE))
            self.member.remove_roles.assert_not_awaited()
            removal = self.reaction(GRANT)
            del removal.member
            await self.bot.on_raw_reaction_remove(removal)
        self.member.add_roles.assert_awaited_once_with(self.role, reason='Роль по реакции на карточке МП')
        self.member.remove_roles.assert_awaited_once_with(self.role, reason='Роль по реакции на карточке МП')
        self.guild.fetch_member.assert_awaited_once_with(MEMBER)
        self.message.remove_reaction.assert_not_awaited()

    async def test_custom_emoji_uses_id_and_ignores_unrelated_reactions(self):
        emoji_id = 777777777777777777
        await self.command.callback(self.interaction, self.channel, self.role, f'<:join:{emoji_id}>')
        renamed = discord.PartialEmoji(name='renamed', id=emoji_id)
        with patch.object(FamilyBot, 'user', SimpleNamespace(id=BOT)), patch.object(self.bot, 'get_guild', return_value=self.guild):
            await self.bot.on_raw_reaction_add(self.reaction('join', emoji=renamed, message_id=MESSAGE + 1))
            await self.bot.on_raw_reaction_add(self.reaction('join', emoji=renamed, channel_id=CHANNEL + 1))
            await self.bot.on_raw_reaction_add(self.reaction('join', emoji=renamed, guild_id=GUILD + 1))
            await self.bot.on_raw_reaction_add(self.reaction('join', emoji=renamed, user_id=BOT))
            await self.bot.on_raw_reaction_add(self.reaction(GRANT))
            self.member.add_roles.assert_not_awaited()
            await self.bot.on_raw_reaction_add(self.reaction('join', emoji=renamed))
            await self.bot.on_raw_reaction_remove(self.reaction('join', emoji=renamed, member=None))
        self.member.add_roles.assert_awaited_once()
        self.member.remove_roles.assert_awaited_once()

    async def test_setup_rejects_non_admin_and_privileged_role(self):
        self.interaction.permissions = discord.Permissions.none()
        with self.assertRaises(UserError):
            await self.command.callback(self.interaction, self.channel, self.role, GRANT)
        self.interaction.permissions = discord.Permissions(administrator=True)
        with self.assertRaises(UserError):
            await self.command.callback(self.interaction, self.channel, self.role, ' ')
        self.role.permissions.manage_roles = True
        with self.assertRaises(UserError):
            await self.command.callback(self.interaction, self.channel, self.role, GRANT)
        self.channel.send.assert_not_awaited()

    async def test_failed_setup_keeps_old_mapping(self):
        self.store.configure_reaction_role(str(GUILD), str(MEMBER), str(CHANNEL), str(MESSAGE - 1), str(ROLE),
                                           GRANT)
        with patch.object(self.store, 'configure_reaction_role', side_effect=OSError('database failed')):
            with self.assertRaises(OSError):
                await self.command.callback(self.interaction, self.channel, self.role, GRANT)
        self.message.delete.assert_awaited_once()
        self.assertEqual(self.store.reaction_role_settings(str(GUILD))['message_id'], str(MESSAGE - 1))
        with self.assertRaises(UserError):
            self.store.configure_prices(str(GUILD), str(MEMBER), str(CHANNEL))

    async def test_message_id_reuses_existing_message_and_its_bot_reaction(self):
        self.channel.permissions_for.return_value.send_messages = False
        self.message.reactions = [SimpleNamespace(me=True, emoji=GRANT)]
        await self.command.callback(self.interaction, self.channel, self.role, GRANT, str(MESSAGE))
        self.channel.fetch_message.assert_awaited_once_with(MESSAGE)
        self.channel.send.assert_not_awaited()
        self.message.add_reaction.assert_not_awaited()
        self.assertEqual(self.store.reaction_role_settings(str(GUILD))['message_id'], str(MESSAGE))

    async def test_failed_binding_leaves_existing_message_and_removes_only_new_reaction(self):
        bot_user = SimpleNamespace(id=BOT)
        with patch.object(FamilyBot, 'user', bot_user), patch.object(self.store, 'configure_reaction_role',
                side_effect=OSError('database failed')), self.assertRaises(OSError):
            await self.command.callback(self.interaction, self.channel, self.role, GRANT, str(MESSAGE))
        self.message.delete.assert_not_awaited()
        self.message.remove_reaction.assert_awaited_once()
        emoji, user = self.message.remove_reaction.call_args.args
        self.assertEqual((str(emoji), user), (GRANT, bot_user))
        self.assertIsNone(self.store.reaction_role_settings(str(GUILD)))

    async def test_old_card_is_upgraded_and_old_cross_is_ignored(self):
        self.store.configure_reaction_role(str(GUILD), str(MEMBER), str(CHANNEL), str(MESSAGE), str(ROLE), GRANT)
        self.store.db.execute('UPDATE reaction_role_settings SET revoke_emoji=?', (REVOKE,))
        self.message.author = SimpleNamespace(id=BOT)
        self.message.content = f'**Роль <@&{ROLE}>**\n{GRANT} Получить роль\n{REVOKE} Снять роль'
        self.message.edit = AsyncMock()
        bot_user = SimpleNamespace(id=BOT)
        with patch.object(FamilyBot, 'user', bot_user), patch.object(self.bot, 'fetch_channel', return_value=self.channel):
            await self.bot.reaction_roles.upgrade_card()
            await self.bot.reaction_roles.upgrade_card()
        self.assertNotIn(REVOKE, self.message.edit.call_args.kwargs['content'])
        self.message.remove_reaction.assert_awaited_once()
        emoji, user = self.message.remove_reaction.call_args.args
        self.assertEqual((str(emoji), user), (REVOKE, bot_user))
        self.assertEqual(self.store.reaction_role_settings(str(GUILD))['revoke_emoji'], '')
        with patch.object(FamilyBot, 'user', bot_user), patch.object(self.bot, 'get_guild', return_value=self.guild):
            await self.bot.on_raw_reaction_add(self.reaction(REVOKE))
            await self.bot.on_raw_reaction_remove(self.reaction(REVOKE, member=None))
            await self.bot.on_raw_reaction_remove(self.reaction(GRANT, member=None))
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_awaited_once()
