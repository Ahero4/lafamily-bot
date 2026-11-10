"""Сообщения отчётов и временная форма выбора состава мероприятия."""

import asyncio
import time
from typing import TYPE_CHECKING

import discord
from discord.utils import escape_markdown

from lafamily.errors import UserError
from lafamily.async_database import database_call
from lafamily.modules.events.models import Draft
from lafamily.modules.events.time import display_date
from lafamily.permissions import require_access_async
from lafamily.responses import send_error

if TYPE_CHECKING:
    from lafamily.modules.events.service import EventService


def add_members(embed: discord.Embed, members: list[str]):
    # Поле Discord вмещает 1024 символа: разбиваем состав по 25 упоминаний.
    for offset in range(0, len(members), 25):
        embed.add_field(
            name=f"Участники {offset + 1}–{min(offset + 25, len(members))} из {len(members)}",
            value=" ".join(f"<@{member}>" for member in members[offset:offset + 25]), inline=False,
        )


def report_embed(report: dict) -> discord.Embed:
    cancelled = bool(report["cancelled_at"])
    if 'barn_amount' in report:
        embed = discord.Embed(title=f"🐄 Коровник — сбор #{report['id']}" + (' — отменён' if cancelled else ''),
                              colour=0xD64545 if cancelled else 0x43B581)
        embed.add_field(name='Собрал', value=f"<@{report['members'][0]}>")
        embed.add_field(name='Сумма', value=f"${report['barn_amount']:,}".replace(',', ' '))
        embed.add_field(name='Начислено' if not cancelled else 'Исключено из статистики', value=f"{report['points']} балл.")
        if cancelled:
            embed.add_field(name='Причина отмены', value=escape_markdown(report['cancel_reason']), inline=False)
        return embed.set_footer(text='Сбор отменён. Баллы исключены.' if cancelled else
            'Баллы учтены в общей статистике. Ошибка? Обратитесь к руководству с номером сбора.')
    embed = discord.Embed(
        title=f"МП #{report['id']} — {'отменено' if cancelled else 'учтено'}",
        description=escape_markdown(report["type_name"]), colour=0xD64545 if cancelled else 0x41A878,
    )
    embed.add_field(name="Дата (Москва)", value=display_date(report["occurred_at"]))
    embed.add_field(name="Очки каждому", value=str(report["points"]))
    embed.add_field(name="Организатор", value=f"<@{report['organizer_id']}>")
    if report.get("outcome"):
        embed.add_field(name="Итог", value=("Победа" if report["outcome"] == "win" else "Без победы") + f" • бонус каждому +{report['win_bonus']}")
    if report.get("mvp_id"):
        embed.add_field(name="MVP", value=f"<@{report['mvp_id']}> • дополнительно +{report['mvp_bonus']} очк. • всего {report['points'] + report['mvp_bonus']}")
    add_members(embed, report["members"])
    if report["note"]:
        embed.add_field(name="Примечание", value=escape_markdown(report["note"]), inline=False)
    if cancelled:
        embed.add_field(name="Причина отмены", value=escape_markdown(report["cancel_reason"]), inline=False)
    return embed.set_footer(text="LaFamily • учёт мероприятий")


