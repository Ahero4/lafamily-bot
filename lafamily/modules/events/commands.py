"""Slash-команды модуля МП; русские названия сохранены при переносе с JS."""

from io import BytesIO
import asyncio
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.utils import escape_markdown

from lafamily.errors import UserError
from lafamily.release import VERSION, LATEST_UPDATE
from lafamily.async_database import database_call
from lafamily.info import send_info, send_farm_info
from lafamily.modules.events.models import Draft
from lafamily.modules.events.payouts import send_payout
from lafamily.modules.events.service import EventService
from lafamily.modules.events.time import MOSCOW, display_date, parse_event_date, week_range
from lafamily.modules.events.ui import report_embed
from lafamily.permissions import require_access_async
from lafamily.settings_ui import SettingsView

WEEKS = [app_commands.Choice(name="Текущая", value=0), app_commands.Choice(name="Прошлая", value=-1)]


@app_commands.guild_only()
class EventCommands(app_commands.Group):
    def __init__(self, service: EventService):
        super().__init__(name="мп", description="Мероприятия семьи")
        self.service = service

    async def type_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # Автодополнение нельзя отложить через defer: при занятой базе отвечаем пустым списком.
        try:
            types = await asyncio.wait_for(database_call(self.service.store, "types", str(interaction.guild_id)), timeout=1)
        except TimeoutError:
            return []
        return [app_commands.Choice(name=f"{kind['name']} — {kind['points']} очк."[:100], value=str(kind["id"]))
                for kind in types if current.casefold() in kind["name"].casefold()][:25]

    @app_commands.command(name="отчёт", description="Создать отчёт о мероприятии", extras={"access": "organizer"})
    @app_commands.rename(kind="вид", date="дата", note="примечание", voice="голосовой-канал", outcome="итог", mvp="mvp")
    @app_commands.describe(kind="Выберите вид МП из подсказок", date="Выберите «Сегодня» или введите ГГГГ-ММ-ДД ЧЧ:ММ по Москве", note="Краткое примечание", voice="Добавить находящихся в голосовом канале участников", outcome="Победа или Без победы, если включено для вида МП", mvp="Один MVP: должен войти в итоговый состав; награда задаётся руководством")
    @app_commands.choices(outcome=[app_commands.Choice(name="Победа", value="win"), app_commands.Choice(name="Без победы", value="no_win")])
    async def create_report(self, interaction: discord.Interaction, kind: str, date: str | None = None, note: app_commands.Range[str, 1, 300] | None = None, voice: discord.VoiceChannel | None = None, outcome: str | None = None, mvp: discord.Member | None = None):
        # Фиксируем время до ожидания базы; повторное подтверждение его не меняет.
        created_at = datetime.now(UTC)
        await interaction.response.defer(thinking=True)
        await require_access_async(interaction, self.service.store, "organizer")
        guild_id = str(interaction.guild_id)
        settings = await database_call(self.service.store, "settings", guild_id)
        if not settings:
            raise UserError("Сначала администратор должен выполнить /настройки.")
        event_type = await database_call(self.service.store, "event_type", guild_id, int(kind)) if kind.isascii() and kind.isdigit() else None
        if not event_type:
            raise UserError("Выберите активный вид МП из подсказок.")
        if event_type["win_enabled"]:
            if outcome not in ("win", "no_win"):
                raise UserError("Для этого вида МП выберите итог: Победа или Без победы.")
        elif outcome is not None:
            raise UserError("Выбор итога для этого вида МП отключён руководством.")
        if mvp and (not event_type["mvp_bonus"] or mvp.bot):
            raise UserError("Для MVP нужна настроенная награда; выбрать бота нельзя.")
        draft = Draft(
            guild_id=guild_id, organizer_id=str(interaction.user.id), channel_id=settings["channel_id"],
            type_id=event_type["id"], type_name=event_type["name"], points=event_type["points"],
            occurred_at=parse_event_date(date, now=created_at), note=note or "",
            outcome=outcome or "", win_bonus=event_type["win_bonus"] if outcome == "win" else 0,
            mvp_id=str(mvp.id) if mvp else None, mvp_bonus=event_type["mvp_bonus"] if mvp else 0,
        )
        if voice is not None:
            # Снимок присутствующих на момент создания; боты не получают баллы.
            if not voice.permissions_for(interaction.user).view_channel:
                raise UserError("У вас нет доступа к выбранному голосовому каналу.")
            draft.voice_channel_id = str(voice.id)
            draft.members = list(dict.fromkeys(str(member.id) for member in voice.members if not member.bot))
            if len(draft.members) > 100:
                raise UserError("В голосовом канале больше 100 участников. Выберите состав вручную.")
        view = await self.service.new_view_async(draft)
        view.bind(interaction)
        await interaction.edit_original_response(**view.payload())

    @create_report.autocomplete("kind")
    async def report_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.type_choices(interaction, current)

    @create_report.autocomplete("date")
    async def date_autocomplete(self, interaction: discord.Interaction, current: str):
        today = datetime.now(MOSCOW).strftime("%d.%m.%Y")
        choices = [app_commands.Choice(name=f"Сегодня ({today}) — время при создании отчёта", value="сегодня")]
        # Автодополнение оставляет свободный ввод. Корректную ручную дату тоже можно выбрать.
        if current and current.strip().casefold() != "сегодня":
            try:
                parse_event_date(current)
            except UserError:
                pass
            else:
                choices.append(app_commands.Choice(name=f"Вручную: {current}", value=current))
        return choices

    @app_commands.command(name="виды", description="Показать активные виды МП и очки")
    async def list_types(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        types = await database_call(self.service.store, "types", str(interaction.guild_id))
        content = "\n".join(f"{escape_markdown(kind['name'])} — {kind['points']} очк.; итог: {'вкл' if kind['win_enabled'] else 'выкл'}, победа +{kind['win_bonus']}, MVP +{kind['mvp_bonus']}" for kind in types)
        if len(content) <= 1800:
            await interaction.edit_original_response(content=content or "Виды МП ещё не настроены. Руководство может добавить их через /мп вид-добавить.")
        else:
            raw = "\n".join(f"{kind['name']} — {kind['points']} очк." for kind in types)
            await interaction.edit_original_response(content="Список видов МП:", attachments=[discord.File(BytesIO(raw.encode("utf-8")), filename="event-types.txt")])

    @app_commands.command(name="вид-добавить", description="Добавить вид МП или изменить его ставки", extras={"access": "manager"})
    @app_commands.rename(name="название", points="очки", win="очки-победы", mvp="очки-mvp")
    @app_commands.describe(name="Выберите существующее название для изменения или введите новое",
                           points="Базовые очки; обязательны для нового вида, иначе сохраняются",
                           win="Бонус каждому за победу; 0 отключает итог, пропуск сохраняет ставку",
                           mvp="Дополнительные очки одному MVP; 0 отключает, пропуск сохраняет ставку")
    async def add_type(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 80],
                       points: app_commands.Range[int, 0, 1000000] | None = None,
                       win: app_commands.Range[int, 0, 1000000] | None = None,
                       mvp: app_commands.Range[int, 0, 1000000] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, self.service.store, "manager")
        kind = await database_call(self.service.store, "set_type", str(interaction.guild_id),
                                   str(interaction.user.id), name, points, win, mvp)
        await interaction.edit_original_response(content=(
            f"Вид МП «{escape_markdown(kind['name'])}» сохранён (ID {kind['id']}). "
            f"Базовые очки: {kind['points']}; победа: +{kind['win_bonus']}; MVP: +{kind['mvp_bonus']}. "
            "Ставки применяются к новым черновикам; открытые черновики и прошлые отчёты не меняются."))

    @add_type.autocomplete("name")
    async def type_name_autocomplete(self, interaction: discord.Interaction, current: str):
        # Возвращаем точное имя, чтобы редактирование не создавало новый вид.
        try:
            types = await asyncio.wait_for(database_call(self.service.store, "types", str(interaction.guild_id)), timeout=1)
        except TimeoutError:
            return []
        return [app_commands.Choice(name=kind["name"], value=kind["name"])
                for kind in types if current.casefold() in kind["name"].casefold()][:25]

    @app_commands.command(name="вид-отключить", description="Скрыть вид МП, сохранив историю", extras={"access": "manager"})
    @app_commands.rename(kind="вид")
    @app_commands.describe(kind="Выберите вид МП из подсказок")
    async def disable_type(self, interaction: discord.Interaction, kind: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, self.service.store, "manager")
        if not kind.isascii() or not kind.isdigit():
            raise UserError("Выберите активный вид МП из подсказок.")
        await database_call(self.service.store, "disable_type", str(interaction.guild_id), str(interaction.user.id), int(kind))
        await interaction.edit_original_response(content="Вид МП отключён. История начислений сохранена.")

    @disable_type.autocomplete("kind")
    async def disable_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.type_choices(interaction, current)

    @app_commands.command(name="просмотр", description="Посмотреть сохранённый отчёт")
    @app_commands.rename(number="номер")
    @app_commands.describe(number="Номер отчёта")
    async def view_report(self, interaction: discord.Interaction, number: app_commands.Range[int, 1]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await database_call(self.service.store, "report", str(interaction.guild_id), number)
        await interaction.edit_original_response(embed=report_embed(report), allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="статистика", description="Очки участников за всё время и текущую неделю", extras={"access": "admin"})
    @app_commands.rename(page="страница")
    @app_commands.describe(page="По 20 участников на страницу, сортировка по общим очкам")
    async def points_statistics(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 10000] = 1):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Проверяем именно право администратора Discord: роль руководства сама по себе не даёт доступ.
        await require_access_async(interaction, self.service.store, "admin")
        period = week_range()
        rows = await database_call(self.service.store, "points_summary", str(interaction.guild_id), period, limit=21, offset=(page - 1) * 20)
        has_next = len(rows) > 20
        rows = rows[:20]
        lines = [f"{index}. <@{row['user_id']}> — всего **{row['total_points']}**, неделя **{row['week_points']}**"
                 for index, row in enumerate(rows, start=(page - 1) * 20 + 1)]
        if not lines:
            lines = ["Отчётов с участниками пока нет." if page == 1 else "На этой странице участников нет. Укажите меньший номер страницы."]
        embed = discord.Embed(title="МП • очки участников", colour=0x7289DA,
            description=f"**Текущая неделя:** {display_date(period[0])} — {display_date(period[1])} (Москва, конец не включён).\n"
            "Всего — очки за всё время. Отменённые отчёты не учитываются.\n\n" + "\n".join(lines))
        footer = f"Страница {page} • Сортировка по общим очкам"
        if has_next:
            footer += f" • Дальше: /мп статистика страница:{page + 1}"
        embed.set_footer(text=footer)
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="выплата-премии", description="Рассчитать премии за неделю пропорционально баллам", extras={"access": "admin"})
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(bank="банк-денег", week="неделя")
    @app_commands.describe(bank="Банк денег в целых долларах: распределяется весь между участниками",
                           week="Текущая или прошлая неделя по Москве; по умолчанию прошлая")
    @app_commands.choices(week=WEEKS)
    async def payout(self, interaction: discord.Interaction,
                     bank: app_commands.Range[int, 1, 1000000000], week: int = -1):
        await send_payout(interaction, self.service.store, bank, week)

    @app_commands.command(name="отменить", description="Отменить отчёт и убрать начисление", extras={"access": "manager"})
    @app_commands.rename(number="номер", reason="причина")
    @app_commands.describe(number="Номер отчёта", reason="Почему отчёт отменён")
    async def cancel_report(self, interaction: discord.Interaction, number: app_commands.Range[int, 1], reason: app_commands.Range[str, 1, 300]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, self.service.store, "manager")
        report = await database_call(self.service.store, "cancel_report", str(interaction.guild_id), str(interaction.user.id), number, reason)
        published = await self.service.update_cancelled(interaction.guild, report)
        content = f"Отчёт #{number} отменён, очки исключены из статистики."
        if not published:
            content += " Публикацию обновить не удалось. Повторите /мп отменить с тем же номером: очки повторно не изменятся, исходная причина сохранится."
        await interaction.edit_original_response(content=content)


