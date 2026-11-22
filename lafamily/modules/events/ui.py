"""Сообщения отчётов и временная форма выбора состава мероприятия."""

import asyncio
from functools import partial
import time
from typing import TYPE_CHECKING

import discord
from discord.utils import escape_markdown, escape_mentions

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
            value="\n".join(f"<@{member}>" for member in members[offset:offset + 25]), inline=False,
        )


_TYPE_MARKDOWN_ESCAPES = str.maketrans({symbol: "\\" + symbol for symbol in r"\`*_~|[]#()+-!."})


def _short_type_name(name: str) -> str:
    text = " ".join(name.split())
    if len(text) > 40:
        text = text[:39].rstrip() + "…"
    text = text.replace("<", "‹").replace(">", "›")
    return escape_mentions(text).translate(_TYPE_MARKDOWN_ESCAPES)


def _member_change(members: list[str], singular: str, plural: str) -> str:
    if len(members) == 1:
        return f"{singular} <@{members[0]}>"
    shown = 8
    roster = ", ".join(f"<@{member}>" for member in members[:shown])
    if len(members) > shown:
        roster += f" и ещё {len(members) - shown}"
    return f"{plural} ({len(members)}): {roster}"


def _report_edit_summary(edit: dict) -> str:
    before, after = edit["before"], edit["after"]
    old_members, new_members = set(before["members"]), set(after["members"])
    added = sorted(new_members - old_members)
    removed = sorted(old_members - new_members)
    lines = []
    if added:
        lines.append(_member_change(added, "Добавлен участник", "Добавлены участники"))
    if removed:
        lines.append(_member_change(removed, "Удалён участник", "Удалены участники"))

    outcomes = {"": "Без итога", "win": "Победа", "no_win": "Без победы"}
    old_base = before["points"] - before["win_bonus"]
    new_base = after["points"] - after["win_bonus"]
    changes = (
        ("Вид", before["type_name"], after["type_name"], _short_type_name),
        ("Дата (Москва)", before["occurred_at"], after["occurred_at"], display_date),
        ("Итог", before["outcome"], after["outcome"], outcomes.__getitem__),
        ("Базовые очки", old_base, new_base, str),
        ("Бонус победы", before["win_bonus"], after["win_bonus"], str),
        ("MVP", before["mvp_id"], after["mvp_id"], lambda value: f"<@{value}>" if value else "не выбран"),
        ("Бонус MVP", before["mvp_bonus"], after["mvp_bonus"], str),
    )
    for label, old, new, format_value in changes:
        if old != new:
            lines.append(f"{label}: {format_value(old)} → {format_value(new)}")
    if before["note"] != after["note"]:
        if not after["note"]:
            lines.append("Примечание удалено")
        elif not before["note"]:
            lines.append("Примечание добавлено")
        else:
            lines.append("Примечание изменено")
    return "\n".join(lines)


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
    if report.get("last_edit"):
        summary = _report_edit_summary(report["last_edit"])
        if summary:
            embed.add_field(name="Последнее исправление", value=summary, inline=False)
    return embed.set_footer(text="LaFamily • учёт мероприятий")


def closed_draft(text):
    layout = discord.ui.LayoutView(timeout=None)
    layout.add_item(discord.ui.TextDisplay(text))
    return {'view': layout, 'allowed_mentions': discord.AllowedMentions.none()}


