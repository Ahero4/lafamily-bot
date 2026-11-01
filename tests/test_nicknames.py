"""Форматы ников, полный обход сервера, права и отсутствие частичного отчёта при ошибке."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from lafamily.bot import FamilyBot
from lafamily.config import Config, load_config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.nicknames import valid_nickname, audit_members

GUILD=111111111111111111
ROLE=888888888888888888

def member(name,uid=222222222222222222,bot=False,roles=(ROLE,)):
    return SimpleNamespace(display_name=name,id=uid,bot=bot,roles=[SimpleNamespace(id=r) for r in roles])

async def members(values):
    for value in values: yield value

class PatternTests(unittest.TestCase):
    def test_valid_name_with_and_without_position(self):
        for value in ('Sin Bertoni | 15096','Зам. | Sin Bertoni | 15096','Глава | John Smith | 1',
                      "Зам. по МП | Jean-Luc O'Connor | 123",'sin bertoni|15096'):
            with self.subTest(value=value): self.assertTrue(valid_nickname(value))

    def test_invalid_missing_or_extra_parts_and_non_latin_names(self):
        for value in ('Sin Bertoni','Sin | 15096','Sin Bertoni | abc','Sin Bertoni | 0',
                      '| Sin Bertoni | 15096','Зам. | | Sin Bertoni | 15096','Син Бертони | 15096',
                      'Sin Bertoni | 15096 | extra','Sin Bertoni | 15096\n',''):
            with self.subTest(value=value): self.assertFalse(valid_nickname(value))

    def test_intent_config_is_explicit_and_validated(self):
        env={'DISCORD_TOKEN':'fake','DISCORD_CLIENT_ID':'123456789012345678','DISCORD_GUILD_ID':str(GUILD)}
        with patch('lafamily.config.load_dotenv'),patch.dict('os.environ',env,clear=True):
            self.assertFalse(load_config().members_intent)
            with patch.dict('os.environ',{'DISCORD_MEMBERS_INTENT':'true'}): self.assertTrue(load_config().members_intent)
            with patch.dict('os.environ',{'DISCORD_MEMBERS_INTENT':'maybe'}):
                with self.assertRaises(UserError): load_config()

class NicknameCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests=[]
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.bot=FamilyBot(Config('fake',123456789012345678,GUILD,Path(':memory:'),members_intent=True),self.store)
        self.addAsyncCleanup(self.bot.close)
        self.role=SimpleNamespace(id=ROLE,mention=f"<@&{ROLE}>")
        self.command=self.bot.tree.get_command('проверка-ника',guild=discord.Object(id=GUILD))

    def request(self,values=(),admin=True):
        request=SimpleNamespace(channel_id=999999999999999999,guild_id=GUILD,guild=SimpleNamespace(fetch_members=MagicMock(side_effect=lambda **kw:members(values)),filesize_limit=8*1024*1024),
            client=self.bot,user=SimpleNamespace(id=123456789012345678),permissions=discord.Permissions(administrator=admin),
            response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())
        self.requests.append(request)
        return request

    async def asyncTearDown(self):
        for request in self.requests:
            if request.edit_original_response.call_args:
                view=request.edit_original_response.call_args.kwargs.get('view')
                if view:
                    view.finish()

    async def test_scan_fetches_every_member_not_cache_and_skips_bots(self):
        request=self.request([member('Sin Bertoni | 15096'),member('Wrong',3),member('Bot',4,True)])
        result=await audit_members(request.guild, ROLE)
        request.guild.fetch_members.assert_called_once_with(limit=None)
        self.assertEqual(result.checked,2);self.assertEqual(result.skipped_bots,1)
        self.assertEqual(result.invalid,[(3,'Wrong')])

    async def test_non_admin_and_disabled_intent_are_rejected(self):
        request=self.request(admin=False)
        with self.assertRaises(UserError): await self.command.callback(request,self.role)
        request.guild.fetch_members.assert_not_called()
        disabled=self.request()
        disabled.client=SimpleNamespace(intents=discord.Intents.none())
        with self.assertRaises(UserError): await self.command.callback(disabled,self.role)

    async def test_output_is_private_with_no_pings_and_file_contains_all_violations(self):
        request=self.request([member('Wrong Name',300000000000000000+i) for i in range(20)])
        await self.command.callback(request,self.role)
        self.assertTrue(request.response.defer.call_args.kwargs['ephemeral'])
        payload=request.edit_original_response.call_args.kwargs
        self.assertEqual(payload['allowed_mentions'].to_dict(),{'parse':[]})
        self.assertEqual(len(payload['attachments']),1)
        self.assertIsNotNone(payload['view'])
        self.assertIn('Уведомить участников',payload['content'])
        raw=payload['attachments'][0].fp.read().decode('utf-8')
        self.assertEqual(len(raw.splitlines()),21)
        self.assertLessEqual(len(payload['embed']),6000)
        self.assertTrue(all(len(f.value)<=1024 for f in payload['embed'].fields))

    async def test_permission_failure_does_not_produce_partial_success_and_retry_is_possible(self):
        request=self.request()
        async def broken(**kwargs):
            yield member('Wrong')
            raise discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'denied')
        request.guild.fetch_members.side_effect=broken
        with self.assertRaises(UserError): await self.command.callback(request,self.role)
        request.edit_original_response.assert_not_awaited()
        retry=self.request([member('Зам. | Sin Bertoni | 15096')])
        await self.command.callback(retry,self.role)
        self.assertIn('Нарушений: **0**',retry.edit_original_response.call_args.kwargs['embed'].description)
        self.assertIsNone(retry.edit_original_response.call_args.kwargs['view'])

    async def test_registered_command_is_admin_and_intent_enabled_without_login(self):
        self.assertTrue(self.bot.intents.members)
        data=self.command.to_dict(self.bot.tree)
        self.assertEqual(data['name'],'проверка-ника')
        self.assertEqual(data['default_member_permissions'],discord.Permissions(administrator=True).value)
        self.assertEqual(self.command.extras['access'],'admin')
        self.assertEqual(data['options'][0]['name'],'роль')
        self.assertEqual(data['options'][0]['type'],discord.AppCommandOptionType.role.value)
        self.assertFalse(data['options'][0]['required'])

    async def test_only_selected_role_is_checked_even_with_multiple_roles(self):
        request=self.request([
            member('Wrong selected',1,roles=(999,ROLE)),
            member('Sin Bertoni | 15096',2),
            member('Wrong outside',3,roles=(999,)),
            member('No roles',4,roles=()),
            member('Selected bot',5,bot=True),
            member('Outside bot',6,bot=True,roles=(999,)),
        ])
        result=await audit_members(request.guild, ROLE)
        self.assertEqual(result.checked,2)
        self.assertEqual(result.invalid,[(1,'Wrong selected')])
        self.assertEqual(result.skipped_bots,1)
        await self.command.callback(request,self.role)
        payload=request.edit_original_response.call_args.kwargs
        self.assertIn(self.role.mention,payload['embed'].description)
        self.assertNotIn('Wrong outside',str(payload['embed'].to_dict()))

    async def test_empty_role_has_explicit_message(self):
        request=self.request([member('Wrong outside',roles=(999,))])
        await self.command.callback(request,self.role)
        embed=request.edit_original_response.call_args.kwargs['embed']
        self.assertIn('Проверено участников: **0**',embed.description)
        self.assertIn('не найдено',embed.fields[0].value)

    async def test_no_role_checks_all_humans_and_skips_bots(self):
        request=self.request([
            member('Sin Bertoni | 15096',1),
            member('Wrong outside',2,roles=(999,)),
            member('No roles',3,roles=()),
            member('Bot',4,bot=True,roles=()),
        ])
        result=await audit_members(request.guild)
        self.assertEqual(result.checked,3)
        self.assertEqual(result.skipped_bots,1)
        self.assertEqual(result.invalid,[(2,'Wrong outside'),(3,'No roles')])
        await self.command.callback(request)
        payload=request.edit_original_response.call_args.kwargs
        self.assertIn('все участники сервера',payload['embed'].description)
        self.assertIn('Проверено участников: **3**',payload['embed'].description)
