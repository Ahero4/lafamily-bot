"""Общая справка: описания берутся из команд, права — из тех же проверок, что при выполнении."""

import discord
from types import SimpleNamespace
from discord import app_commands

from lafamily.database import Store
from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.access_roles import role_ids
from lafamily.permissions import require_access


def available(interaction: discord.Interaction, store: Store, level: str) -> bool:
    """Справка не вычисляет права отдельно от бота: используем действующую проверку."""
    if level == "all":
        return interaction.guild is not None
    try:
        require_access(interaction, store, level)
    except UserError:
        return False
    return True


def role_label(interaction: discord.Interaction, role_id: str | None) -> str:
    if not role_id:
        return "роль не назначена"
    role = interaction.guild.get_role(int(role_id)) if role_id.isdigit() else None
    return role.mention if role else "назначенная роль удалена или недоступна"


def build_info(interaction: discord.Interaction, tree: app_commands.CommandTree, store: Store) -> discord.Embed:
    settings = store.settings(str(interaction.guild_id)) or {}
    organizer = ", ".join(dict.fromkeys(role_label(interaction, role) for role in role_ids(settings, "organizer"))) or "роль не назначена"
    manager = ", ".join(dict.fromkeys(role_label(interaction, role) for role in role_ids(settings, "manager"))) or "роль не назначена"
    if available(interaction, store, "admin"):
        personal = "У вас права **администратора Discord** — доступны все команды бота."
    elif available(interaction, store, "manager"):
        personal = f"Ваш доступ: **руководство** ({manager}). Можно управлять МП, подавать отчёты и настраивать канал цен."
    elif available(interaction, store, "organizer"):
        personal = f"Ваш доступ: **организатор** ({organizer}). Можно подавать отчёты о МП."
    else:
        personal = "Ваш доступ: **участник**. Можно смотреть отчёты, виды МП и статистику, выбирать товары для уведомлений о ценах."

    embed = discord.Embed(title="LaFamily • команды и права", colour=0x7289DA,
        description=personal + "\n\n🌾 Цены и подписки: `/инфо фарм`\n\n✅ — доступно вам · 🔒 — требуется другая роль.\n"
        "`<параметр>` — обязательный, `[параметр]` — необязательный. Значения выбираются в полях команды Discord.")
    groups = {
        "all": ("Все участники", "Любой участник сервера."),
        "organizer": ("Подача отчётов", "Любая роль из списков «Организаторы» и «Руководство» ниже; администратор Discord."),
        "manager": ("Управление МП и ценами", "Любая роль из списка «Руководство» ниже; администратор Discord."),
        "admin": ("Администратор", "Только участники с правом «Администратор» в Discord."),
    }
    commands = {level: [] for level in groups}
    # Обходим именно зарегистрированные команды, чтобы новые описания не дублировать вручную.
    for command in tree.walk_commands(guild=discord.Object(id=interaction.guild_id)):
        if not isinstance(command, app_commands.Command):
            continue
        level = command.extras.get("access", "all")
        parameters = " ".join(
            f"<{param.display_name}>" if param.required else f"[{param.display_name}]"
            for param in command.parameters
        )
        usage = f"/{command.qualified_name}" + (f" {parameters}" if parameters else "")
        commands[level].append(f"`{usage}`\n{command.description}")

    for level, (title, audience) in groups.items():
        status = "✅" if available(interaction, store, level) else "🔒"
        # Поле Discord ограничено 1024 символами; длинную группу делим на продолжения.
        value = f"**Кто может:** {audience}\n\n"
        for line in sorted(commands[level]):
            if len(value) + len(line) + 2 > 1000:
                embed.add_field(name=f"{status} {title}", value=value.rstrip(), inline=False)
                value = ""
            value += line + "\n\n"
        embed.add_field(name=f"{status} {title}", value=value.rstrip(), inline=False)

    embed.add_field(name="Организаторы", value=organizer, inline=False)
    embed.add_field(name="Руководство", value=manager, inline=False)
    if not settings:
        embed.add_field(name="Первый запуск", value="Роли ещё не настроены. Администратору нужно выполнить `/настройки`.", inline=False)
    embed.add_field(name="📝 Отчёт по МП", value=
        "**1.** `/мп отчёт` — выберите вид и дату.\n"
        "**2.** Отметьте участников и подтвердите — бот начислит очки.\n"
        "Автор и руководство могут менять состав, результат и MVP открытого черновика и подтверждать его. Другие организаторы чужим черновиком не управляют.\n"
        "Руководство задаёт ставки через `/мп вид-добавить`: очки, очки-победы, очки-mvp. Повтор того же названия обновляет вид, пропущенные ставки сохраняются. Победа добавляет очки всем, MVP — ещё выбранному участнику.\n"
        "Ошибка в отчёте? Руководство отменяет его, организатор подаёт новый.", inline=False)
    embed.add_field(name="💰 Недельные премии", value=
        "Администратор: `/мп выплата-премии банк-денег [неделя]`. По умолчанию прошлая неделя. "
        "Банк распределяется по всем положительным баллам, включая коровник и бонусы. "
        "Остаток округления распределяется целыми долларами. Ответ личный; это расчёт без списания баллов и перевода денег.", inline=False)
    embed.add_field(name="🐄 Сбор коровника", value=
        "`/коровник-сбор сумма [участник]` — укажите сумму, например `157000` или `+157к`.\n"
        "Без выбора участника баллы получает автор команды. Баллы за один сбор задаёт администратор через "
        "`/коровник-настройка баллы`. Сборы входят в общий учёт; отменить ошибочный сбор можно через `/мп отменить` по номеру отчёта.", inline=False)
    embed.add_field(name="🌾 Фарм и скупщики", value=
        "Откройте `/инфо фарм`: там объяснены значки цен, подписки на товары и уведомления.", inline=False)
    embed.add_field(name="🔎 Проверка ников", value=
        "Администратор запускает `/проверка-ника [роль]`. Без роли проверяются все участники, кроме ботов.\n"
        "Формат: `Name Surname | Static ID`. Должность перед именем допустима.\n"
        "Тегирование нарушителей — только после подтверждения.", inline=False)
    embed.set_footer(text="Неделя: с понедельника 00:00 по Москве. Дополнительные ограничения команд могут задаваться настройками Discord.")
    return embed