class ReportView(discord.ui.LayoutView):
    """Черновик живёт 1 час; им управляют автор и руководство сервера."""

    PAGE_SIZE = 7
    add_row = discord.ui.ActionRow()
    confirm_row = discord.ui.ActionRow()
    join_row = discord.ui.ActionRow()
    outcome_row = discord.ui.ActionRow()
    mvp_row = discord.ui.ActionRow()

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
        self.page = 0
        self.roster_snapshot = tuple(draft.members)
        self.roster_revision = 0
        self.controls = [self.add_row, self.confirm_row, self.join_row]
        if draft.win_enabled:
            self.controls.append(self.outcome_row)
        if draft.available_mvp_bonus:
            self.controls.append(self.mvp_row)
        for row in self.controls:
            for item in row.children:
                item.custom_id = f"mp:{item.custom_id}:{draft.request_id}"
        self.payload()

    def check_open(self, interaction):
        if self.finished or time.monotonic() >= self.expires_at:
            raise UserError("Черновик истёк или уже обработан. Создайте новый через /мп создать.")
        if str(interaction.guild_id) != self.draft.guild_id:
            raise UserError("Черновик относится к другому серверу.")

    async def validate(self, interaction: discord.Interaction, *, removal=False):
        self.check_open(interaction)
        # Любой организатор может убрать игрока. Остальные действия в чужом черновике доступны руководству.
        # Права читаются заново при каждом действии, включая открытые ранее меню.
        level = "organizer" if removal or str(interaction.user.id) == self.draft.organizer_id else "manager"
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

    def payload(self, members=None, notice=None) -> dict:
        for option in self.select_outcome.options:
            option.default = option.value == self.draft.outcome
        members = self.draft.members if members is None else members
        self.confirm.disabled = (self.finished or not members
                                 or (self.draft.win_enabled and not self.draft.outcome))
        self.clear_items()
        snapshot = tuple(members)
        if self.roster_snapshot != snapshot:
            self.roster_snapshot = snapshot
            self.roster_revision += 1
        last_page = max(0, (len(members) - 1) // self.PAGE_SIZE)
        self.page = min(self.page, last_page)
        start = self.PAGE_SIZE * self.page
        lines = [f'**Подтверждение отчёта · {escape_markdown(self.draft.type_name)}**',
                 f'{display_date(self.draft.occurred_at)} (Москва) · {self.draft.points + self.draft.win_bonus} очк. каждому']
        if self.draft.note:
            lines.append(f'Примечание: {escape_markdown(self.draft.note)}')
        if self.draft.voice_channel_id:
            lines.append(f'Голосовой канал МП: <#{self.draft.voice_channel_id}>')
        if self.draft.win_enabled:
            outcome = {'win': 'Победа', 'no_win': 'Без победы'}.get(self.draft.outcome, 'выберите результат ниже')
            lines.append(f'Итог: {outcome} · бонус каждому +{self.draft.win_bonus}')
        if self.draft.available_mvp_bonus:
            mvp = f'<@{self.draft.mvp_id}>' if self.draft.mvp_id else 'не выбран'
            lines.append(f'MVP: {mvp} · бонус +{self.draft.available_mvp_bonus}')
        pending = [member for member in self.pending if member not in members]
        if pending:
            lines.append(f'Ожидают проверки: {len(pending)} · всего с заявками: {len(members) + len(pending)}\n' + ', '.join(f'<@{member}>' for member in pending))
        lines.extend(['', 'Нажмите «Участвовать», чтобы записаться. Организаторы могут убрать участника кнопкой ❌.',
                      'Черновик действует 1 час. При подтверждении баллы получат только участники состава.',
                      f'**Состав: {len(members)}/100** · страница {self.page + 1}/{last_page + 1}'])
        if notice:
            lines.insert(0, notice + '\n')
        self.add_item(discord.ui.TextDisplay('\n'.join(lines)))
        for index, player in enumerate(members[start:start + self.PAGE_SIZE], start=start+1):
            button = discord.ui.Button(emoji='❌', style=discord.ButtonStyle.secondary,
                custom_id=f'mp:remove:{self.draft.request_id}:{player}:{self.roster_revision}')
            button.callback = partial(self.remove_player, player=player, revision=self.roster_revision)
            self.add_item(discord.ui.Section(f'{index}. <@{player}>', accessory=button))
        if last_page:
            pages = discord.ui.ActionRow()
            for label, page in (('Назад', self.page - 1), ('Далее', self.page + 1)):
                button = discord.ui.Button(label=label, disabled=not 0 <= page <= last_page,
                    custom_id=f'mp:page:{self.draft.request_id}:{page}')
                button.callback = partial(self.show_page, page=page)
                pages.add_item(button)
            self.add_item(pages)
        for row in self.controls:
            self.add_item(row)
        return {'view': self, 'allowed_mentions': discord.AllowedMentions.none()}

    async def edit_layout(self, edit, members=None, *, page=None, notice=None):
        # Discord.py хранит обработчики прежних кнопок. При сбое возвращаем эти же объекты,
        # чтобы на опубликованной карточке можно было повторить нажатие.
        previous = self.children
        position = self.page, self.roster_snapshot, self.roster_revision
        confirm_disabled = self.confirm.disabled
        defaults = [option.default for option in self.select_outcome.options]
        if page is not None:
            self.page = max(0, page)
        try:
            await edit(**self.payload(members, notice))
        except BaseException:
            self.page, self.roster_snapshot, self.roster_revision = position
            self.confirm.disabled = confirm_disabled
            for option, selected in zip(self.select_outcome.options, defaults):
                option.default = selected
            self.clear_items()
            for child in previous:
                self.add_item(child)
            self.needs_refresh = True
            raise

    async def show_page(self, interaction, *, page=None):
        await interaction.response.defer()
        async with self.lock:
            self.check_open(interaction)
            await self.edit_layout(interaction.edit_original_response, page=page)
            self.needs_refresh = False

    async def remove_player(self, interaction, *, player, revision):
        await interaction.response.defer()
        async with self.lock:
            await self.validate(interaction, removal=True)
            if revision != self.roster_revision or player not in self.draft.members:
                raise UserError('Состав изменился. Выберите участника в текущем списке.')
            members = [member for member in self.draft.members if member != player]
            await self.update_members(interaction, members)
            self.draft.duplicate_ack = ''


    def finish(self):
        self.finished = True
        for item in self.walk_children():
            if item.is_dispatchable():
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
                    await self.message.edit(**closed_draft(reason))
                else:
                    await self.original.edit_original_response(**closed_draft(reason))
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
                await interaction.edit_original_response(**closed_draft("Черновик отменён. Очки не начислялись."))
                return
            if action == 'allow_duplicate':
                self.draft.enforce_duplicates = True
                key, matches = await database_call(self.service.store, 'duplicate_report_key', self.draft)
                if not matches:
                    raise UserError('Похожих отчётов нет. Используйте обычное подтверждение.')
                notice = 'Разрешён отдельный отчёт при наличии похожих: ' + ', '.join(f'#{n}' for n in matches)
                await self.edit_layout(interaction.edit_original_response, notice=notice + '. Проверьте состав перед подтверждением.')
                self.draft.duplicate_ack = key
                return
            if action == "reject_pending":
                # Отклонение — отдельное явное действие организатора, а не побочный эффект сохранения.
                previous = self.pending
                self.pending = []
                try:
                    await self.edit_layout(interaction.edit_original_response, notice="Оставшиеся заявки отклонены. Проверьте подтверждённый состав.")
                except BaseException:
                    self.pending = previous
                    raise
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
                    await self.edit_layout(interaction.edit_original_response)
                except BaseException:
                    self.draft.outcome, self.draft.win_bonus, self.draft.mvp_id, self.draft.mvp_bonus = old
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
                    await self.edit_layout(interaction.edit_original_response,
                        notice="Состав заново показан после ошибки связи. Проверьте его и подтвердите ещё раз.")
                    self.needs_refresh = False
                    return
                content = await self.service.confirm(interaction, self)
                await interaction.edit_original_response(**closed_draft(content))
                return
            await self.update_members(interaction, members)

    async def update_members(self, interaction, members):
        previous_mvp = (self.draft.mvp_id, self.draft.mvp_bonus)
        if self.draft.mvp_id not in members:
            self.draft.mvp_id, self.draft.mvp_bonus = None, 0
        try:
            await self.edit_layout(interaction.edit_original_response, members)
        except BaseException:
            self.draft.mvp_id, self.draft.mvp_bonus = previous_mvp
            raise
        # Состав меняется только после успешного показа пользователю.
        self.draft.members = members
        self.pending = [member for member in self.pending if member not in members]
        self.needs_refresh = False

    @join_row.button(label="Участвовать", custom_id="join", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
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
                    await interaction.followup.send(
                        f'<@{interaction.user.id}>, МП проводится в <#{self.draft.voice_channel_id}>. '
                        f'Зайдите в канал и нажмите «Участвовать» снова.\n'
                        f'https://discord.com/channels/{self.draft.guild_id}/{self.draft.voice_channel_id}',
                        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[interaction.user]))
                    return
            member_id = str(interaction.user.id)
            if member_id in self.pending or member_id in self.draft.members:
                await interaction.followup.send("Вы уже участвуете в этом МП.", ephemeral=True)
                return
            if len(self.pending) + len(self.draft.members) >= 100:
                raise UserError("В этом отчёте уже записано 100 человек.")
            await self.update_members(interaction, [*self.draft.members, member_id])

    @join_row.button(label="Обновить", custom_id="refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_page(interaction)

    @join_row.button(label="Добавить из голосового", custom_id="voice", style=discord.ButtonStyle.secondary)
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
                await self.edit_layout(self.message.edit, members)
            except BaseException:
                self.draft.voice_channel_id = old_voice
                raise
            self.draft.members = members
            self.pending = [member for member in self.pending if member not in members]
            self.needs_refresh = False
        await interaction.edit_original_response(
            content=f"Из <#{channel.id}> добавлено: {added}. Всего в составе: {len(members)}. "
                    "Проверьте общий черновик и подтвердите отчёт, когда он готов.",
            view=None, allowed_mentions=discord.AllowedMentions.none())

    @add_row.select(cls=discord.ui.UserSelect, custom_id="add", placeholder="Организатор: добавить участников", min_values=1, max_values=25)
    async def select_members(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await self.dispatch("add", interaction, select.values)

    @confirm_row.button(label="Подтвердить отчёт", custom_id="confirm", style=discord.ButtonStyle.success, disabled=True)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.dispatch("confirm", interaction)

    @confirm_row.button(label="Очистить состав", custom_id="clear", style=discord.ButtonStyle.secondary)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.dispatch("clear", interaction)

    @confirm_row.button(label="Отменить черновик", custom_id="discard", style=discord.ButtonStyle.danger)
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.dispatch("discard", interaction)

    @confirm_row.button(label='Разрешить похожий отчёт', custom_id='allow_duplicate', style=discord.ButtonStyle.secondary)
    async def allow_duplicate(self, interaction, button):
        await self.dispatch('allow_duplicate', interaction)



    @outcome_row.select(custom_id="outcome", placeholder="Организатор: выбрать результат",
                       options=[discord.SelectOption(label="Победа", value="win"),
                                discord.SelectOption(label="Без победы", value="no_win")])
    async def select_outcome(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self.dispatch("outcome", interaction, select.values[0])

    @mvp_row.select(cls=discord.ui.UserSelect, custom_id="mvp", placeholder="Организатор: выбрать MVP (очистить — без MVP)",
                       min_values=0, max_values=1)
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
