"""Личная справка: публичные команды, отдельный справочник управления и памятка фарма."""
import asyncio
from types import SimpleNamespace

import discord
from discord import app_commands

from lafamily.access_roles import role_ids
from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.info_guides import COMMAND_GUIDES
from lafamily.permissions import require_access, require_access_async
from lafamily.responses import send_error

ACCESS_LABELS = {'all': 'все участники', 'organizer': 'организаторы, руководство и администраторы',
                 'manager': 'руководство и администраторы', 'admin': 'только администратор Discord'}
SYNTAX = '`<параметр>` — обязательный, `[параметр]` — необязательный. Значения выбирайте в полях команды Discord.'


def available(interaction, store, level):
    if level == 'all':
        return interaction.guild is not None
    try:
        require_access(interaction, store, level)
    except UserError:
        return False
    return True


def role_label(interaction, role_id):
    if not role_id:
        return 'роль не назначена'
    role = interaction.guild.get_role(int(role_id)) if role_id.isdigit() else None
    return role.mention if role else 'назначенная роль удалена или недоступна'


def command_usage(command):
    parameters = ' '.join(f'<{p.display_name}>' if p.required else f'[{p.display_name}]' for p in command.parameters)
    return f'/{command.qualified_name}' + (f' {parameters}' if parameters else '')


def _command_pages(interaction, tree, store, administrative=False):
    settings = store.settings(str(interaction.guild_id)) or {}
    organizers = ', '.join(dict.fromkeys(role_label(interaction,r) for r in role_ids(settings,'organizer'))) or 'роль не назначена'
    managers = ', '.join(dict.fromkeys(role_label(interaction,r) for r in role_ids(settings,'manager'))) or 'роль не назначена'
    levels = ('manager','admin') if administrative else ('all','organizer')
    title = 'LaFamily • руководство и администраторы' if administrative else 'LaFamily • команды и права'
    if available(interaction,store,'admin'):
        personal = 'Ваш доступ: **администратор Discord**.'
    elif available(interaction,store,'manager'):
        personal = 'Ваш доступ: **руководство**.'
    elif available(interaction,store,'organizer'):
        personal = 'Ваш доступ: **организатор**.'
    else:
        personal = 'Ваш доступ: **участник**.'
    navigation = ('Для участников: `/инфо общее`; цены и подписки: `/инфо фарм`.' if administrative else
                  '🌾 Цены и подписки: `/инфо фарм`.\nНастройки и управление для руководства и администраторов: `/инфо админ`.')
    description = f'{personal}\n📋 Все команды без описаний: `/команды`.\n{navigation}\n\n✅ — доступно вам · 🔒 — нужна другая роль.\n{SYNTAX}'
    def new_page():
        return discord.Embed(title=title,description=description,colour=0x7289DA)
    pages = []
    page = new_page()
    page.add_field(name='Организаторы',value=organizers,inline=False)
    page.add_field(name='Руководство',value=managers,inline=False)
    if not settings:
        page.add_field(name='Первый запуск',value='Роли ещё не настроены. Обратитесь к администратору сервера.',inline=False)
    commands = [c for c in tree.walk_commands(guild=discord.Object(id=interaction.guild_id))
                if isinstance(c,app_commands.Command) and c.extras.get('access','all') in levels]
    commands.sort(key=lambda c: (levels.index(c.extras.get('access','all')),c.qualified_name))
    for command in commands:
        level = command.extras.get('access','all')
        status = '✅' if available(interaction,store,level) else '🔒'
        name = f'{status} /{command.qualified_name}'
        guide = COMMAND_GUIDES.get(command.qualified_name,'Выберите параметры в полях команды Discord.')
        value = f'`{command_usage(command)}`\n{command.description}.\n**Доступ:** {ACCESS_LABELS[level]}.\n{guide}'
        # Один embed на страницу: оставляем запас для подвала и ограничений Discord.
        if len(page) + len(name) + len(value) > 5400 or len(page.fields) >= 24:
            pages.append(page)
            page = new_page()
        page.add_field(name=name,value=value,inline=False)
    pages.append(page)
    for index, embed in enumerate(pages,1):
        embed.set_footer(text=f'Страница {index}/{len(pages)} · Все даты по Москве. Справка личная; кнопки действуют 10 минут.')
    return pages


def build_info_pages(interaction, tree, store):
    return _command_pages(interaction,tree,store)


def build_info(interaction, tree, store):
    return build_info_pages(interaction,tree,store)[0]


def build_admin_info_pages(interaction, tree, store):
    return _command_pages(interaction,tree,store,administrative=True)


