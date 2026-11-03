"""Проверка отображаемых имён сервера без изменения ников; уведомления только после подтверждения."""
from lafamily.command_groups import command_group
import asyncio
import gzip
import re
from dataclasses import dataclass, field
from io import BytesIO

import discord
from discord import app_commands
from discord.utils import escape_markdown, escape_mentions

from lafamily.errors import UserError
from lafamily.permissions import require_access_async

# Должность необязательна и отделена своим символом |. Имена — два латинских слова.
# Дефисы и апострофы допустимы внутри имени/фамилии; Static ID содержит только цифры.
NAME = r"[A-Za-z]+(?:[-'][A-Za-z]+)*"
NICKNAME = re.compile(rf"(?:(?P<position>[^|\r\n]+?)\s*\|\s*)?(?P<first>{NAME})[ \t]+(?P<last>{NAME})[ \t]*\|[ \t]*(?P<static>[0-9]+)")


def valid_nickname(value: str) -> bool:
    if "\n" in value or "\r" in value:
        return False
    match = NICKNAME.fullmatch(value.strip())
    return bool(match and (match['position'] is None or match['position'].strip()) and int(match['static']) > 0)


@dataclass
class AuditResult:
    checked: int = 0
    skipped_bots: int = 0
    invalid: list[tuple[int, str]] = field(default_factory=list)


async def audit_members(guild, role_id: int | None = None) -> AuditResult:
    result = AuditResult()
    # Запрашиваем все страницы API, а не неполный список из кэша (в том числе офлайн).
    async for member in guild.fetch_members(limit=None):
        # Проверяем принадлежность по ID роли. Другие роли участника не мешают проверке.
        if role_id is not None and not any(role.id == role_id for role in member.roles):
            continue
        if member.bot:
            result.skipped_bots += 1
            continue
        result.checked += 1
        if not valid_nickname(member.display_name):
            result.invalid.append((member.id, member.display_name))
    return result


def register_nickname_command(tree, store, guild_id):
    group = command_group(tree, guild_id, 'ники', 'Проверка имён участников')
    group.default_permissions = discord.Permissions(administrator=True)
    running = set()

    @group.command(name='проверить', description='Проверить ники всех участников или только выбранной роли',
                  extras={'access': 'admin'})
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(role="роль")
    @app_commands.describe(role="Оставьте пустым для всех участников или выберите роль")
    async def check_nicknames(interaction: discord.Interaction, role: discord.Role | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, store, 'admin')
        if not interaction.client.intents.members:
            raise UserError('Для полного списка участников включите Server Members Intent в Developer Portal → Bot, '
                            'добавьте DISCORD_MEMBERS_INTENT=true в .env и перезапустите бота.')
        if interaction.guild_id in running:
            raise UserError('Проверка ников на этом сервере уже идёт. Дождитесь результата.')
        running.add(interaction.guild_id)
        try:
            try:
                async with asyncio.timeout(120):
                    result = await audit_members(interaction.guild, role.id if role else None)
            except TimeoutError as error:
                raise UserError('Не удалось завершить проверку за 2 минуты. Полный результат не получен; повторите позже.') from error
            except discord.Forbidden as error:
                raise UserError('Discord не разрешил получить список участников. Проверьте Server Members Intent в Developer Portal и настройки приложения.') from error
            # Безопасно показываем имена как текст: упоминания не должны уведомлять людей.
            lines = [f'{i}. <@{member_id}> — {escape_markdown(escape_mentions(name))}'
                     for i, (member_id, name) in enumerate(result.invalid[:15], 1)]
            audience = role.mention if role else 'все участники сервера'
            summary = (f'Проверка: {audience}\nПроверено участников: **{result.checked}**\n'
                       f'Подходят: **{result.checked - len(result.invalid)}**\n'
                       f'Нарушений: **{len(result.invalid)}**\nПропущено ботов: **{result.skipped_bots}**\n\n'
                       'Формат: `Name Surname | Static ID` или `Должность | Name Surname | Static ID`.\n'
                       'Пример: `Зам. | Sin Bertoni | 15096`. Ники не изменялись.')
            embed = discord.Embed(title='Проверка ников LaFamily', description=summary, colour=0xD64545 if result.invalid else 0x41A878)
            if lines:
                # Длинные списки остаются в файле; сообщение показывает первые 15 нарушений.
                for start in range(0, len(lines), 5):
                    embed.add_field(name='Неправильные ники' if start == 0 else 'Продолжение', value='\n'.join(lines[start:start+5]), inline=False)
            else:
                embed.add_field(name='Результат', value='Все проверенные ники подходят под формат.' if result.checked else ('Участников с этой ролью для проверки не найдено.' if role else 'На сервере нет участников для проверки.'), inline=False)
            attachments = []
            if len(result.invalid) > 15:
                report = ('Discord ID\tОтображаемое имя\n' + '\n'.join(f'{uid}\t{name}' for uid, name in result.invalid)).encode('utf-8')
                filename = 'nickname-errors.txt'
                if len(report) > interaction.guild.filesize_limit:
                    report, filename = gzip.compress(report), 'nickname-errors.txt.gz'
                if len(report) <= interaction.guild.filesize_limit:
                    attachments = [discord.File(BytesIO(report), filename=filename)]
                    embed.set_footer(text='В сообщении первые 15 нарушений. Полный список — в приложенном файле.')
                else:
                    embed.set_footer(text='В сообщении первые 15 нарушений. Полный файл превышает лимит загрузки Discord.')
            view = None
            if result.invalid:
                from lafamily.modules.nickname_notifications import NicknameNoticeView
                view = NicknameNoticeView(store, interaction, [uid for uid, _ in result.invalid], role.id if role else None)
            try:
                await interaction.edit_original_response(content=view.prompt() if view else None,
                    embed=embed, attachments=attachments, view=view, allowed_mentions=discord.AllowedMentions.none())
            except BaseException:
                if view:
                    view.finish()
                raise
        finally:
            running.discard(interaction.guild_id)
