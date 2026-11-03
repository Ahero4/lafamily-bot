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
from lafamily.diagnostics import log_error


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
                await database_call(service.store, 'approve_onboarding', str(self.message.id), str(interaction.user.id), name, static)
                result = await service.complete(interaction, self.message, member)
            await interaction.edit_original_response(content=result)
        else:
            settings = await database_call(service.store, 'onboarding_settings', str(interaction.guild_id))
            if not settings or str(interaction.channel_id) != settings['entry_id']:
                raise UserError('Форма устарела. Откройте её в настроенном канале получения роли.')
            await database_call(service.store, 'ensure_channel_separation', str(interaction.guild_id), settings['review_id'])
            channel = await interaction.guild.fetch_channel(int(settings['review_id']))
            # Сохраняем заявку ДО отправки. Карточка сразу содержит постоянные кнопки.
            pending_id = f'pending:{interaction.id}'
            await database_call(service.store, 'save_onboarding_request', pending_id, str(interaction.guild_id),
                str(interaction.user.id), settings['role_id'], name, static, settings['review_id'])
            embed = service.embed(name, static, interaction.user.id)
            embed.set_footer(text=f'Заявка: {interaction.id}')
            try:
                message = await channel.send(embed=embed, view=ReviewView(service), allowed_mentions=discord.AllowedMentions.none())
            except Exception as error:
                if isinstance(error, discord.HTTPException) and 400 <= error.status < 500 and error.status != 408:
                    await database_call(service.store, 'discard_pending_onboarding', pending_id)
                    raise UserError('Discord отклонил отправку заявки. Попросите администратора исправить доступ к каналу и подайте анкету снова.') from error
                log_error('Отправка новой заявки не подтверждена; данные сохранены', error)
                raise UserError('Отправка заявки не подтверждена. Попросите администратора проверить канал заявок перед повторной подачей.') from error
            try:
                await database_call(service.store, 'attach_onboarding_message', pending_id, str(message.id),
                                    str(interaction.guild_id), settings['review_id'])
            except Exception as error:
                # Постоянная кнопка восстановит связь по номеру заявки в карточке.
                log_error('Карточка заявки отправлена; привязка будет восстановлена при проверке', error)
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
        async with self.service.lock:
            row = await self.service.request_for_message(interaction)
            if row['approved']:
                await interaction.response.defer(ephemeral=True, thinking=True)
                result = await self.service.complete(interaction, interaction.message)
                await interaction.edit_original_response(content=result)
            else:
                await interaction.response.send_modal(IdentityModal(self.service, row, interaction.message))


    @discord.ui.button(label='Отклонить заявку', style=discord.ButtonStyle.danger, custom_id='onboarding:reject')
    async def reject(self, interaction, button):
        await require_access_async(interaction, self.service.store, 'admin')
        row = await self.service.request_for_message(interaction)
        if row['approved']:
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
            await database_call(self.service.store, 'reject_onboarding', str(self.message.id), str(interaction.user.id), reason)
            result = await self.service.complete(interaction, self.message)
        await interaction.edit_original_response(content=result)


class RecoveryView(discord.ui.View):
    """Повторяем только незавершённую доставку; решение и роль не меняются."""
    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service

    async def on_error(self, interaction, error, item):
        await send_error(interaction, error)

    @discord.ui.button(label='Обновить карточку / уведомление', custom_id='onboarding:repair')
    async def repair(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, self.service.store, 'admin')
        async with self.service.lock:
            row = await self.service.request_for_message(interaction)
            if not row['approved']:
                raise UserError('Сначала проверьте заявку.')
            result = await self.service.complete(interaction, interaction.message)
        await interaction.edit_original_response(content=result)


