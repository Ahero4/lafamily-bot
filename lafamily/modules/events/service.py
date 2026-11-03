"""Операции Discord, общие для команд и формы отчёта."""

import asyncio
import logging

import discord

from lafamily.database import Store
from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.diagnostics import log_error
from lafamily.modules.events.models import Draft
from lafamily.modules.events.ui import ReportView, report_embed

logger = logging.getLogger(__name__)


class EventService:
    def __init__(self, store: Store):
        self.store = store
        self.views: dict[tuple[str, str], ReportView] = {}

    async def channel(self, guild: discord.Guild, channel_id: str) -> discord.TextChannel:
        try:
            channel = await guild.fetch_channel(int(channel_id))
            me = guild.me
        except discord.DiscordException as error:
            raise UserError("Канал отчётов недоступен. Попросите администратора проверить /бот настройки.") from error
        if not isinstance(channel, discord.TextChannel):
            raise UserError("Для отчётов нужен обычный текстовый канал.")
        if me is None:
            raise UserError("Информация о правах бота ещё не загружена. Повторите действие после подключения.")
        permissions = channel.permissions_for(me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            raise UserError("Боту нужны права просмотра канала, отправки сообщений и вставки ссылок.")
        return channel

    def new_view(self, draft: Draft) -> ReportView:
        key = (draft.guild_id, draft.organizer_id)
        previous = self.views.get(key)
        if previous:
            if previous.lock.locked():
                raise UserError("Предыдущий отчёт ещё сохраняется. Дождитесь результата.")
            previous.finish()
        view = ReportView(self, draft)
        self.views[key] = view
        return view

    async def new_view_async(self, draft: Draft) -> ReportView:
        previous = self.views.get((draft.guild_id, draft.organizer_id))
        if previous:
            if previous.lock.locked():
                raise UserError("Предыдущий отчёт ещё сохраняется. Дождитесь результата.")
            await previous.retire("Черновик заменён новым. Используйте новое сообщение /мп создать.")
        return self.new_view(draft)

    async def confirm(self, interaction: discord.Interaction, view: ReportView) -> str:
        draft = view.draft
        if getattr(view, "pending", []):
            raise UserError("Не все заявки проверены. Подтвердите или отклоните оставшиеся заявки перед сохранением.")
        if draft.win_enabled and draft.outcome not in ("win", "no_win"):
            raise UserError("Выберите результат в меню подтверждения: Победа или Без победы.")
        if not draft.members:
            raise UserError("Сначала выберите хотя бы одного участника.")
        if draft.mvp_id and draft.mvp_id not in draft.members:
            raise UserError("MVP отсутствует в составе. Добавьте его в состав или измените MVP в меню подтверждения.")
        channel = await self.channel(interaction.guild, draft.channel_id)

        async def fetch_member(member_id):
            try:
                return await interaction.guild.fetch_member(int(member_id))
            except discord.DiscordException:
                return None

        # Проверяем состав адресно: привилегированный Server Members Intent не нужен.
        members = await asyncio.gather(*(fetch_member(member_id) for member_id in draft.members))
        if any(member is None or member.bot for member in members):
            raise UserError("Часть состава недоступна на сервере. Очистите состав и выберите участников заново.")
        report = await database_call(self.store, "create_report", draft)
        view.finish()
        # Сначала сохраняем начисление. Сбой публикации не должен потерять отчёт.
        published = False
        try:
            message = await channel.send(embed=report_embed(report), allowed_mentions=discord.AllowedMentions.none())
            await database_call(self.store, "attach_message", draft.guild_id, report["id"], str(message.id))
            # Если руководство успело отменить отчёт во время отправки, обновляем публикацию.
            latest = await database_call(self.store, "report", draft.guild_id, report["id"])
            if latest["cancelled_at"]:
                await message.edit(embed=report_embed(latest))
            published = True
        except Exception as error:
            log_error(f"Отчёт #{report['id']} сохранён; ошибка публикации", error)
        content = f"Отчёт #{report['id']} сохранён. Каждому из {len(report['members'])} участников начислено {report['points']} очк."
        if report.get('mvp_id'):
            content += f" MVP <@{report['mvp_id']}> дополнительно: +{report['mvp_bonus']} очк."
        if not published:
            content += " Опубликовать сообщение не удалось. Повторно подавать отчёт не нужно; используйте /мп посмотреть."
        return content

    async def update_cancelled(self, guild: discord.Guild, report: dict) -> bool:
        if not report["message_id"]:
            return False
        try:
            channel = await self.channel(guild, report["channel_id"])
            await channel.get_partial_message(int(report["message_id"])).edit(
                embed=report_embed(report), allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except (discord.DiscordException, UserError):
            return False

    def close(self):
        for view in list(self.views.values()):
            view.finish()
