"""Новая структура slash-команд и доступный всем каталог без описаний."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord import app_commands

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.info import build_command_list

GUILD = 111111111111111111
EXPECTED = {
    'баллы история':'all','участник профиль':'all','скупщик история':'all','скупщик проверить-лс':'all',
    'баллы ведомости создать':'admin','баллы ведомости список':'admin','баллы ведомости посмотреть':'admin','баллы ведомости статус':'admin',
    'бот диагностика':'admin','бот операции':'admin','бот восстановить-операцию':'admin',
    'мп запись создать':'organizer','мп запись состав':'all','мп запись закрыть':'manager','мп запись отчёт':'organizer',
    'бот бэкапы настройки':'admin','бот бэкапы создать':'admin','бот бэкапы восстановить':'admin',
    'мп создать':'organizer','мп посмотреть':'all','мп отменить':'manager',
    'мп виды список':'all','мп виды добавить':'manager','мп виды отключить':'manager',
    'баллы мои':'all','баллы рейтинг':'all','баллы общие':'admin','баллы премии':'admin',
    'коровник сбор':'all','коровник настройки':'admin',
    'скупщик подписки':'all','скупщик настройки':'manager','скупщик обработать':'manager','скупщик статистика':'admin',
    'заявки настройки':'manager','ники проверить':'admin',
    'бот настройки':'admin','бот статус':'all','бот обновления':'admin','бот отчёты':'admin',
    'анонс настройки':'admin','анонс шаблон':'admin','анонс разово':'admin',
    'анонс расписание':'admin','анонс список':'admin','анонс отключить':'admin',
    'инфо общее':'all','инфо фарм':'all','инфо админ':'manager',
    'старт участник':'all','старт админ':'admin','помощь':'all','команды':'all',
}


class CommandCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure(str(GUILD),'444444444444444444','555555555555555555','222222222222222222','333333333333333333')
        self.bot=FamilyBot(Config('fake',666666666666666666,GUILD,Path(':memory:')),self.store)
        self.addAsyncCleanup(self.bot.close)

    def find(self,path):
        parts=path.split()
        command=self.bot.tree.get_command(parts[0],guild=discord.Object(id=GUILD))
        for part in parts[1:]:
            command=command.get_command(part)
        return command

    def interaction(self):
        return SimpleNamespace(guild_id=GUILD,guild=SimpleNamespace(get_role=lambda n: None),
            user=SimpleNamespace(id=777777777777777777,roles=[]),permissions=discord.Permissions.none(),
            response=SimpleNamespace(defer=AsyncMock(),autocomplete=AsyncMock(),is_done=lambda:False),
            edit_original_response=AsyncMock())

    async def test_new_paths_and_access_levels_match_approved_schema(self):
        commands=[c for c in self.bot.tree.walk_commands(guild=discord.Object(id=GUILD)) if isinstance(c,app_commands.Command)]
        self.assertEqual({c.qualified_name:c.extras.get('access','all') for c in commands},EXPECTED)
        for c in commands:
            self.assertLessEqual(len(c.qualified_name.split()),3)
        for old in ('статистика','настройки','скупщик-уведомление','коровник-сбор','статус','админ-отчёты'):
            self.assertIsNone(self.bot.tree.get_command(old,guild=discord.Object(id=GUILD)))
        roots=self.bot.tree.get_commands(guild=discord.Object(id=GUILD))
        self.assertTrue(all(c.to_dict(self.bot.tree) for c in roots))

    async def test_catalog_is_complete_grouped_and_has_only_names(self):
        pages=build_command_list(self.bot.tree,GUILD)
        names=[]
        for page in pages:
            self.assertIsNone(page.description)
            self.assertLessEqual(len(page),6000)
            self.assertLessEqual(len(page.fields),25)
            for field in page.fields:
                self.assertLessEqual(len(field.value),1024)
                for line in field.value.splitlines():
                    self.assertRegex(line,r'^`/[^`<>\[\]]+`$')
                    names.append(line[2:-1])
        self.assertCountEqual(names,EXPECTED)
        sections={f.name:f.value for p in pages for f in p.fields}
        self.assertIn('`/скупщик статистика`',sections['Скупщик'])
        self.assertIn('`/бот отчёты`',sections['Бот'])
        self.assertIn('`/мп виды добавить`',sections['Мероприятия'])

    async def test_catalog_is_private_and_available_without_roles_or_database_changes(self):
        request=self.interaction()
        before=self.store.db.total_changes
        await self.find('команды').callback(request)
        request.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
        payload=request.edit_original_response.call_args.kwargs
        self.assertEqual(payload['allowed_mentions'].to_dict(),{'parse':[]})
        view=payload['view']
        if view:
            self.addCleanup(view.stop)
        pages=view.pages if view else [payload['embed']]
        text='\n'.join(f.value for p in pages for f in p.fields)
        self.assertIn('/бот настройки',text)
        self.assertIn('/скупщик статистика',text)
        self.assertEqual(before,self.store.db.total_changes)
        # Наличие команды в каталоге не даёт права её выполнить.
        with self.assertRaises(UserError):
            await self.find('бот отчёты').callback(request)

    async def test_reparented_callbacks_and_autocomplete_keep_their_binding(self):
        kind=self.store.set_type(str(GUILD),'99','Капт',10)
        command=self.find('мп виды добавить')
        self.assertIs(command.binding.service,self.bot.events)
        request=self.interaction()
        request.permissions=discord.Permissions(administrator=True)
        await command._invoke_with_namespace(request,SimpleNamespace(название='Капт',очки=20))
        self.assertEqual(self.store.event_type(str(GUILD),kind['id'])['points'],20)
        await command._invoke_autocomplete(request,'название',SimpleNamespace(название='Кап'))
        choices=request.response.autocomplete.call_args.args[0]
        self.assertEqual([c.name for c in choices],['Капт'])
        report=self.find('мп создать')
        await report._invoke_autocomplete(request,'вид',SimpleNamespace(вид='Кап'))
        self.assertEqual(request.response.autocomplete.call_args.args[0][0].value,str(kind['id']))
        for name in ('баллы общие','баллы премии'):
            self.assertIs(self.find(name).binding.service,self.bot.events)

    async def test_catalog_follows_newly_registered_commands(self):
        @self.bot.tree.command(name='пример',description='Описание, которого не должно быть в каталоге',guild=discord.Object(id=GUILD))
        async def example(interaction:discord.Interaction):
            pass
        pages=build_command_list(self.bot.tree,GUILD)
        text='\n'.join(f.value for p in pages for f in p.fields)
        self.assertIn('`/пример`',text)
        self.assertNotIn(example.description,text)
