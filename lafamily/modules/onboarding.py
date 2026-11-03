"""Постоянная форма получения роли и проверка заявки администратором."""
from lafamily.command_groups import command_group
import asyncio
import re
import discord
from discord import app_commands
from lafamily.async_database import database_call
from lafamily.permissions import require_access_async
from lafamily.errors import UserError
from lafamily.responses import send_error


def validate_identity(name, static):
    name = ' '.join(name.split())
    if not re.fullmatch(r"[A-Za-z]+(?:[-'][A-Za-z]+)* [A-Za-z]+(?:[-'][A-Za-z]+)*", name):
        raise UserError('Введите имя и фамилию латиницей, например Sin Bertoni.')
    if not re.fullmatch(r'[0-9]{1,12}', static) or int(static) == 0:
        raise UserError('Static ID должен быть положительным числом до 12 цифр.')
    nickname = f'{name} | {static}'
    if len(nickname) > 32:
        raise UserError('Имя и Static ID вместе должны помещаться в ник Discord (32 символа).')
    return name, static, nickname


class IdentityModal(discord.ui.Modal):
    name = discord.ui.TextInput(label='Имя Фамилия', max_length=28)
    static = discord.ui.TextInput(label='Static ID', max_length=12)

    def __init__(self, service, request=None, message=None):
        super().__init__(title='Исправить и утвердить' if request else 'Получение роли')
        self.service, self.request, self.message = service, request, message
        if request:
            self.name.default = message.embeds[0].fields[0].value
            self.static.default = message.embeds[0].fields[1].value

    async def on_error(self, interaction, error):
        await send_error(interaction, error)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        name, static, nickname = validate_identity(str(self.name), str(self.static))
        service = self.service
        if self.request:
            await require_access_async(interaction, service.store, 'admin')
            async with service.lock:
                row = await database_call(service.store, 'onboarding_request', str(self.message.id))
                if not row or row['guild_id'] != str(interaction.guild_id):
                    raise UserError('Заявка не найдена.')
                if row['approved']:
                    raise UserError('Эта заявка уже обработана.')
                member = await interaction.guild.fetch_member(int(row['user_id']))
                role = interaction.guild.get_role(int(row['role_id']))
                service.check_role(interaction.guild, role)
                me = interaction.guild.me
                if member.id == interaction.guild.owner_id or member.top_role >= me.top_role:
                    raise UserError('Роль бота должна быть выше всех ролей участника; владельцу сервера ник изменить нельзя.')
                # При частичном сбое повторное утверждение безопасно: выдача той же роли идемпотентна.
                await member.edit(nick=nickname, reason=f'Заявка одобрена {interaction.user.id}')
                await member.add_roles(role, reason=f'Заявка одобрена {interaction.user.id}')
                await database_call(service.store, 'approve_onboarding', str(self.message.id), str(interaction.user.id))
                embed = service.embed(name, static, member.id)
                embed.title = '✅ Заявка утверждена'
                embed.set_footer(text=f'Утвердил: {interaction.user.display_name}')
                await self.message.edit(embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none())
            delivered = await service.notify(member, f'✅ Ваша заявка на сервере «{interaction.guild.name}» утверждена. Роль выдана, ваш ник: {nickname}.')
            await interaction.edit_original_response(content='Роль выдана, никнейм изменён. ' + ('Участник уведомлён.' if delivered else 'Не удалось отправить участнику личное сообщение.'))
        else:
            settings = await database_call(service.store, 'onboarding_settings', str(interaction.guild_id))
            if not settings or str(interaction.channel_id) != settings['entry_id']:
                raise UserError('Форма устарела. Откройте её в настроенном канале получения роли.')
            channel = await interaction.guild.fetch_channel(int(settings['review_id']))
            message = await channel.send(embed=service.embed(name, static, interaction.user.id), allowed_mentions=discord.AllowedMentions.none())
            await database_call(service.store, 'save_onboarding_request', str(message.id), str(interaction.guild_id), str(interaction.user.id), settings['role_id'])
            await message.edit(view=ReviewView(service))
            await interaction.edit_original_response(content='Заявка отправлена! Дождитесь проверки администратором.')


class EntryView(discord.ui.View):
    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)

    @discord.ui.button(label='Заполнить заявку', style=discord.ButtonStyle.success, custom_id='onboarding:entry')
    async def enter(self, interaction, button):
        await interaction.response.send_modal(IdentityModal(self.service))