class OnboardingService:
    def __init__(self, store):
        self.store, self.lock = store, asyncio.Lock()

    async def request_for_message(self, interaction):
        message = interaction.message
        row = await database_call(self.store, 'onboarding_request', str(message.id))
        if row is None:
            # Только собственная карточка бота может восстановить незавершённую привязку.
            if message.author.id != interaction.client.user.id or not message.embeds:
                raise UserError('Заявка не найдена.')
            footer = message.embeds[0].footer.text or ''
            match = re.fullmatch(r'Заявка: ([0-9]{17,20})', footer)
            if not match:
                raise UserError('Заявка не найдена.')
            await database_call(self.store, 'attach_onboarding_message', f'pending:{match[1]}', str(message.id),
                                str(interaction.guild_id), str(interaction.channel_id))
            row = await database_call(self.store, 'onboarding_request', str(message.id))
        if not row or row['guild_id'] != str(interaction.guild_id):
            raise UserError('Заявка не найдена на этом сервере.')
        return row

    async def complete(self, interaction, message, member=None):
        row = await database_call(self.store, 'onboarding_request', str(message.id))
        if not row or row['guild_id'] != str(interaction.guild_id) or not row['approved']:
            raise UserError('Решение по заявке не найдено.')
        name = row['name'] or message.embeds[0].fields[0].value
        static = row['static'] or message.embeds[0].fields[1].value
        approved = row['approved'] == 1
        if await database_call(self.store, 'claim_onboarding_notification', str(message.id)):
            state = 'uncertain'
            sending = False
            try:
                if member is None:
                    member = await interaction.guild.fetch_member(int(row['user_id']))
                text = (f'✅ Ваша заявка на сервере «{interaction.guild.name}» утверждена. Роль выдана, ваш ник: {name} | {static}.'
                        if approved else f'❌ Ваша заявка на сервере «{interaction.guild.name}» отклонена.\nПричина: {row["reason"]}\nВы можете исправить данные и подать новую заявку.')
                sending = True
                state = 'sent' if await self.notify(member, text) else 'failed'
            except (discord.Forbidden, discord.NotFound):
                state = 'failed'
            except Exception as error:
                state = 'uncertain' if sending else 'failed'
                log_error('Доставка решения по заявке не подтверждена; повтор DM отключён', error)
            await database_call(self.store, 'finish_onboarding_notification', str(message.id), state)
        row = await database_call(self.store, 'onboarding_request', str(message.id))
        state = row['notification_state']
        embed = self.embed(name, static, int(row['user_id']))
        embed.title = '✅ Заявка утверждена' if approved else '❌ Заявка отклонена'
        if not approved:
            embed.colour = discord.Colour.red()
            embed.add_field(name='Причина', value=discord.utils.escape_markdown(row['reason'] or 'Отказ сохранён ранее.'), inline=False)
        embed.set_footer(text=await self.decision_footer(interaction, row))
        result = 'Роль выдана, никнейм изменён. ' if approved else 'Заявка отклонена. '
        result += {'sent':'Участник уведомлён.', 'failed':'Личное сообщение не доставлено. Доставку можно повторить кнопкой карточки.',
                   'legacy':'Прежнее уведомление повторно не отправлялось.'}.get(state,
                   'Доставка личного сообщения не подтверждена; повтор отключён, чтобы не создавать дубли.')
        try:
            await message.edit(embed=embed, view=RecoveryView(self) if state == 'failed' else None,
                               allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            log_error('Решение по заявке сохранено; карточку обновить не удалось', error)
            result += ' Карточку обновить не удалось. Нажмите её кнопку проверки ещё раз: роль и решение не изменятся.'
        return result

    @staticmethod
    async def decision_footer(interaction, row):
        approved = row['approved'] == 1
        fallback = 'Утверждено администратором' if approved else 'Отклонено администратором'
        if not row['actor_id']:
            return fallback
        try:
            actor_id = int(row['actor_id'])
            # При восстановлении берём автора сохранённого решения, а не нажавшего кнопку.
            if interaction.user.id == actor_id:
                actor = interaction.user
            else:
                actor = interaction.guild.get_member(actor_id)
                if actor is None:
                    actor = await interaction.guild.fetch_member(actor_id)
            return f"{'Утвердил' if approved else 'Отклонил'}: {actor.display_name}"
        except Exception as error:
            # Недоступность имени не должна мешать обновлению уже обработанной заявки.
            log_error('Не удалось получить имя администратора для карточки заявки', error)
            return fallback

    @staticmethod
    async def notify(member, text):
        try:
            await member.send(text, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException as error:
            if 400 <= error.status < 500 and error.status != 408:
                return False
            raise

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
