"""Одна актуальная памятка семьи в выбранном канале."""
import asyncio

import discord

from lafamily.async_database import database_call
from lafamily.diagnostics import log_error
from lafamily.errors import UserError
from lafamily.release import VERSION


TITLE = 'LaFamily Bot · Памятка для семьи'
FOOTER = 'LaFamily · Памятка семьи'


def guide_embed():
    guide = discord.Embed(title=TITLE, colour=0x497653,
        description='Откройте `/меню` или нажмите «Открыть LaFamily». В разделе «Помощь» выберите тему, чтобы увидеть правила и команды. Там же есть памятка и каталог.')
    guide.add_field(name='Как попасть в отчёт МП', inline=False, value=(
        'Организатор открывает черновик из раздела «Мероприятия». Нажмите **Участвовать** и проверьте своё имя в составе. '
        'Если указан голосовой канал, сначала зайдите в него. В большом составе листайте страницы. '
        'Организаторы, руководство и администраторы могут убрать игрока кнопкой ❌. '
        'Баллы начисляются после подтверждения отчёта тем, кто остался в составе.'))
    guide.add_field(name='Организаторам и руководству', inline=False, value=(
        'Выберите вид МП из `/мп виды список`. В `/мп создать` дату можно пропустить: бот возьмёт время создания. '
        'Для прошедшего МП укажите его дату и время по Москве. Автор с ролью организатора, руководство или администратор '
        'проверяют состав, выбирают результат и MVP, если они предусмотрены, и нажимают **Подтвердить отчёт**.'))
    guide.add_field(name='Где проверить баллы', inline=False, value=(
        'В «Профиле» видны баллы, рейтинг и участие за неделю и за всё время. В участии учитываются только подтверждённые отчёты из `/мп создать`. '
        'Бот сохраняет признак команды с версии 0.6.23. В истории можно выбрать начисление и открыть отчёт. '
        ''
        'Если заметили ошибку, найдите номер отчёта в истории и передайте его администратору Discord. '
        'Последнее исправление видно в карточке МП. Подробности доступны через `/мп посмотреть`.'))
    guide.add_field(name='Когда начинается неделя', inline=False, value=(
        'Неделя начинается в понедельник в 00:00 по Москве. Баллы за МП относятся к неделе его проведения. '
        'Рассчитать суммы через `/баллы премии` может только администратор Discord. Руководство выплачивает премии вручную.'))
    guide.add_field(name='Сад, скупщик и коровник', inline=False, value=(
        'Откройте **Сад** в меню или введите `/сад`, чтобы проверить посадки, уход и сбор. '
        'В разделе **Скупщик** выберите товары и пороги для личных уведомлений или посмотрите историю цен. '
        'Кнопка «Коровник» сразу открывает форму записи сбора.'))
    return guide.set_footer(text=f'{FOOTER} · версия {VERSION}')


class FamilyGuide:
    def __init__(self, client):
        self.client = client
        self.guild_id = str(client.config.guild_id)
        self.lock = asyncio.Lock()

    async def db(self, method, *args):
        return await database_call(self.client.store, method, self.guild_id, *args)

    async def channel(self, channel_id):
        channel = await self.client.fetch_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel) or str(channel.guild.id) != self.guild_id:
            raise UserError('Выберите текстовый канал сервера семьи.')
        member = channel.guild.me
        perms = channel.permissions_for(member) if member else None
        if not (perms and perms.view_channel and perms.read_message_history and perms.send_messages and perms.embed_links):
            raise UserError('Для памятки боту нужны просмотр канала, чтение истории, отправка сообщений и вставка ссылок.')
        return channel

    def owns(self, message):
        return (message.author.id == self.client.user.id and len(message.embeds) == 1
                and message.embeds[0].title == TITLE
                and (message.embeds[0].footer.text or '').startswith(FOOTER))

    async def find(self, channel, row, *, for_publication):
        message_id = row['message_id']
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound as error:
                if error.code != 10008:
                    raise
            else:
                if self.owns(message):
                    return message
        # Сохраняем ID последнего сообщения ДО первой попытки send. После сбоя
        # проверяем всё, что появилось с той попытки, а не всю историю канала.
        boundary = row['search_after']
        if boundary is None:
            if not for_publication:
                return None
            latest = await anext(channel.history(limit=1), None)
            if latest and self.owns(latest):
                return latest
            boundary = await self.db('begin_family_guide_search', str(channel.id), str(latest.id if latest else 0))
        async for message in channel.history(limit=None, after=discord.Object(id=int(boundary))):
            if self.owns(message):
                return message
        return None

    async def publish(self, channel, row):
        await self.db('ensure_channel_separation', str(channel.id))
        message = await self.find(channel, row, for_publication=True)
        embed = guide_embed()
        if message is None:
            nonce = 'g' + str(row['message_id'] or channel.id)
            message = await channel.send(embed=embed, nonce=nonce, allowed_mentions=discord.AllowedMentions.none())
            if not self.owns(message):
                raise UserError('Discord вернул другое сообщение. Проверьте канал памятки и повторите выбор.')
        # При совпадении nonce Discord может вернуть прежнюю памятку со ссылкой на перенос.
        if message.embeds[0] != embed:
            await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        if row['status'] != 'active' or row['message_id'] != str(message.id) or row['search_after'] is not None:
            await self.db('bind_family_guide', str(channel.id), str(message.id))
        return message

    async def retire(self, destination):
        complete = True
        for row in await self.db('family_guide_posts'):
            if row['status'] != 'retiring':
                continue
            try:
                channel = await self.channel(row['channel_id'])
                previous = await self.find(channel, row, for_publication=False)
                if previous:
                    redirect = discord.Embed(title=TITLE, description=f'[Читать памятку в новом канале]({destination.jump_url})')
                    redirect.set_footer(text=FOOTER + ' · перенесена')
                    await previous.edit(embed=redirect, allowed_mentions=discord.AllowedMentions.none())
                await self.db('finish_family_guide_move', row['channel_id'])
            except discord.NotFound as error:
                if error.code in (10003, 10008):
                    await self.db('finish_family_guide_move', row['channel_id'])
                else:
                    complete = False
                    log_error('Не удалось обновить прежнюю памятку семьи', error)
            except Exception as error:
                complete = False
                log_error('Не удалось обновить прежнюю памятку семьи', error)
        return complete

    async def select(self, channel_id):
        # Вызывается администратором под общим lock с восстановлением на старте.
        channel = await self.channel(channel_id)
        await self.db('select_family_guide_channel', str(channel.id))
        row = next(r for r in await self.db('family_guide_posts') if r['channel_id'] == str(channel.id))
        message = await self.publish(channel, row)
        moved = await self.retire(message)
        return message.jump_url, moved

    async def sync(self):
        async with self.lock:
            if self.client.backups.restoring:
                return
            try:
                rows = await self.db('family_guide_posts')
            except Exception as error:
                log_error('Не удалось прочитать настройки памятки семьи', error)
                return
            # При неудачном переносе прежняя привязка продолжает работать.
            for status in ('target', 'active'):
                row = next((r for r in rows if r['status'] == status), None)
                if row:
                    try:
                        channel = await self.channel(row['channel_id'])
                        message = await self.publish(channel, row)
                        await self.retire(message)
                        return
                    except Exception as error:
                        log_error('Не удалось обновить памятку семьи', error)