class HelpPages(discord.ui.View):
    def __init__(self, original, pages):
        super().__init__(timeout=600)
        self.original, self.pages = original, pages
        self.owner_id, self.guild_id = original.user.id, original.guild_id
        self.index = 0
        self.lock = asyncio.Lock()
        self.update_buttons(0)

    def update_buttons(self, index):
        self.previous.disabled = index == 0
        self.next_page.disabled = index == len(self.pages) - 1
        self.counter.label = f'{index+1} / {len(self.pages)}'

    def check_owner(self, interaction):
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            raise UserError('Это чужая справка. Откройте /инфо для себя.')
        if self.is_finished():
            raise UserError('Справка закрыта. Откройте нужный раздел /инфо заново.')

    async def interaction_check(self, interaction):
        try:
            self.check_owner(interaction)
        except UserError as error:
            await send_error(interaction,error)
            return False
        return True

    async def on_error(self, interaction, error, item):
        await send_error(interaction,error)

    async def move(self, interaction, delta):
        async with self.lock:
            self.check_owner(interaction)
            target = max(0,min(len(self.pages)-1,self.index+delta))
            self.update_buttons(target)
            try:
                await interaction.response.edit_message(embed=self.pages[target],view=self,
                                                        allowed_mentions=discord.AllowedMentions.none())
            except BaseException:
                self.update_buttons(self.index)
                raise
            self.index = target

    @discord.ui.button(label='Назад',style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self.move(interaction,-1)

    @discord.ui.button(label='1 / 1',disabled=True,style=discord.ButtonStyle.secondary)
    async def counter(self, interaction, button):
        pass

    @discord.ui.button(label='Далее',style=discord.ButtonStyle.primary)
    async def next_page(self, interaction, button):
        await self.move(interaction,1)

    async def on_timeout(self):
        async with self.lock:
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await self.original.edit_original_response(view=self)
            except discord.HTTPException:
                pass


async def _send_pages(interaction, pages):
    view = HelpPages(interaction,pages) if len(pages) > 1 else None
    try:
        await interaction.edit_original_response(embed=pages[0],view=view,
                                                 allowed_mentions=discord.AllowedMentions.none())
    except BaseException:
        if view:
            view.stop()
        raise


async def send_info(interaction, tree, store):
    await interaction.response.defer(ephemeral=True,thinking=True)
    settings = await database_call(store,'settings',str(interaction.guild_id))
    snapshot = SimpleNamespace(settings=lambda guild_id: settings)
    await _send_pages(interaction,build_info_pages(interaction,tree,snapshot))


async def send_admin_info(interaction, tree, store):
    await interaction.response.defer(ephemeral=True,thinking=True)
    await require_access_async(interaction,store,'manager')
    settings = await database_call(store,'settings',str(interaction.guild_id))
    snapshot = SimpleNamespace(settings=lambda guild_id: settings)
    await _send_pages(interaction,build_admin_info_pages(interaction,tree,snapshot))


def build_farm_info(price_settings=None):
    channel = f'<#{price_settings["channel_id"]}>' if price_settings else 'канал ещё не выбран руководством'
    embed = discord.Embed(title='🌾 LaFamily • фарм и скупщики',colour=0x43B581,
        description=f'**Канал цен:** {channel}\nСравнение цен находится в канале, уведомления по вашим товарам приходят в личные сообщения Discord (DM).')
    embed.add_field(name='🔔 /скупщик подписки — включить подписки',value=
        '**1.** Откройте `/скупщик подписки` и отметьте нужные товары в первом списке.\n'
        '**2.** Во втором списке выберите, для кого менять режим: все выбранные товары или один товар. Само это поле режим не меняет.\n'
        '**3.** В третьем списке задайте «95 - 100» или «Макс. цена». Например: всем товарам диапазон, а меди — максимум.\n'
        '**4.** Проверьте список и нажмите **Подтвердить**. До подтверждения ничего не сохраняется.\n'
        'Нужен доступ к каналу цен. Меню видите только вы; оно действует 10 минут.',inline=False)
    embed.add_field(name='🎯 Как работают режимы',value=
        '**95 - 100** — лучшая обычная цена от **95–100%** справочного максимума включительно; выше 100% этот режим не срабатывает.\n'
        '**Макс. цена** — обычная цена достигла максимума или выше.\n'
        'Для сырья берётся лучшая цена среди трёх скупщиков, для продуктов — единственный продавец. Бонус +20% порог не меняет.',inline=False)
    embed.add_field(name='📩 Где приходит уведомление',value=
        'Каждый подписчик получает личное сообщение с товаром, ценой, скупщиком и ссылкой на отчёт. Разрешите ЛС от участников этого сервера.\n'
        'При закрытых ЛС сообщение доставить нельзя; подписка при этом сохраняется. Ошибка одного человека не мешает другим. '
        'Уведомления подписчиков в общий канал не отправляются. Успех отправки не означает прочтение.',inline=False)
    embed.add_field(name='🔁 Когда ждать следующее уведомление',value=
        'Проверка начинается со следующего нового автоматического отчёта. Внутри выбранного условия повторов нет. '
        'Новый цикл — после выхода цены из условия и нового входа. Рост с 95% до 100% не вызывает второй сигнал в режиме диапазона. '
        'Ручная обработка не уведомляет подписчиков. Подписки сохраняются после перезапуска.',inline=False)
    embed.add_field(name='✏️ Изменить или отключить подписки',value=
        'Снова откройте `/скупщик подписки`: сохранённые товары уже отмечены. Снимите лишние отметки или измените режим и подтвердите. '
        '**Пустой выбор + Подтвердить** отключает все подписки. **Отмена** и истечение меню оставляют прежние настройки.',inline=False)
    embed.add_field(name='💰 Значки в сравнении цен',value=
        '✅ и **жирный** — лучшая цена среди трёх скупщиков; при равенстве выделяются все.\n'
        '🏆 — **95–100%** справочного максимума. ⚠️ — **90–95%**, не включая 95%.\n'
        'В скобках — бонусная цена **+20%**. Для уведомлений проверяется обычная цена.',inline=False)
    embed.add_field(name='📦 Где искать свой товар',value=
        '**Охота:** шкура, идеальное и хорошее мясо. **Горщик:** медь, изумруд, рубин, алмаз.\n'
        '**Рыбалка:** окунь, карп, форель, лосось. **Нефтекачка:** бензин, солярка, керосин.\n'
        '**Остальное:** жемчуг, фальшивые деньги. **Продукты:** капуста, тыква, мандарины, ананасы.',inline=False)
    embed.add_field(name='🐄 /коровник сбор — записать сбор',value=
        '`/коровник сбор сумма:+157к [участник]` — запишите сумму (от 1 до 1 000 000 000). '
        'Без участника баллы получит автор команды; можно выбрать другого человека. Награда за сбор фиксированная. '
        'Свои результаты смотрите в `/баллы мои`, семейный рейтинг — в `/баллы рейтинг`.',inline=False)
    embed.add_field(name='📚 Другие разделы',value=
        '`/инфо общее` или `/помощь` — команды участников и организаторов. '
        'Руководству и администраторам: `/инфо админ` — настройки скупщиков, служебные отчёты и выгрузка истории доставки.',inline=False)
    embed.set_footer(text='Справка личная. Цены сравниваются с локальным справочником максимумов.')
    return embed


async def send_farm_info(interaction, store):
    await interaction.response.defer(ephemeral=True,thinking=True)
    settings = await database_call(store,'price_settings',str(interaction.guild_id))
    await interaction.edit_original_response(embed=build_farm_info(settings),allowed_mentions=discord.AllowedMentions.none())


COMMAND_SECTIONS = {
    'мп': 'Мероприятия', 'баллы': 'Баллы и премии', 'коровник': 'Коровник',
    'скупщик': 'Скупщик', 'заявки': 'Вступление', 'ники': 'Проверка ников',
    'бот': 'Бот', 'анонс': 'Анонсы', 'инфо': 'Справка', 'старт': 'Первое знакомство',
    'команды': 'Быстрый доступ', 'помощь': 'Быстрый доступ',
}


def build_command_list(tree, guild_id):
    """Только полные названия, включая служебные команды, без фильтрации по правам."""
    sections = {}
    for command in tree.walk_commands(guild=discord.Object(id=guild_id)):
        if isinstance(command, app_commands.Command):
            root = command.qualified_name.split()[0]
            section = COMMAND_SECTIONS.get(root, root)
            sections.setdefault(section, []).append(command.qualified_name)
    pages = []
    page = discord.Embed(title='LaFamily • список команд', colour=0x7289DA)
    order = list(dict.fromkeys(COMMAND_SECTIONS.values()))
    for section in sorted(sections, key=lambda s: (order.index(s) if s in order else len(order), s)):
        chunks = ['']
        for name in sorted(sections[section]):
            line = f'`/{name}`\n'
            if len(chunks[-1]) + len(line) > 1000:
                chunks.append('')
            chunks[-1] += line
        for chunk in chunks:
            if len(page) + len(section) + len(chunk) > 5400 or len(page.fields) >= 24:
                pages.append(page)
                page = discord.Embed(title='LaFamily • список команд', colour=0x7289DA)
            page.add_field(name=section, value=chunk.rstrip(), inline=False)
    pages.append(page)
    for i, page in enumerate(pages, 1):
        page.set_footer(text=f'{i}/{len(pages)} · Описания: /инфо общее, /инфо фарм, /инфо админ.')
    return pages


async def send_command_list(interaction, tree):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await _send_pages(interaction, build_command_list(tree, interaction.guild_id))
