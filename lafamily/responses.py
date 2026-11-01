"""Общая обработка ошибок команд и кнопок без раскрытия токенов и внутренних запросов."""

import logging

import discord
from discord import app_commands

from lafamily.errors import UserError
from lafamily.diagnostics import log_error

logger = logging.getLogger(__name__)


async def send_error(interaction: discord.Interaction, error: Exception):
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, UserError):
        content = str(error)
    elif isinstance(error, app_commands.AppCommandError):
        content = "Проверьте параметры и доступ к команде. Справка: /помощь."
    else:
        # Полный Discord HTTP-запрос может содержать секреты, поэтому его не печатаем.
        log_error("Ошибка действия", error)
        content = "Не удалось выполнить действие. Перед повторной подачей проверьте сохранённый отчёт через /мп просмотр."
    try:
        if interaction.response.type == discord.InteractionResponseType.deferred_channel_message:
            await interaction.edit_original_response(content=content)
        elif interaction.response.is_done():
            # Ошибка кнопки не должна удалять состав и кнопки действующего черновика.
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.HTTPException:
        logger.warning("Не удалось отправить уведомление об ошибке.")
