"""Личное подтверждение с повторной проверкой прав непосредственно перед действием."""
import asyncio

import discord

from lafamily.permissions import require_access_async
from lafamily.responses import send_error
from lafamily.errors import UserError


class ConfirmAction(discord.ui.View):
    def __init__(self, store, interaction, access, action):
        super().__init__(timeout=180)
        self.store, self.owner, self.guild = store, interaction.user.id, interaction.guild_id
        self.access, self.action, self.lock = access, action, asyncio.Lock()
        self.finished = False

    async def on_error(self, interaction, error, item):
        await send_error(interaction,error)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner or interaction.guild_id != self.guild:
            await send_error(interaction,UserError('Это подтверждение другого пользователя.'))
            return False
        await require_access_async(interaction,self.store,self.access)
        return True

    @discord.ui.button(label='Подтвердить',style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        async with self.lock:
            if interaction.user.id!=self.owner or interaction.guild_id!=self.guild:
                raise UserError('Это подтверждение другого пользователя.')
            if self.finished or self.is_finished():
                raise UserError('Подтверждение уже завершено или истекло.')
            await interaction.response.defer(ephemeral=True)
            await require_access_async(interaction,self.store,self.access)
            content = await self.action(interaction)
            self.finished = True
            self.stop()
            await interaction.edit_original_response(content=content,view=None,embeds=[],allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='Отмена',style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        async with self.lock:
            if interaction.user.id!=self.owner or interaction.guild_id!=self.guild:
                raise UserError('Это подтверждение другого пользователя.')
            if self.finished or self.is_finished():
                raise UserError('Подтверждение уже завершено или истекло.')
            self.finished = True
            self.stop()
            await interaction.response.edit_message(content='Действие отменено.',view=None,embeds=[])
