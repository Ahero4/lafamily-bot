"""Проверки справки для разных ролей без подключения к Discord."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord import app_commands

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.info import build_info, build_farm_info

GUILD = 111111111111111111
ORGANIZER = 222222222222222222
MANAGER = 333333333333333333


class InfoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.configure(str(GUILD), "444444444444444444", "555555555555555555", str(ORGANIZER), str(MANAGER))
        self.bot = FamilyBot(Config("fake", 666666666666666666, GUILD, Path(":memory:")), self.store)
        self.addAsyncCleanup(self.bot.close)

    def interaction(self, roles=(), admin=False):
        return SimpleNamespace(
            guild_id=GUILD,
            guild=SimpleNamespace(get_role=lambda role_id: SimpleNamespace(mention=f"<@&{role_id}>")),
            user=SimpleNamespace(roles=[SimpleNamespace(id=role_id) for role_id in roles]),
            permissions=discord.Permissions(administrator=admin),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()), edit_original_response=AsyncMock(),
        )

    def build(self, interaction):
        return build_info(interaction, self.bot.tree, self.store)

    async def test_newcomer_sees_every_command_with_description_and_role_mapping(self):
        embed = self.build(self.interaction())
        text = "\n".join(field.value for field in embed.fields)
        commands = [command for command in self.bot.tree.walk_commands(guild=discord.Object(id=GUILD))
                    if isinstance(command, app_commands.Command)]
        self.assertEqual(len(commands), 23)
        for command in commands:
            self.assertIn(f"/{command.qualified_name}", text)
            self.assertIn(command.description, text)
        self.assertIn(f"<@&{ORGANIZER}>", text)
        self.assertIn(f"<@&{MANAGER}>", text)
        self.assertIn("<номер>", text)
        self.assertIn("[неделя]", text)
        self.assertIn("**участник**", embed.description)
        admin_field = next(field for field in embed.fields if "Администратор" in field.name)
        self.assertIn("/мп статистика", admin_field.value)
        self.assertTrue(admin_field.name.startswith("🔒"))
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    async def test_availability_matches_member_organizer_manager_and_admin(self):
        scenarios = [
            ((), False, [True, False, False, False]),
            ((ORGANIZER,), False, [True, True, False, False]),
            ((MANAGER,), False, [True, True, True, False]),
            ((ORGANIZER, MANAGER), False, [True, True, True, False]),
            ((), True, [True, True, True, True]),
        ]
        for roles, admin, expected in scenarios:
            with self.subTest(roles=roles, admin=admin):
                fields = self.build(self.interaction(roles, admin)).fields[:4]
                self.assertEqual([field.name.startswith("✅") for field in fields], expected)

    async def test_unconfigured_and_deleted_roles_have_clear_explanations(self):
        self.store.db.execute("DELETE FROM settings")
        embed = self.build(self.interaction())
        self.assertIn("Первый запуск", [field.name for field in embed.fields])
        self.assertIn("роль не назначена", " ".join(field.value for field in embed.fields))
        self.store.configure(str(GUILD), "444444444444444444", "555555555555555555", str(ORGANIZER), str(MANAGER))
        interaction = self.interaction()
        interaction.guild.get_role = lambda role_id: None
        self.assertIn("роль удалена или недоступна", " ".join(field.value for field in self.build(interaction).fields))

    async def test_info_and_help_send_private_response_without_role_notifications(self):
        for name in ("инфо", "помощь"):
            interaction = self.interaction()
            command = self.bot.tree.get_command(name, guild=discord.Object(id=GUILD))
            if isinstance(command, app_commands.Group):
                command = command.get_command("общее")
            await command.callback(interaction)
            payload = interaction.edit_original_response.call_args.kwargs
            self.assertTrue(interaction.response.defer.call_args.kwargs["ephemeral"])
            self.assertEqual(payload["allowed_mentions"].to_dict(), {"parse": []})
            self.assertEqual(payload["embed"].title, "LaFamily • команды и права")

    async def test_description_changes_are_reflected_without_editing_info(self):
        command = self.bot.tree.get_command("инфо", guild=discord.Object(id=GUILD))
        command = command.get_command("общее")
        command.description = "Обновлённое описание справки"
        self.assertIn(command.description, " ".join(field.value for field in self.build(self.interaction()).fields))


    async def test_farm_help_is_private_and_contains_current_subscription_instructions(self):
        self.store.configure_prices(str(GUILD), "123", "777777777777777777")
        group = self.bot.tree.get_command("инфо", guild=discord.Object(id=GUILD))
        self.assertEqual({command.name for command in group.commands}, {"общее", "фарм"})
        interaction = self.interaction()
        await group.get_command("фарм").callback(interaction)
        payload = interaction.edit_original_response.call_args.kwargs
        self.assertTrue(interaction.response.defer.call_args.kwargs["ephemeral"])
        self.assertEqual(payload["allowed_mentions"].to_dict(), {"parse": []})
        embed = payload['embed']
        self.assertIn('<#777777777777777777>', embed.description)
        text = '\n'.join(field.value for field in embed.fields)
        for word in ('/скупщик-уведомление', 'Подтвердить', 'Отмена', '95–100%', '90–95%', 'Ручная обработка не тегает'):
            self.assertIn(word, text)
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertTrue(all(not field.inline for field in embed.fields))

    async def test_farm_help_without_settings_and_general_navigation(self):
        self.assertIn('канал ещё не выбран', build_farm_info().description)
        self.assertIn('/инфо фарм', self.build(self.interaction()).description)


    async def test_status_is_public_and_available_to_members_without_mentions(self):
        interaction = self.interaction()
        command = self.bot.tree.get_command("статус", guild=discord.Object(id=GUILD))
        self.assertEqual(command.extras['access'], 'all')
        await command.callback(interaction)
        call = interaction.response.send_message.call_args
        self.assertIn('запущен и готов к работе', call.args[0])
        self.assertFalse(call.kwargs.get('ephemeral', False))
        self.assertEqual(call.kwargs['allowed_mentions'].to_dict(), {'parse': []})


    async def test_start_is_short_private_and_has_first_steps(self):
        interaction = self.interaction()
        command = self.bot.tree.get_command("старт", guild=discord.Object(id=GUILD))
        self.assertEqual(command.extras['access'], 'all')
        await command.callback(interaction)
        payload = interaction.response.send_message.call_args.kwargs
        self.assertTrue(payload['ephemeral'])
        self.assertEqual(payload['allowed_mentions'].to_dict(), {'parse': []})
        embed = payload['embed']
        self.assertLess(len(embed), 2000)
        self.assertEqual(len(embed.fields), 5)
        text = '\n'.join(field.value for field in embed.fields)
        for name in ('/статистика моя', '/коровник-сбор', '/скупщик-уведомление', 'Подтвердить', '/инфо фарм'):
            self.assertIn(name, text)
