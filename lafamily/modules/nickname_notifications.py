"""Уведомление нарушителей формата ника только после отдельного подтверждения."""
import asyncio
import time
from datetime import UTC, datetime

import discord

from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from lafamily.responses import send_error

BATCH_SIZE = 20


class NicknameNoticeView(discord.ui.View):
    def __init__(self, store, original, recipients, role_id=None):
        # Учитываем время исходного сканирования, чтобы закрыть кнопки до истечения токена.
        now = datetime.now(UTC)
        created = getattr(original, 'created_at', now)
        duration = max(1, 840 - (now - created).total_seconds())
        super().__init__(timeout=duration)
        self.store, self.original = store, original
        self.owner_id, self.guild_id = original.user.id, original.guild_id
        self.channel_id = original.channel_id
        self.recipients = list(dict.fromkeys(recipients))
        self.role_id = role_id
        self.finished = False
        self.lock = asyncio.Lock()
        self.expires_at = time.monotonic() + duration
        self.expiry_handle = asyncio.get_running_loop().call_later(duration, lambda: asyncio.create_task(self.on_timeout()))

    def prompt(self):
        batches = (len(self.recipients) + BATCH_SIZE - 1) // BATCH_SIZE
        return (f'**Уведомить участников?**\nНайдено нарушений: **{len(self.recipients)}**. '
                f'После подтверждения отправлю в <#{self.channel_id}> до **{batches}** сообщений '
                'с тегами и просьбой исправить ник. До подтверждения никто не уведомляется.\n'
                'Перед отправкой проверю ники и роль повторно: уже исправивших ник не отмечу. '
                'Подтверждение действует до 14 минут.')

    def finish(self):
        self.finished = True
        self.expiry_handle.cancel()
        self.stop()
        for item in self.children:
            item.disabled = True

    async def update_status(self, interaction, content):
        # После отправки уведомлений ошибка личного ответа не должна запускать повторную рассылку.
        try:
            await interaction.edit_original_response(content=content, view=None, allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            log_error('Не удалось обновить подтверждение уведомлений', error)
            try:
                await interaction.followup.send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            except Exception as followup_error:
                log_error('Не удалось отправить результат уведомления администратору', followup_error)

    async def on_timeout(self):
        async with self.lock:
            if not self.finished:
                self.finish()
                await self.update_status(self.original, 'Срок подтверждения истёк. Участники не уведомлялись. Запустите /ники проверить заново.')

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)

    async def current_recipients(self, guild):
        from lafamily.modules.nicknames import valid_nickname
        wanted = set(self.recipients)
        eligible = set()
        try:
            async with asyncio.timeout(120):
                async for member in guild.fetch_members(limit=None):
                    if member.id not in wanted or member.bot:
                        continue
                    if self.role_id is not None and not any(role.id == self.role_id for role in member.roles):
                        continue
                    if not valid_nickname(member.display_name):
                        eligible.add(member.id)
        except (TimeoutError, discord.DiscordException) as error:
            raise UserError('Не удалось повторно проверить участников. Никто не уведомлён. Попробуйте подтвердить позже.') from error
        # Новые нарушения, появившиеся после исходного отчёта, не добавляются к согласованному списку.
        return [member_id for member_id in self.recipients if member_id in eligible]

    async def handle(self, interaction, action):
        if self.lock.locked():
            raise UserError('Уведомление уже обрабатывается. Дождитесь результата.')
        async with self.lock:
            await interaction.response.defer()
            if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
                raise UserError('Подтвердить может только администратор, запустивший эту проверку.')
            if self.finished or time.monotonic() >= self.expires_at:
                raise UserError('Подтверждение истекло или уже обработано. Запустите новую проверку.')
            await require_access_async(interaction, self.store, 'admin')
            if action == 'cancel':
                self.finish()
                await self.update_status(interaction, 'Тегирование отменено. Участники не уведомлялись.')
                return
            try:
                channel = await interaction.guild.fetch_channel(self.channel_id)
            except discord.DiscordException as error:
                raise UserError('Канал для уведомлений недоступен. Никто не уведомлён.') from error
            me = interaction.guild.me
            permissions = channel.permissions_for(me) if me else None
            # В ветке используется отдельное право отправки, а не Send Messages родительского канала.
            can_send = permissions and permissions.view_channel and (
                permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages)
            if not can_send:
                raise UserError('Боту нужны права просмотра и отправки сообщений в канал проверки. Никто не уведомлён.')
            recipients = await self.current_recipients(interaction.guild)
            # С этого момента подтверждение одноразовое. При неоднозначном сетевом сбое не повторяем отправку.
            self.finish()
            if not recipients:
                await self.update_status(interaction, 'Уведомлять некого: участники исправили ники, потеряли выбранную роль или вышли с сервера.')
                return
            sent = 0
            for start in range(0, len(recipients), BATCH_SIZE):
                batch = recipients[start:start + BATCH_SIZE]
                mentions = ' '.join(f'<@{member_id}>' for member_id in batch)
                message = (f'{mentions}\nПожалуйста, исправьте ник на сервере на **Name Surname | Static ID**.\n'
                           'Должность перед именем допускается: `Зам. | Sin Bertoni | 15096`. Спасибо!')
                try:
                    await channel.send(content=message, allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=[discord.Object(id=uid) for uid in batch], replied_user=False))
                except Exception as error:
                    log_error('Отправка уведомлений о никах прервана', error)
                    await self.update_status(interaction,
                        f'Отправка прервана. Подтверждена отправка для **{sent}** участников. '
                        f'Результат текущей группы (**{len(batch)}** чел.) не подтверждён — проверьте канал. '
                        f'До оставшихся **{len(recipients) - sent - len(batch)}** участников отправка не дошла. '
                        'Автоматического повтора нет, чтобы не тегировать людей дважды.')
                    return
                sent += len(batch)
            await self.update_status(interaction, f'Сообщения с тегами для **{sent}** участников отправлены в <#{self.channel_id}>. '
                                     f'Исключено после повторной проверки: **{len(self.recipients) - sent}**. Ники бот не менял.')

    @discord.ui.button(label='Уведомить участников', style=discord.ButtonStyle.primary, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle(interaction, 'confirm')

    @discord.ui.button(label='Не тегировать', style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle(interaction, 'cancel')
