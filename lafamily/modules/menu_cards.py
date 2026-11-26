"""Local artwork for the personal Discord menu."""

from contextlib import contextmanager
from pathlib import Path

import discord

from lafamily.diagnostics import log_error

CARD_DIR = Path(__file__).resolve().parents[1] / 'assets' / 'menu'


@contextmanager
def menu_card(section, embed=None):
    if section is None:
        yield embed, []
        return

    try:
        file = discord.File(CARD_DIR / f'{section}.jpg')
    except OSError as error:
        log_error(f'Не удалось открыть иллюстрацию меню: {section}', error)
        yield embed, []
        return

    try:
        if embed is None:
            embed = discord.Embed()
        embed.set_image(url=f'attachment://{file.filename}')
        yield embed, [file]
    finally:
        file.close()