class ReportView(discord.ui.View):
    """Черновик живёт 1 час; им управляют автор и руководство сервера."""

    def __init__(self, service: "EventService", draft: Draft):
        # Каждый клик даёт свежий interaction; таймер завершения редактирует
        # обычное сообщение через bot API, без зависимости от 15-минутного токена.
        super().__init__(timeout=3600)
        self.service = service
        self.draft = draft
        self.expires_at = time.monotonic() + 3600
        self.lock = asyncio.Lock()
        self.finished = False
        self.needs_refresh = False
        self.pending = []  # Совместимость с проверками старого интерфейса; самозапись сразу идёт в состав.
        self.original = None
        self.message = None
        self.expiry_handle = None
        if not draft.win_enabled:
            self.remove_item(self.select_outcome)
        if not draft.available_mvp_bonus:
            self.remove_item(self.select_mvp)
        # Свой ID у каждого компонента позволяет отличить разные черновики.
        for item in self.children:
            item.custom_id = f"mp:{item.custom_id}:{draft.request_id}"

    async def validate(self, interaction: discord.Interaction):
        if self.finished or time.monotonic() >= self.expires_at:
            raise UserError("Черновик истёк или уже обработан. Создайте новый через /мп создать.")
        if str(interaction.guild_id) != self.draft.guild_id:
            raise UserError("Черновик относится к другому серверу.")
        # Автор управляет своим черновиком; чужим — только руководство или admin.
        # Права читаются заново при каждом действии, включая открытые ранее меню.
        level = "organizer" if str(interaction.user.id) == self.draft.organizer_id else "manager"
        await require_access_async(interaction, self.service.store, level)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            # Проверка роли выполняется после defer в dispatch, до операций с данными.
            if self.finished or time.monotonic() >= self.expires_at:
                raise UserError("Черновик истёк или уже обработан. Создайте новый через /мп создать.")
            return True
        except UserError as error:
            await send_error(interaction, error)
            return False

    def payload(self, members=None) -> dict:
        for option in self.select_outcome.options:
            option.default = option.value == self.draft.outcome
        members = self.draft.members if members is None else members
        self.confirm.disabled = (self.finished or not members
                                 or (self.draft.win_enabled and not self.draft.outcome))
        embed = discord.Embed(colour=0x7289DA)
        add_members(embed, members)
        pending = [member for member in self.pending if member not in members]
        for offset in range(0, len(pending), 25):
            embed.add_field(name="Заявки — ожидают проверки организатором",
                value=" ".join(f"<@{member}>" for member in pending[offset:offset + 25]), inline=False)
        if self.draft.voice_channel_id:
            embed.add_field(name="Голосовой канал МП", value=f"<#{self.draft.voice_channel_id}>", inline=False)
        if self.draft.win_enabled and not self.draft.outcome:
            embed.add_field(name="Итог", value="Выберите Победа / Без победы в меню ниже.")
        if self.draft.available_mvp_bonus and not self.draft.mvp_id:
            embed.add_field(name="MVP", value=f"Не выбран • можно назначить одного участника состава (+{self.draft.available_mvp_bonus} очк.).")
        if self.draft.outcome:
            embed.add_field(name="Итог", value=("Победа" if self.draft.outcome == "win" else "Без победы") + f" • +{self.draft.win_bonus} каждому")
        if self.draft.mvp_id:
            embed.add_field(name="MVP", value=f"<@{self.draft.mvp_id}> • ещё +{self.draft.mvp_bonus} очк. (должен быть в составе)")
        note = f"Примечание: {escape_markdown(self.draft.note)}\n" if self.draft.note else ""
        content = (
            f"**Подтверждение отчёта**\n{escape_markdown(self.draft.type_name)} • {self.draft.points + self.draft.win_bonus} очк. каждому\n"
            f"{display_date(self.draft.occurred_at)} (Москва)\n{note}"
            f"Подтверждено: **{len(members)}** · Ожидают проверки: **{len(pending)}** · Всего: **{len(members) + len(pending)}**.\n"
            "Нажмите «Участвовать» — вы сразу попадёте в состав МП.\n"
            "Автор и руководство могут менять состав, результат и MVP. Итоговое подтверждение начислит баллы всему составу. До 100 участников.\n"
            "Черновик действует 1 час. Подтверждение сохранит отчёт и начислит очки."
        )
        return {"content": content, "embeds": [embed] if members or self.pending or self.draft.voice_channel_id or self.draft.outcome or self.draft.mvp_id else [], "view": self,
                "allowed_mentions": discord.AllowedMentions.none()}

    def finish(self):
        self.finished = True
        for item in self.children:
            item.disabled = True
        if self.expiry_handle:
            self.expiry_handle.cancel()
        self.stop()
        key = (self.draft.guild_id, self.draft.organizer_id)
        if self.service.views.get(key) is self:
            self.service.views.pop(key)

    def bind(self, interaction: discord.Interaction):
        self.original = interaction
        if self.expiry_handle is None:
            self.expiry_handle = asyncio.get_running_loop().call_later(
                max(0, self.expires_at - time.monotonic()), lambda: asyncio.create_task(self.on_timeout()))

    async def retire(self, reason: str):
        self.finish()
        if self.message is not None or self.original:
            try:
                if self.message is not None:
                    await self.message.edit(content=reason, embeds=[], view=None)
                else:
                    await self.original.edit_original_response(content=reason, embeds=[], view=None)
            except Exception as error:
                from lafamily.diagnostics import log_error
                log_error("Не удалось отключить старый черновик", error)

    async def on_timeout(self):
        async with self.lock:
            if not self.finished:
                await self.retire("Срок черновика истёк. Создайте новый через /мп создать.")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        await send_error(interaction, error)

    async def dispatch(self, action: str, interaction: discord.Interaction, selected=None):
        # Блокировка ставится до первого сетевого запроса: параллельный клик не создаёт дубль.
        if self.lock.locked():
            raise UserError("Отчёт обрабатывается. Дождитесь результата.")
        async with self.lock:
            await interaction.response.defer()
            await self.validate(interaction)
            self.original = interaction
            if getattr(interaction, "message", None) is not None:
                self.message = interaction.message
            if action == "discard":
                self.finish()
                await interaction.edit_original_response(content="Черновик отменён. Очки не начислялись.", embeds=[], view=None)
                return
            if action == 'allow_duplicate':
                self.draft.enforce_duplicates = True
                key, matches = await database_call(self.service.store, 'duplicate_report_key', self.draft)
                if not matches:
                    raise UserError('Похожих отчётов нет. Используйте обычное подтверждение.')
                payload = self.payload()
                payload['content'] = ('Вы разрешили отдельный отчёт при наличии похожих: '+
                    ', '.join(f'#{n}' for n in matches)+'. Проверьте состав и нажмите «Подтвердить отчёт».\n'+payload['content'])
                await interaction.edit_original_response(**payload)
                self.draft.duplicate_ack = key
                return
            if action == "reject_pending":
                # Отклонение — отдельное явное действие организатора, а не побочный эффект сохранения.
                await interaction.edit_original_response(content="Оставшиеся заявки отклонены. Проверьте подтверждённый состав и нажмите «Подтвердить отчёт».", embeds=[], view=self)
                self.pending = []
                await interaction.edit_original_response(**self.payload())
                return
            if action in ("outcome", "mvp"):
                # Изменения сериализуются с составом и подтверждением. При сетевом
                # сбое откатываем непоказанный выбор, сохраняя снимок ставок.
                old = (self.draft.outcome, self.draft.win_bonus, self.draft.mvp_id, self.draft.mvp_bonus)
                if action == "outcome":
                    if not self.draft.win_enabled or selected not in ("win", "no_win"):
                        raise UserError("Выбор результата недоступен.")
                    self.draft.outcome = selected
                    self.draft.win_bonus = self.draft.available_win_bonus if selected == "win" else 0
                else:
                    if not self.draft.available_mvp_bonus:
                        raise UserError("Награда MVP не настроена для этого вида МП.")
                    member = selected[0] if selected else None
                    if member and (member.bot or str(member.id) not in self.draft.members):
                        raise UserError("MVP должен быть участником состава и не быть ботом. Сначала добавьте его в состав.")
                    self.draft.mvp_id = str(member.id) if member else None
                    self.draft.mvp_bonus = self.draft.available_mvp_bonus if member else 0
                try:
                    await interaction.edit_original_response(**self.payload())
                except BaseException:
                    self.draft.outcome, self.draft.win_bonus, self.draft.mvp_id, self.draft.mvp_bonus = old
                    self.payload()
                    self.needs_refresh = True
                    raise
                self.needs_refresh = False
                return
            if action == "clear":
                members = []
            elif action == "add":
                if any(member.bot or not isinstance(member, discord.Member) for member in selected):
                    raise UserError("Выбирайте участников этого сервера; ботов включать нельзя.")
                members = list(dict.fromkeys([*self.draft.members, *(str(member.id) for member in selected)]))
                if len(members) > 100:
                    raise UserError("В первой версии можно указать не больше 100 участников.")
            elif action == "confirm":
                if self.pending:
                    raise UserError(f"Ожидают проверки ещё {len(self.pending)} заявок. Подтвердите присутствующих через меню или явно отклоните оставшиеся заявки. Отчёт не сохранён.")
                if self.needs_refresh:
                    # После неоднозначного сетевого сбоя сначала показываем сохранённый состав.
                    payload = self.payload()
                    payload["content"] = "Состав заново показан после ошибки связи. Проверьте его и подтвердите ещё раз.\n\n" + payload["content"]
                    await interaction.edit_original_response(**payload)
                    self.needs_refresh = False
                    return
                content = await self.service.confirm(interaction, self)
                await interaction.edit_original_response(content=content, embeds=[], view=None)
                return
            previous_mvp = (self.draft.mvp_id, self.draft.mvp_bonus)
            if self.draft.mvp_id not in members:
                self.draft.mvp_id, self.draft.mvp_bonus = None, 0
            try:
                await interaction.edit_original_response(**self.payload(members))
            except BaseException:
                self.draft.mvp_id, self.draft.mvp_bonus = previous_mvp
                self.needs_refresh = True
                self.payload()  # Восстанавливаем локальное состояние кнопки подтверждения.
                raise
            # Состав меняется только после успешного показа пользователю.
            self.draft.members = members
            self.pending = [member for member in self.pending if member not in members]
            self.needs_refresh = False

    @discord.ui.button(label="Участвовать", custom_id="join", style=discord.ButtonStyle.success, row=2)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Самозапись сразу включает участника в начисляемый состав; общий lock защищает от дублей.
        async with self.lock:
            if self.finished or time.monotonic() >= self.expires_at:
                raise UserError("Запись закрыта. Попросите организатора создать новый отчёт.")
            if str(interaction.guild_id) != self.draft.guild_id or interaction.user.bot:
                raise UserError("Записаться может участник этого сервера.")
            if self.draft.voice_channel_id:
                voice = getattr(interaction.user, 'voice', None)
                channel = getattr(voice, 'channel', None)
                if channel is None or str(channel.id) != self.draft.voice_channel_id:
                    # Публично отмечаем только нажавшего, без упоминаний ролей и everyone.
                    await interaction.response.send_message(
                        f'<@{interaction.user.id}>, МП проводится в <#{self.draft.voice_channel_id}>. '
                        f'Зайдите в канал и нажмите «Участвовать» снова.\n'
                        f'https://discord.com/channels/{self.draft.guild_id}/{self.draft.voice_channel_id}',
                        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[interaction.user]))
                    return
            member_id = str(interaction.user.id)
            if member_id in self.pending or member_id in self.draft.members:
                await interaction.response.send_message("Вы уже участвуете в этом МП.", ephemeral=True)
                return
            if len(self.pending) + len(self.draft.members) >= 100:
                raise UserError("В этом отчёте уже записано 100 человек.")
            self.draft.members.append(member_id)
            try:
                await interaction.response.edit_message(**self.payload())
            except BaseException:
                self.draft.members.remove(member_id)
                self.needs_refresh = True
                self.payload()
                raise

    @discord.ui.button(label="Добавить из голосового", custom_id="voice", style=discord.ButtonStyle.secondary, row=2)
    async def choose_voice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.validate(interaction)
        if getattr(interaction, "message", None) is not None:
            self.message = interaction.message
        picker = VoiceRosterPicker(self, interaction.user.id)
        await interaction.edit_original_response(
            content="Выберите голосовой канал. Его текущие участники без ботов будут добавлены к составу без дублей. "
                    "Ранее записанные останутся. Выбранный канал станет каналом МП: самозапись будет проверять присутствие в нём.",
            view=picker, allowed_mentions=discord.AllowedMentions.none())

    async def import_voice(self, interaction: discord.Interaction, channel_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            await self.validate(interaction)
            channel = await interaction.guild.fetch_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel) or str(channel.guild.id) != self.draft.guild_id:
                raise UserError("Выберите голосовой канал этого сервера.")
            if not channel.permissions_for(interaction.user).view_channel:
                raise UserError("У вас нет доступа к выбранному голосовому каналу.")
            members = list(dict.fromkeys([*self.draft.members,
                *(str(member.id) for member in channel.members if not member.bot)]))
            if len(members) > 100:
                raise UserError("Общий состав превышает 100 участников. Состав не изменён.")
            if self.message is None:
                raise UserError("Сообщение черновика недоступно. Откройте выбор канала из отчёта заново.")
            old_voice = self.draft.voice_channel_id
            added = len(members) - len(self.draft.members)
            self.draft.voice_channel_id = str(channel.id)
            try:
                # Обновляем общий черновик через bot API, а не личное окно выбора канала.
                await self.message.edit(**self.payload(members))
            except BaseException:
                self.draft.voice_channel_id = old_voice
                self.payload()
                self.needs_refresh = True
                raise
            self.draft.members = members
            self.pending = [member for member in self.pending if member not in members]
            self.needs_refresh = False
        await interaction.edit_original_response(
            content=f"Из <#{channel.id}> добавлено: {added}. Всего в составе: {len(members)}. "
                    "Проверьте общий черновик и подтвердите отчёт, когда он готов.",
            view=None, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.select(cls=discord.ui.UserSelect, custom_id="add", placeholder="Организатор: добавить участников", min_values=1, max_values=25, row=0)
    async def select_members(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await self.dispatch("add", interaction, select.values)

    @discord.ui.button(label="Подтвердить отчёт", custom_id="confirm", style=discord.ButtonStyle.success, disabled=True, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.dispatch("confirm", interaction)

    @discord.ui.button(label="Очистить состав", custom_id="clear", style=discord.ButtonStyle.secondary, row=1)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.dispatch("clear", interaction)

    @discord.ui.button(label="Отменить черновик", custom_id="discard", style=discord.ButtonStyle.danger, row=1)
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.dispatch("discard", interaction)

    @discord.ui.button(label='Разрешить похожий отчёт', custom_id='allow_duplicate', style=discord.ButtonStyle.secondary, row=1)
    async def allow_duplicate(self, interaction, button):
        await self.dispatch('allow_duplicate', interaction)



    @discord.ui.select(custom_id="outcome", placeholder="Организатор: выбрать результат", row=3,
                       options=[discord.SelectOption(label="Победа", value="win"),
                                discord.SelectOption(label="Без победы", value="no_win")])
    async def select_outcome(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self.dispatch("outcome", interaction, select.values[0])

    @discord.ui.select(cls=discord.ui.UserSelect, custom_id="mvp", placeholder="Организатор: выбрать MVP (очистить — без MVP)",
                       min_values=0, max_values=1, row=4)
    async def select_mvp(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await self.dispatch("mvp", interaction, select.values)


class VoiceRosterPicker(discord.ui.View):
    """Личное окно выбора, не занимающее дополнительную строку основного отчёта."""
    def __init__(self, parent, owner_id):
        super().__init__(timeout=max(1, min(300, parent.expires_at - time.monotonic())))
        self.parent = parent
        self.owner_id = owner_id

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await send_error(interaction, UserError("Откройте выбор канала своей кнопкой в отчёте."))
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.voice],
                       placeholder="Выберите голосовой канал для добавления состава", min_values=1, max_values=1)
    async def channel_select(self, interaction, select):
        await self.parent.import_voice(interaction, select.values[0].id)
        self.stop()