class ReviewView(EntryView):
    def __init__(self, service):
        super().__init__(service)
        self.remove_item(self.enter)

    @discord.ui.button(label='Проверить / исправить и утвердить', style=discord.ButtonStyle.success, custom_id='onboarding:review')
    async def review(self, interaction, button):
        await require_access_async(interaction, self.service.store, 'admin')
        row = await database_call(self.service.store, 'onboarding_request', str(interaction.message.id))
        if not row or row['approved']:
            raise UserError('Заявка не найдена или уже обработана.')
        await interaction.response.send_modal(IdentityModal(self.service, row, interaction.message))


    @discord.ui.button(label='Отклонить заявку', style=discord.ButtonStyle.danger, custom_id='onboarding:reject')
    async def reject(self, interaction, button):
        await require_access_async(interaction, self.service.store, 'admin')
        row = await database_call(self.service.store, 'onboarding_request', str(interaction.message.id))
        if not row or row['approved'] or row['guild_id'] != str(interaction.guild_id):
            raise UserError('Заявка уже обработана или не найдена.')
        await interaction.response.send_modal(RejectModal(self.service, interaction.message))


class RejectModal(discord.ui.Modal, title='Отклонить заявку'):
    reason = discord.ui.TextInput(label='Причина отказа', style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, service, message):
        super().__init__()
        self.service, self.message = service, message

    async def on_error(self, interaction, error):
        await send_error(interaction, error)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, self.service.store, 'admin')
        reason = str(self.reason).strip()
        if not reason:
            raise UserError('Укажите причину отказа.')
        async with self.service.lock:
            row = await database_call(self.service.store, 'onboarding_request', str(self.message.id))
            if not row or row['approved'] or row['guild_id'] != str(interaction.guild_id):
                raise UserError('Заявка уже обработана или не найдена.')
            member = await interaction.guild.fetch_member(int(row['user_id']))
            await database_call(self.service.store, 'reject_onboarding', str(self.message.id), str(interaction.user.id), reason)
            # Решение сохраняется до уведомления: двойной клик не вызовет повторную рассылку.
            delivered = await self.service.notify(member, f'❌ Ваша заявка на сервере «{interaction.guild.name}» отклонена.\nПричина: {reason}\nВы можете исправить данные и подать новую заявку.')
            embed = self.message.embeds[0].copy()
            embed.title = '❌ Заявка отклонена'
            embed.colour = discord.Colour.red()
            embed.add_field(name='Причина', value=discord.utils.escape_markdown(reason), inline=False)
            embed.set_footer(text=f'Отклонил: {interaction.user.display_name}')
            await self.message.edit(embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none())
        await interaction.edit_original_response(content='Заявка отклонена. ' + ('Участник уведомлён.' if delivered else 'Не удалось отправить участнику личное сообщение.'))


class OnboardingService:
    def __init__(self, store):
        self.store, self.lock = store, asyncio.Lock()

    @staticmethod
    async def notify(member, text):
        try:
            await member.send(text, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException:
            return False

    @staticmethod
    def embed(name, static, user_id):
        embed = discord.Embed(title='📋 Заявка на роль', description=f'Участник: <@{user_id}>', colour=0x7289DA)
        embed.add_field(name='Имя Фамилия', value=name)
        embed.add_field(name='Static ID', value=static)
        return embed

    @staticmethod
    def check_role(guild, role):
        me = guild.me
        if not me or not me.guild_permissions.manage_roles or not me.guild_permissions.manage_nicknames:
            raise UserError('Боту нужны права «Управлять ролями» и «Управлять никнеймами».')
        if not role or role.is_default() or role.managed or role >= me.top_role or role.permissions.administrator:
            raise UserError('Выберите обычную роль без права администратора ниже роли бота.')


def register_onboarding(tree, service, guild_id):
    group = command_group(tree, guild_id, 'заявки', 'Вступление и выдача роли')
    @group.command(name='настройки', description='Настроить получение роли: канал формы, канал заявок и роль', extras={'access': 'manager'})
    @app_commands.guild_only()
    @app_commands.rename(entry='начальный-чат', review='админский-чат', role='роль')
    async def settings(interaction: discord.Interaction, entry: discord.TextChannel, review: discord.TextChannel, role: discord.Role):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, 'manager')
        service.check_role(interaction.guild, role)
        for channel in (entry, review):
            permissions = channel.permissions_for(interaction.guild.me)
            if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
                raise UserError('Боту нужны просмотр, отправка сообщений и вставка ссылок в обоих каналах.')
        await database_call(service.store, 'configure_onboarding', str(interaction.guild_id), str(entry.id), str(review.id), str(role.id))
        await entry.send('**Добро пожаловать!** Нажмите кнопку, введите имя, фамилию и Static ID, затем отправьте заявку. После проверки администратором вы получите роль и никнейм.', view=EntryView(service))
        await interaction.edit_original_response(content=f'Форма опубликована в {entry.mention}, заявки поступают в {review.mention}. Выдаваемая роль: {role.mention}.', allowed_mentions=discord.AllowedMentions.none())