@app_commands.guild_only()
class StatisticsCommands(app_commands.Group):
    def __init__(self, service: EventService):
        super().__init__(name="статистика", description="Недельный учёт участия в МП")
        self.service = service

    async def show(self, interaction: discord.Interaction, week: int, personal: bool, page: int = 1):
        await interaction.response.defer(ephemeral=True, thinking=True)
        period = week_range(week)
        rows = await database_call(self.service.store, "statistics", str(interaction.guild_id), period, str(interaction.user.id) if personal else None, 20, (page - 1) * 20)
        heading = "Ваше участие" if personal else f"Рейтинг • страница {page}"
        content = f"**{heading}**\n{display_date(period[0])} — {display_date(period[1])} (Москва, конец не включён)\n"
        for index, row in enumerate(rows, start=(page - 1) * 20 + 1):
            prefix = "" if personal else f"{index}. "
            content += f"{prefix}<@{row['user_id']}> — **{row['points']}** очк., МП: {row['events']}\n"
        if not rows:
            content += "Начислений за этот период нет."
        await interaction.edit_original_response(content=content, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="моя", description="Ваши очки и количество МП")
    @app_commands.rename(week="неделя")
    @app_commands.describe(week="Период по московскому времени")
    @app_commands.choices(week=WEEKS)
    async def personal(self, interaction: discord.Interaction, week: int = 0):
        await self.show(interaction, week, True)

    @app_commands.command(name="рейтинг", description="Рейтинг участников семьи")
    @app_commands.rename(week="неделя", page="страница")
    @app_commands.describe(week="Период по московскому времени", page="По 20 участников на страницу")
    @app_commands.choices(week=WEEKS)
    async def ranking(self, interaction: discord.Interaction, week: int = 0, page: app_commands.Range[int, 1, 10000] = 1):
        await self.show(interaction, week, False, page)


