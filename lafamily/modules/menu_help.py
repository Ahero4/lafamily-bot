"""Тематическая справка личного меню и выбор команд по действующим правам."""

from dataclasses import dataclass

import discord
from discord import app_commands

from lafamily.errors import UserError
from lafamily.info import ACCESS_LABELS
from lafamily.info_guides import COMMAND_GUIDES
from lafamily.permissions import check_access

COMMANDS_PER_PAGE = 8


@dataclass(frozen=True)
class HelpTopic:
    title: str
    overview: str
    prefixes: tuple[str, ...]


TOPICS = {
    'points': HelpTopic('Баллы', (
        'Участники подтверждённого отчёта МП получают базовые баллы по ставке вида. Если настроены бонусы, '
        'победа даёт дополнительные баллы всему составу, а MVP получает отдельный бонус. За сбор коровника '
        'начисляется фиксированная награда. В недельном итоге баллы учитываются по дате события. Неделя начинается '
        'в понедельник в 00:00 по Москве. Профиль показывает участия, победы и MVP только в подтверждённых отчётах '
        'из `/мп создать` с признаком команды, который сохраняется с версии 0.6.23. В истории можно открыть отчёт. Если его отменили, запись остаётся в истории, а баллы исключаются из учёта.'
    ), ('баллы', 'участник профиль')),
    'events': HelpTopic('Мероприятия', (
        'До начала МП нажмите «Участвую» в карточке записи. Если мест в основном составе нет, бот добавит '
        'вас в резерв. Если ещё не уверены, нажмите «Возможно». Сама запись баллов не даёт. В черновике отчёта '
        'нажмите «Участвовать». Если указан голосовой канал, сначала зайдите в него. Баллы начисляются '
        'после подтверждения отчёта.'
    ), ('мп',)),
    'garden': HelpTopic('Сад', (
        'В саду до 9 мест. Капуста растёт 4 PayDay, тыква 5, мандарины 6, ананасы 7. Первый PayDay новой '
        'посадки засчитывается автоматически. Дальше отмечайте обработку в текущем часу и удобрение один раз '
        'за посадку. В :50 бот напоминает о пропущенном уходе в ЛС. Сбор освобождает место для новой посадки. '
        'Если забыли отметку, выберите растущую посадку и укажите фактический остаток через «Исправить PayDay».'
    ), ('сад',)),
    'prices': HelpTopic('Скупщик', (
        'Выберите товары и порог по обычной цене. Режим «95 - 100» охватывает 95–100% от справочного максимума '
        'включительно. «Макс. цена» срабатывает при достижении справочного максимума и выше. Бот присылает цены '
        'в ЛС, когда есть доступ к каналу цен и личным сообщениям. История показывает диапазон цен по дням '
        'и позволяет выгрузить таблицу и график.'
    ), ('скупщик',)),
    'barn': HelpTopic('Коровник', (
        'Нажмите «Коровник» и введите сумму сбора, например 157000 или +157к. Можно выбрать получателя. '
        'По умолчанию баллы получает тот, кто заполнил форму. Награда фиксирована и не зависит от суммы. '
        'Карточка сбора публикуется в текущем канале.'
    ), ('коровник',)),
    'onboarding': HelpTopic('Вступление и роли', (
        'Откройте форму заявки кнопкой в канале вступления и заполните её. После одобрения бот выдаёт роль '
        'и меняет ник. Ник оформляют как Name Surname | Static ID. Если на сервере настроена выдача ролей по реакции, '
        'поставьте нужную реакцию на карточке. После снятия реакции роль будет снята.'
    ), ('старт участник', 'заявки', 'ники', 'роль')),
    'channels': HelpTopic('Каналы и настройки', (
        'Выберите каналы и роли с учётом их назначения. В разделе «Администрирование → Каналы и публикации» '
        'администратор может разместить постоянную кнопку меню и семейную памятку.'
    ), ('бот настройки', 'бот отчёты', 'бот обновления', 'скупщик настройки', 'скупщик обработать',
        'заявки настройки', 'роль выдача', 'старт админ')),
    'diagnostics': HelpTopic('Диагностика и операции', (
        'Диагностика показывает изменения состояния базы, каналов и прав. В операциях можно найти '
        'незавершённые публикации и доставки. Восстановление карточки отчёта не начисляет баллы повторно.'
    ), ('бот диагностика', 'бот операции', 'бот восстановить-операцию', 'скупщик статистика', 'ники проверить')),
    'backups': HelpTopic('Резервные копии', (
        'Для архивов выберите закрытый текстовый канал. Бот отправляет копии каждые шесть часов. '
        'При восстановлении потребуется подтверждение и страховочная копия. Данные вернутся к состоянию архива.'
    ), ('бот бэкапы',)),
    'announcements': HelpTopic('Анонсы и оповещения', (
        'Создавайте шаблоны анонсов, задавайте время разовых событий или дни еженедельных. Время указывается '
        'по Москве. Для регулярных оповещений есть отдельная настройка дней, роли и текста.'
    ), ('анонс',)),
}

PUBLIC_TOPICS = ('points', 'events', 'garden', 'prices', 'barn', 'onboarding')
SERVICE_TOPICS = ('channels', 'diagnostics', 'backups', 'announcements')


def help_commands(interaction, tree, settings):
    levels = {'all'}
    for level in ('organizer', 'manager', 'admin'):
        try:
            check_access(interaction, settings, level)
        except UserError:
            continue
        levels.add(level)
    return tuple(command for command in tree.walk_commands(guild=discord.Object(id=interaction.guild_id))
                 if isinstance(command, app_commands.Command) and not command.extras.get('hidden')
                 and command.extras.get('access', 'all') in levels)


def topic_commands(topic, commands):
    return tuple(command for command in commands if any(
        command.qualified_name == prefix or command.qualified_name.startswith(prefix + ' ')
        for prefix in topic.prefixes
    ))


def help_topic_embed(topic, commands, page):
    embed = discord.Embed(title=topic.title, description=topic.overview, colour=0x7289DA)
    first = page * COMMANDS_PER_PAGE
    for command in commands[first:first + COMMANDS_PER_PAGE]:
        path = command.qualified_name
        guide = COMMAND_GUIDES.get(path, '').split('. ', 1)[0].removesuffix('.')
        # Длинные объяснения и ссылки на команды из других разделов остаются в полной справке.
        summary = guide if guide and '/' not in guide and len(guide) <= 160 else command.description.removesuffix('.')
        level = command.extras.get('access', 'all')
        embed.add_field(name=f'/{path}', value=f'{summary}.\nДоступ — {ACCESS_LABELS[level]}.', inline=False)
    pages = max(1, (len(commands) + COMMANDS_PER_PAGE - 1) // COMMANDS_PER_PAGE)
    if pages > 1:
        embed.set_footer(text=f'Страница {page + 1}/{pages}')
    return embed
