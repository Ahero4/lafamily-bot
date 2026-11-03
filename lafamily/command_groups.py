"""Общие корневые группы slash-команд, используемые несколькими модулями."""
import discord
from discord import app_commands


def command_group(tree, guild_id, name, description):
    guild = discord.Object(id=guild_id)
    existing = tree.get_command(name, guild=guild)
    if existing is not None:
        if not isinstance(existing, app_commands.Group):
            raise TypeError(f'Command {name} is not a group')
        return existing
    group = app_commands.Group(name=name, description=description, guild_only=True)
    tree.add_command(group, guild=guild)
    return group