def install_commands(tree: app_commands.CommandTree, service: EventService, guild_id: int):
    """Команды устанавливаются локально; публикация в Discord выполняется отдельно deploy.py."""
    guild = discord.Object(id=guild_id)
    tree.add_command(EventCommands(service), guild=guild)
    tree.add_command(StatisticsCommands(service), guild=guild)

    @tree.command(name="настройки", description="Начальная настройка бота семьи", guild=guild, extras={"access": "admin"})
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал публикации отчётов; роли выбираются в меню после команды")
    async def settings(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, "admin")
        await service.channel(interaction.guild, str(channel.id))
        current = await database_call(service.store, "settings", str(interaction.guild_id))
        view = SettingsView(service, interaction, channel, current)
        await interaction.edit_original_response(**view.payload())

    # Discord требует подкоманды у группы: общая справка теперь /инфо общее.
    info_group = app_commands.Group(name="инфо", description="Справка по командам семьи и фарму", guild_only=True)
    tree.add_command(info_group, guild=guild)

    @info_group.command(name="общее", description="Все команды, права ролей и доступные вам действия")
    @app_commands.guild_only()
    async def info_command(interaction: discord.Interaction):
        await send_info(interaction, tree, service.store)

    @info_group.command(name="фарм", description="Цены скупщиков, подписки на товары и уведомления")
    @app_commands.guild_only()
    async def farm_info_command(interaction: discord.Interaction):
        await send_farm_info(interaction, service.store)

    @tree.command(name="помощь", description="Общая справка по командам и правам (как /инфо общее)", guild=guild)
    @app_commands.guild_only()
    async def help_command(interaction: discord.Interaction):
        await send_info(interaction, tree, service.store)


    @tree.command(name="статус", description="Сообщить в текущий чат, что бот запущен и готов к работе", guild=guild, extras={"access": "all"})
    @app_commands.guild_only()
    async def status_command(interaction: discord.Interaction):
        # Публичный ответ отправляется только по команде участника, без массовых упоминаний.
        await interaction.response.send_message(
            "🟢 **LaFamily Bot запущен и готов к работе!**\n"
            f"**Версия:** {VERSION}\n"
            f"**Последнее обновление:** {LATEST_UPDATE}\n\n"
            "Команды и права: `/инфо общее` · Скупщики и подписки: `/инфо фарм`.",
            allowed_mentions=discord.AllowedMentions.none())


    @tree.command(name="старт", description="Короткое знакомство с ботом: первые шаги для новичка", guild=guild, extras={"access": "all"})
    @app_commands.guild_only()
    async def start_command(interaction: discord.Interaction):
        # Личное приветствие без административных инструкций и полного каталога команд.
        embed = discord.Embed(title="👋 Добро пожаловать в LaFamily!", colour=0x43B581,
            description="Я помогаю учитывать участие в жизни семьи и находить выгодные цены. Вот с чего начать:")
        embed.add_field(name="1 · Приведи ник в порядок", value=
            "Формат: `Name Surname | Static ID`.\nПример: `Sin Bertoni | 15096`. Должность перед именем допустима.", inline=False)
        embed.add_field(name="2 · Участвуй и получай баллы", value=
            "Ходи на МП — организатор добавит тебя в отчёт. Самостоятельно подавать отчёт об участии не нужно.\n"
            "Собрал коровник? Введи `/коровник-сбор` и укажи сумму, например `+157к`. "
            "Без выбора участника баллы получишь ты.", inline=False)
        embed.add_field(name="3 · Посмотри свои результаты", value=
            "`/статистика моя` — твои баллы за выбранную неделю.\n"
            "`/статистика рейтинг` — результаты участников семьи.", inline=False)
        embed.add_field(name="4 · Лови выгодные цены", value=
            "`/скупщик-уведомление` → отметь нужные товары → **Подтвердить**.\n"
            "Бот тегнет тебя в канале цен при достижении справочного максимума обычной цены. "
            "В той же команде можно изменить подписки или отключить их.", inline=False)
        embed.add_field(name="Если хочется разобраться подробнее", value=
            "🌾 `/инфо фарм` — цены и подписки.\n"
            "📖 `/инфо общее` или `/помощь` — все команды и права.\n"
            "Не хватает доступа? Обратись к руководству семьи.", inline=False)
        embed.set_footer(text="Это сообщение видишь только ты. Вернуться к нему можно командой /старт.")
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
