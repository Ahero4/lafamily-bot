"""Проверки разделения справки, актуальности команд, личных страниц и стартовых инструкций."""
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
from lafamily.info import (HelpPages, available, build_info, build_info_pages, build_admin_info_pages,
                           build_farm_info)
from lafamily.info_guides import COMMAND_GUIDES

GUILD = 111111111111111111
ORGANIZER = 222222222222222222
MANAGER = 333333333333333333


def page_text(pages):
    return '\n'.join((p.description or '')+'\n'+'\n'.join(f.name+'\n'+f.value for f in p.fields) for p in pages)


class InfoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure(str(GUILD),'444444444444444444','555555555555555555',str(ORGANIZER),str(MANAGER))
        self.bot = FamilyBot(Config('fake',666666666666666666,GUILD,Path(':memory:')),self.store)
        self.addAsyncCleanup(self.bot.close)

    def interaction(self, roles=(), admin=False):
        return SimpleNamespace(guild_id=GUILD,
            guild=SimpleNamespace(get_role=lambda role_id: SimpleNamespace(mention=f'<@&{role_id}>')),
            user=SimpleNamespace(id=777777777777777777,roles=[SimpleNamespace(id=r) for r in roles]),
            permissions=discord.Permissions(administrator=admin),
            response=SimpleNamespace(send_message=AsyncMock(),defer=AsyncMock(),edit_message=AsyncMock(),
                                     is_done=lambda: False,type=None),edit_original_response=AsyncMock())

    def build(self, interaction):
        return build_info(interaction,self.bot.tree,self.store)

    def check_limits(self,pages):
        for page in pages:
            self.assertLessEqual(len(page),6000)
            self.assertLessEqual(len(page.fields),25)
            self.assertTrue(all(len(f.name)<=256 and len(f.value)<=1024 and not f.inline for f in page.fields))

    async def test_all_registered_commands_have_guides_in_the_correct_section(self):
        interaction = self.interaction()
        general = build_info_pages(interaction,self.bot.tree,self.store)
        admin = build_admin_info_pages(interaction,self.bot.tree,self.store)
        general_text,admin_text = page_text(general),page_text(admin)
        commands = [c for c in self.bot.tree.walk_commands(guild=discord.Object(id=GUILD))
                    if isinstance(c,app_commands.Command)]
        self.assertEqual(set(COMMAND_GUIDES),{c.qualified_name for c in commands})
        for command in commands:
            administrative = command.extras.get('access','all') in ('manager','admin')
            text = admin_text if administrative else general_text
            self.assertIn('/'+command.qualified_name,text)
            self.assertIn(command.description,text)
            self.assertIn(COMMAND_GUIDES[command.qualified_name],text)
            if administrative and command.qualified_name != 'инфо админам':
                self.assertNotIn('/'+command.qualified_name,general_text)
                self.assertNotIn(COMMAND_GUIDES[command.qualified_name],general_text)
        self.assertIn(f'<@&{ORGANIZER}>',general_text)
        self.assertIn(f'<@&{MANAGER}>',general_text)
        self.assertIn('<номер>',general_text)
        self.assertIn('[неделя]',general_text)
        self.assertIn('**участник**',general[0].description)
        self.check_limits(general+admin)

    async def test_availability_and_badges_match_real_permissions(self):
        for roles,admin,expected in [
            ((),False,[True,False,False,False]),
            ((ORGANIZER,),False,[True,True,False,False]),
            ((MANAGER,),False,[True,True,True,False]),
            ((ORGANIZER,MANAGER),False,[True,True,True,False]),
            ((),True,[True,True,True,True])]:
            with self.subTest(roles=roles,admin=admin):
                interaction=self.interaction(roles,admin)
                self.assertEqual([available(interaction,self.store,l) for l in ('all','organizer','manager','admin')],expected)
                fields=[f for p in build_info_pages(interaction,self.bot.tree,self.store)+build_admin_info_pages(interaction,self.bot.tree,self.store) for f in p.fields]
                for name,allowed in zip(('мп виды','мп отчёт','мп отменить','админ-отчёты'),expected):
                    field=next(f for f in fields if f.name.endswith('/'+name))
                    self.assertEqual(field.name.startswith('✅'),allowed)

    async def test_unconfigured_and_deleted_roles_have_clear_explanations(self):
        self.store.db.execute('DELETE FROM settings')
        embed=self.build(self.interaction())
        self.assertIn('Первый запуск',[f.name for f in embed.fields])
        self.assertIn('роль не назначена',' '.join(f.value for f in embed.fields))
        self.store.configure(str(GUILD),'444444444444444444','555555555555555555',str(ORGANIZER),str(MANAGER))
        interaction=self.interaction()
        interaction.guild.get_role=lambda role_id: None
        self.assertIn('роль удалена или недоступна',' '.join(f.value for f in self.build(interaction).fields))

    async def test_info_and_help_are_private_without_administrative_instructions(self):
        for name in ('инфо','помощь'):
            interaction=self.interaction(admin=True)
            command=self.bot.tree.get_command(name,guild=discord.Object(id=GUILD))
            if isinstance(command,app_commands.Group):
                command=command.get_command('общее')
            await command.callback(interaction)
            payload=interaction.edit_original_response.call_args.kwargs
            self.assertTrue(interaction.response.defer.call_args.kwargs['ephemeral'])
            self.assertEqual(payload['allowed_mentions'].to_dict(),{'parse':[]})
            self.assertEqual(payload['embed'].title,'LaFamily • команды и права')
            view=payload['view']
            if view:
                self.addCleanup(view.stop)
            text=page_text(view.pages if view else [payload['embed']])
            self.assertNotIn('/админ-отчёты',text)
            self.assertNotIn('/анонс шаблон',text)
            self.assertIn('/инфо админам',text)

    async def test_admin_info_requires_management_access_and_does_not_change_database(self):
        command=self.bot.tree.get_command('инфо',guild=discord.Object(id=GUILD)).get_command('админам')
        self.assertEqual(command.extras['access'],'manager')
        before=self.store.db.total_changes
        for roles in ((),(ORGANIZER,)):
            interaction=self.interaction(roles)
            with self.assertRaises(UserError):
                await command.callback(interaction)
            interaction.edit_original_response.assert_not_awaited()
        for roles,admin in (((MANAGER,),False),((),True)):
            interaction=self.interaction(roles,admin)
            await command.callback(interaction)
            payload=interaction.edit_original_response.call_args.kwargs
            self.assertTrue(interaction.response.defer.call_args.kwargs['ephemeral'])
            self.assertEqual(payload['allowed_mentions'].to_dict(),{'parse':[]})
            view=payload['view']
            if view:
                self.addCleanup(view.stop)
            pages=view.pages if view else [payload['embed']]
            text=page_text(pages)
            for word in ('/анонс расписание','/скупщик-статистика','/админ-отчёты','50 000','CSV','за 15 минут','только администратор Discord'):
                self.assertIn(word,text)
            self.check_limits(pages)
        self.assertEqual(before,self.store.db.total_changes)

    async def test_descriptions_and_parameter_names_are_read_from_registered_commands(self):
        command=self.bot.tree.get_command('инфо',guild=discord.Object(id=GUILD)).get_command('общее')
        command.description='Обновлённое описание справки'
        self.assertIn(command.description,page_text(build_info_pages(self.interaction(),self.bot.tree,self.store)))

    async def test_farm_help_is_private_and_explains_current_subscription_steps(self):
        self.store.configure_prices(str(GUILD),'123','777777777777777777')
        group=self.bot.tree.get_command('инфо',guild=discord.Object(id=GUILD))
        self.assertEqual({c.name for c in group.commands},{'общее','фарм','админам'})
        interaction=self.interaction()
        await group.get_command('фарм').callback(interaction)
        payload=interaction.edit_original_response.call_args.kwargs
        self.assertTrue(interaction.response.defer.call_args.kwargs['ephemeral'])
        self.assertEqual(payload['allowed_mentions'].to_dict(),{'parse':[]})
        embed=payload['embed']
        self.assertIn('<#777777777777777777>',embed.description)
        text=page_text([embed])
        for word in ('/скупщик-уведомление','Подтвердить','Отмена','95–100%','90–95%',
                     'Ручная обработка не уведомляет','ЛС','выше 100%','втором списке','третьем списке',
                     '/коровник-сбор','/инфо админам'):
            self.assertIn(word,text)
        for name in ('/скупщик-настройка','/админ-отчёты','/скупщик-статистика'):
            self.assertNotIn(name,text)
        self.check_limits([embed])

    async def test_farm_help_without_settings_and_general_navigation(self):
        self.assertIn('канал ещё не выбран',build_farm_info().description)
        self.assertIn('/инфо фарм',self.build(self.interaction()).description)

    async def test_help_page_navigation_is_private_and_rolls_back_failed_edit(self):
        original=self.interaction()
        pages=[discord.Embed(title=str(i)) for i in range(3)]
        view=HelpPages(original,pages)
        self.addCleanup(view.stop)
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next_page.disabled)
        click=self.interaction()
        await view.next_page.callback(click)
        self.assertEqual(view.index,1)
        self.assertEqual(view.counter.label,'2 / 3')
        self.assertEqual(click.response.edit_message.call_args.kwargs['allowed_mentions'].to_dict(),{'parse':[]})
        await view.move(click,1)
        self.assertTrue(view.next_page.disabled)
        await view.move(click,1)
        self.assertEqual(view.index,2)
        click.response.edit_message.side_effect=OSError('network')
        with self.assertRaises(OSError):
            await view.previous.callback(click)
        self.assertEqual(view.index,2)
        self.assertTrue(view.next_page.disabled)
        click.response.edit_message.side_effect=None
        await view.previous.callback(click)
        self.assertEqual(view.index,1)
        foreign=self.interaction()
        foreign.user.id=123
        self.assertFalse(await view.interaction_check(foreign))
        self.assertTrue(foreign.response.send_message.call_args.kwargs['ephemeral'])
        foreign.response.edit_message.assert_not_awaited()
        await view.on_timeout()
        self.assertTrue(all(c.disabled for c in view.children))
        with self.assertRaises(UserError):
            await view.move(click,1)


    async def test_status_is_public_and_available_to_members_without_mentions(self):
        interaction = self.interaction()
        command = self.bot.tree.get_command("статус", guild=discord.Object(id=GUILD))
        self.assertEqual(command.extras['access'], 'all')
        await command.callback(interaction)
        call = interaction.response.send_message.call_args
        self.assertIn('запущен и готов к работе', call.args[0])
        from lafamily.release import VERSION, LATEST_UPDATE
        from pathlib import Path
        import tomllib
        metadata = tomllib.loads((Path(__file__).resolve().parent.parent / 'pyproject.toml').read_text(encoding='utf-8'))
        self.assertEqual(VERSION, metadata['project']['version'])
        self.assertIn(f'**Версия:** {VERSION}', call.args[0])
        self.assertIn(f'**Последнее обновление:** {LATEST_UPDATE}', call.args[0])
        self.assertLessEqual(len(call.args[0]), 2000)
        self.assertFalse(call.kwargs.get('ephemeral', False))
        self.assertEqual(call.kwargs['allowed_mentions'].to_dict(), {'parse': []})


    async def test_start_is_short_private_and_has_first_steps(self):
        interaction = self.interaction()
        command = self.bot.tree.get_command("старт", guild=discord.Object(id=GUILD)).get_command("участник")
        self.assertEqual(command.extras['access'], 'all')
        await command.callback(interaction)
        payload = interaction.response.send_message.call_args.kwargs
        self.assertTrue(payload['ephemeral'])
        self.assertEqual(payload['allowed_mentions'].to_dict(), {'parse': []})
        embed = payload['embed']
        self.assertLess(len(embed), 2000)
        self.assertEqual(len(embed.fields), 5)
        text = '\n'.join(field.value for field in embed.fields)
        for name in ('/статистика моя', '/коровник-сбор', '/скупщик-уведомление', 'Подтвердить', '/инфо фарм', '/инфо админам'):
            self.assertIn(name, text)

    async def test_admin_start_is_private_readonly_and_covers_setup_within_discord_limits(self):
        command=self.bot.tree.get_command('старт',guild=discord.Object(id=GUILD)).get_command('администратор')
        self.assertEqual(command.extras['access'],'admin')
        before=self.store.db.total_changes
        for roles in ((),(ORGANIZER,),(MANAGER,)):
            interaction=self.interaction(roles)
            from lafamily.errors import UserError
            with self.assertRaises(UserError):await command.callback(interaction)
            interaction.edit_original_response.assert_not_awaited()
        interaction=self.interaction((),True)
        await command.callback(interaction)
        self.assertTrue(interaction.response.defer.call_args.kwargs['ephemeral'])
        payload=interaction.edit_original_response.call_args.kwargs
        self.assertEqual(payload['allowed_mentions'].to_dict(),{'parse':[]})
        embeds=payload['embeds']
        self.assertEqual(len(embeds),2)
        self.assertLessEqual(sum(len(e) for e in embeds),6000)
        text='\n'.join(e.description or '' for e in embeds)+'\n'+'\n'.join(field.value for e in embeds for field in e.fields)
        for name in ('/настройки','Сохранить','/мп вид-добавить','/коровник-настройка','/скупщик-настройка',
                     '/скупщик-уведомление','/модерация-настройки','/проверка-ника','/обновления-канал',
                     '/мп отчёт','/мп отменить','/мп выплата-премии','main.py','база сохранена','/инфо админам'):
            self.assertIn(name,text)
        self.assertIn('[Ahero4/lafamily-bot](https://github.com/Ahero4/lafamily-bot)', text)
        for embed in embeds:
            self.assertLessEqual(len(embed.fields),25)
            self.assertTrue(all(len(field.value)<=1024 for field in embed.fields))
        self.assertEqual(before,self.store.db.total_changes)
