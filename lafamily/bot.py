"""Подключение Discord отделено от функциональных модулей семьи."""

import logging
import sys

import discord
from discord import app_commands

from lafamily.config import Config
from lafamily.errors import UserError
from lafamily.database import Store
from lafamily.modules.events.commands import install_commands
from lafamily.modules.events.service import EventService
from lafamily.responses import send_error
from lafamily.diagnostics import log_error
from lafamily.modules.nicknames import register_nickname_command
from lafamily.modules.prices.service import PriceService
from lafamily.modules.prices.commands import register_price_commands
from lafamily.modules.barn import register_barn_commands
from lafamily.modules.onboarding import OnboardingService, EntryView, ReviewView, register_onboarding

logger = logging.getLogger(__name__)


class FamilyTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.client.config.guild_id:
            await interaction.response.send_message("Команда доступна только на сервере семьи.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await send_error(interaction, error)


class FamilyBot(discord.Client):
    def __init__(self, config: Config, store: Store):
        # Чтение сообщений для цен и список участников включаются отдельными флагами .env.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True  # Состав голосового канала для отчётов МП.
        intents.guild_messages = True
        intents.message_content = config.message_content_intent
        intents.members = config.members_intent
        super().__init__(intents=intents, application_id=config.client_id,
                         allowed_mentions=discord.AllowedMentions.none(), proxy=config.proxy, chunk_guilds_at_startup=False)
        self.config = config
        self.store = store
        self.events = EventService(store)
        self.tree = FamilyTree(self, fallback_to_global=False)
        install_commands(self.tree, self.events, config.guild_id)
        register_barn_commands(self.tree, store, config.guild_id)
        self.onboarding = OnboardingService(store)
        register_onboarding(self.tree, self.onboarding, config.guild_id)
        register_nickname_command(self.tree, store, config.guild_id)
        self.prices = PriceService(self, store, config.guild_id)
        register_price_commands(self.tree, self.prices, config.guild_id)

    async def setup_hook(self):
        # Постоянные кнопки продолжают работать после перезапуска.
        self.add_view(EntryView(self.onboarding))
        self.add_view(ReviewView(self.onboarding))
        # setup_hook вызывается после авторизации, до Gateway и не повторяется при reconnect.
        # При сбое регистрации прекращаем запуск, чтобы не обслуживать устаревшую схему.
        try:
            commands = await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
        except Exception as error:
            raise UserError("Не удалось зарегистрировать команды. Проверьте доступ к Discord, "
                            "DISCORD_GUILD_ID и права приложения; затем перезапустите бота.") from error
        logger.info("Команды LaFamily зарегистрированы на сервере %s: %s", self.config.guild_id, len(commands))

    async def on_message(self, message):
        await self.prices.handle(message)

    async def on_raw_message_edit(self, payload):
        await self.prices.handle_edit(payload)

    async def on_ready(self):
        logger.info("LaFamily запущен: %s", self.user)

    async def on_error(self, event_method: str, *args, **kwargs):
        # Не выводим аргументы событий: они могут содержать приватные данные.
        error = sys.exception()
        if error is not None:
            log_error(f"Необработанная ошибка события {event_method}", error)

    async def close(self):
        self.events.close()
        await super().close()
