"""Регистрация участника Discord в розыгрышах портала."""
import os
import re
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands

from lafamily.errors import UserError
from lafamily.responses import send_error


RAFFLE_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
PAGE_SIZE = 5


class RafflePortal:
    def __init__(self, guild_id):
        self.guild_id = guild_id

    def credentials(self):
        url = urlsplit(os.getenv('FAMILY_BOT_SYNC_URL', '').strip())
        token = os.getenv('FAMILY_BOT_SYNC_TOKEN', '').strip()
        if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
                or not token or os.getenv('FAMILY_BOT_GUILD_ID') != str(self.guild_id)):
            raise UserError('Связь с розыгрышами портала не настроена.')
        return f'{url.scheme}://{url.netloc}', token

    async def request(self, action, **fields):
        origin, token = self.credentials()
        headers = {'Origin': origin, 'Authorization': 'Bearer ' + token}
        payload = dict(action=action, guildId=str(self.guild_id), **fields)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.post(origin + '/api/integrations/family-bot/raffles', headers=headers,
                                        json=payload, allow_redirects=False) as response:
                    if response.status in (403, 404, 409):
                        data = await response.json()
                        reason = data.get('error') if isinstance(data, dict) else None
                        raise UserError(reason[:300] if isinstance(reason, str) and reason else 'Запись недоступна.')
                    if response.status != 200:
                        raise UserError('Портал временно недоступен. Попробуйте позже.')
                    data = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise UserError('Не удалось связаться с порталом. Попробуйте ещё раз.') from error
        if not isinstance(data, dict):
            raise UserError('Не удалось прочитать ответ портала.')
        return data


class RaffleButton(discord.ui.DynamicItem[discord.ui.Button], template=r'lafraffle:(?P<id>[A-Za-z0-9_-]{1,64})'):
    def __init__(self, raffle_id, label='Участвовать'):
        self.raffle_id = raffle_id
        super().__init__(discord.ui.Button(label=label, custom_id=f'lafraffle:{raffle_id}',
                                           style=discord.ButtonStyle.success))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['id'], item.label)

    async def callback(self, interaction):
        try:
            portal = interaction.client.raffles
            if (interaction.guild_id != portal.guild_id or interaction.user.bot
                    or interaction.message.author.id != interaction.client.user.id):
                raise UserError('Эта кнопка доступна участникам на сервере семьи.')
            await interaction.response.defer(thinking=True, ephemeral=True)
            result = await portal.request('join', discordId=str(interaction.user.id), raffleId=self.raffle_id,
                                          nickname=interaction.user.display_name[:80])
            status = result.get('status')
            if status not in ('ACTIVE', 'PENDING') or not isinstance(result.get('duplicate'), bool):
                raise UserError('Не удалось подтвердить запись. Нажмите кнопку ещё раз.')
            if status == 'PENDING':
                message = 'Ваша заявка уже ожидает проверки.' if result['duplicate'] else 'Заявка отправлена на проверку.'
            else:
                message = 'Вы уже участвуете в этом розыгрыше.' if result['duplicate'] else 'Вы участвуете в розыгрыше.'
            await interaction.edit_original_response(content=message)
        except UserError as error:
            await send_error(interaction, error)


async def show_raffles(interaction, page=1):
    portal = interaction.client.raffles
    if interaction.guild_id != portal.guild_id or interaction.user.bot:
        raise UserError('Команда доступна участникам на сервере семьи.')
    await interaction.response.defer(thinking=True)
    rows = (await portal.request('list')).get('raffles')
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
            or not RAFFLE_ID.fullmatch(row['id']) or not isinstance(row.get('title'), str) for row in rows):
        raise UserError('Не удалось прочитать список розыгрышей.')
    if not rows:
        await interaction.edit_original_response(content='Сейчас нет розыгрышей с открытой записью.')
        return
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    if not 1 <= page <= pages:
        raise UserError(f'Укажите страницу от 1 до {pages}.')
    view = discord.ui.View(timeout=None)
    embed = discord.Embed(title='Открытые розыгрыши', colour=0xd9ac56)
    lines = []
    for i, row in enumerate(rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE], start=1):
        title = discord.utils.escape_markdown(row['title'][:200])
        lines.append(f'**{i}.** {title}')
        view.add_item(RaffleButton(row['id'], 'Участвовать' if len(rows) == 1 else f'Участвовать · {i}'))
    embed.description = '\n'.join(lines)
    if pages > 1:
        embed.set_footer(text=f'Страница {page} из {pages}. Другую страницу открывает команда /розыгрыши.')
    await interaction.edit_original_response(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())


def register_raffles(tree, guild_id):
    @tree.command(name='розыгрыши', description='Показать открытые розыгрыши и кнопки записи',
                  guild=discord.Object(id=guild_id))
    @app_commands.describe(страница='Номер страницы списка розыгрышей')
    async def raffles(interaction: discord.Interaction, страница: app_commands.Range[int, 1] = 1):
        await show_raffles(interaction, страница)