async def send_info(interaction: discord.Interaction, tree: app_commands.CommandTree, store: Store):
    # Ответ виден только вызвавшему участнику; упоминания ролей никого не уведомляют.
    await interaction.response.defer(ephemeral=True, thinking=True)
    settings = await database_call(store, "settings", str(interaction.guild_id))
    snapshot = SimpleNamespace(settings=lambda guild_id: settings)
    await interaction.edit_original_response(embed=build_info(interaction, tree, snapshot),
                                            allowed_mentions=discord.AllowedMentions.none())


def build_farm_info(price_settings=None):
    """Отдельная короткая памятка для фарма: один раздел — одно действие."""
    channel = f'<#{price_settings["channel_id"]}>' if price_settings else 'канал ещё не выбран руководством'
    embed = discord.Embed(title="🌾 LaFamily • фарм и скупщики", colour=0x43B581,
        description=f'**Канал цен:** {channel}\nСмотрите выгодные предложения и включайте уведомления по нужным товарам.')
    embed.add_field(name="🔔 Как подписаться", value=
        "**1.** Введите `/скупщик-уведомление`.\n"
        "**2.** Отметьте несколько товаров в списке.\n"
        "**3.** Нажмите **Подтвердить**.\n"
        "Команда доступна всем участникам с доступом к каналу цен. Меню видите только вы.", inline=False)
    embed.add_field(name="✏️ Изменить или отключить подписки", value=
        "Откройте ту же команду — ваши товары уже отмечены.\n"
        "Снимите лишние отметки и подтвердите. Пустой выбор отключает всё.\n"
        "**Отмена** оставляет подписки без изменений. Меню действует **10 минут**.", inline=False)
    embed.add_field(name="📣 Когда бот тегнет вас", value=
        "По выбранному режиму обычной цены: **95 - 100** или **Макс. цена**. Порог можно задать всем выбранным товарам или отдельному товару. "
        "Для продуктов проверяется единственный скупщик.\n"
        "Проверка начинается со следующего нового автоматического отчёта.\n"
        "Повторный тег — после выхода цены из выбранного условия и нового входа. "
        "Подписки сохраняются после перезапуска.", inline=False)
    embed.add_field(name="💰 Что означают значки", value=
        "✅ + **жирный шрифт** — лучшая цена среди трёх скупщиков, даже если до максимума ещё далеко. "
        "При равенстве отмечаются все.\n"
        "🏆 — **95–100%** справочного максимума.\n"
        "⚠️ — **90–95%**, не включая 95%.\n"
        "Значки могут сочетаться. В скобках — цена **+20%**; подписка срабатывает по обычной цене.", inline=False)
    embed.add_field(name="📦 Где искать свой товар", value=
        "**Охота** — шкура, идеальное и хорошее мясо.\n"
        "**Горщик** — медь, изумруд, рубин, алмаз.\n"
        "**Рыбалка** — окунь, карп, форель, лосось.\n"
        "**Нефтекачка** — бензин, солярка, керосин.\n"
        "**Остальное** — жемчуг, фальшивые деньги.\n"
        "**Продукты** — капуста, тыква, мандарины, ананасы.", inline=False)
    embed.add_field(name="🛠️ Команды руководства", value=
        "`/скупщик-настройка канал`\nВыбрать канал для автоматического сравнения. Исходные сообщения сохраняются.\n\n"
        "`/скупщик-обработать сообщение`\nУказать ID исходного сообщения, чтобы создать или обновить сравнение. "
        "Ручная обработка не тегает подписчиков.\n\n"
        "Обе команды доступны **руководству и администратору Discord**.", inline=False)
    embed.set_footer(text="Цены меняются в течение часа. Общие команды и права: /инфо общее или /помощь.")
    return embed


async def send_farm_info(interaction, store):
    # Ответ личный: справка и упоминание канала не вызывают никаких уведомлений.
    await interaction.response.defer(ephemeral=True, thinking=True)
    settings = await database_call(store, "price_settings", str(interaction.guild_id))
    await interaction.edit_original_response(embed=build_farm_info(settings),
                                            allowed_mentions=discord.AllowedMentions.none())
