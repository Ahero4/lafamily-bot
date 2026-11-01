"""Команды руководства настройки канала цен и ручной обработки старого сообщения."""
import discord
from discord import app_commands

from lafamily.async_database import database_call
from lafamily.errors import UserError
from lafamily.permissions import require_access_async
from .subscriptions import PriceSubscriptionView


def register_price_commands(tree, service, guild_id):
    guild = discord.Object(id=guild_id)

    @tree.command(name='скупщик-настройка', description='Выбрать канал для сравнения цен скупщиков', guild=guild, extras={'access': 'manager'})
    @app_commands.guild_only()
    @app_commands.rename(channel='канал')
    @app_commands.describe(channel='Исходные сообщения остаются; сравнение публикуется в этом же канале')
    async def settings(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, 'manager')
        service.require_enabled()
        service.require_channel(channel, interaction.guild)
        await database_call(service.store, 'configure_prices', str(interaction.guild_id), str(interaction.user.id), str(channel.id))
        await interaction.edit_original_response(content=f'Канал цен: <#{channel.id}>. Новые полные отчёты будут обработаны автоматически. '
            'Исходные сообщения остаются, сравнение публикуется рядом. Для уже опубликованного отчёта используйте /скупщик-обработать.')

    @tree.command(name='скупщик-обработать', description='Сравнить цены из существующего сообщения по его ID', guild=guild, extras={'access': 'manager'})
    @app_commands.guild_only()
    @app_commands.rename(message_id='сообщение')
    @app_commands.describe(message_id='ID исходного сообщения в настроенном канале цен')
    async def process(interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await require_access_async(interaction, service.store, 'manager')
        service.require_enabled()
        if not message_id.isascii() or not message_id.isdigit() or not 17 <= len(message_id) <= 20:
            raise UserError('Укажите ID сообщения (17–20 цифр). Его можно скопировать через меню сообщения в режиме разработчика Discord.')
        settings = await database_call(service.store, 'price_settings', str(interaction.guild_id))
        if not settings:
            raise UserError('Сначала выполните /скупщик-настройка и выберите канал.')
        channel = await interaction.guild.fetch_channel(int(settings['channel_id']))
        service.require_channel(channel, interaction.guild)
        try:
            source = await channel.fetch_message(int(message_id))
        except discord.NotFound as error:
            raise UserError('Сообщение не найдено в настроенном канале цен.') from error
        except discord.Forbidden as error:
            raise UserError('Боту нужно право «Читать историю сообщений» в канале цен.') from error
        status = await service.process(source, notify=False)
        answers = {'published': 'Сравнение опубликовано. Исходное сообщение сохранено.',
                   'updated': 'Сравнение обновлено. Исходное сообщение сохранено.',
                   'already_done': 'Это сообщение уже обработано, повторной публикации не будет.',
                   'unrecognized': 'Не удалось распознать полный отчёт: нужны все товары трёх скупщиков и раздел продуктов. Исходник не изменён.',
                   'ignored': 'Это сообщение не подходит для обработки в настроенном канале.'}
        await interaction.edit_original_response(content=answers[status])


    @tree.command(name='скупщик-уведомление', description='Выбрать товары для уведомлений о максимальной цене', guild=guild, extras={'access': 'all'})
    @app_commands.guild_only()
    async def subscribe(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild_id != service.guild_id:
            raise UserError('Команда доступна только на настроенном сервере семьи.')
        items = await database_call(service.store, 'price_subscriptions', str(interaction.guild_id), str(interaction.user.id))
        modes = await database_call(service.store, 'price_subscription_modes', str(interaction.guild_id), str(interaction.user.id))
        view = PriceSubscriptionView(service, interaction, items, modes)
        await interaction.edit_original_response(content=view.prompt(), view=view,
                                                 allowed_mentions=discord.AllowedMentions.none())
