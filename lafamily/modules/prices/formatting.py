"""Широкие контейнеры Discord с рамкой; жирные цены и галочки сохраняются."""

RENDER_VERSION = 4
import discord
from .parser import LIMITS, MATERIALS, PRODUCTS


# Порядок категорий и товаров влияет только на отображение, расчёты цен остаются общими.
CATEGORIES = (
    ('Охота', ('Шкура', 'Идеальное мясо', 'Хорошее мясо')),
    ('Горщик', ('Медь', 'Изумруд', 'Рубин', 'Алмаз')),
    ('Рыбалка', ('Окунь', 'Карп', 'Форель', 'Лосось')),
    ('Нефтекачка', ('Бензин', 'Солярка', 'Керосин')),
)



def near_maximum(price: int, maximum: int) -> bool:
    # Целочисленное сравнение без ошибок float: обе границы включены.
    return maximum * 9 <= price * 10 <= maximum * 10


def reference_mark(price: int, maximum: int) -> str:
    # При ровно 95% ставим только кубок; предупреждение относится к диапазону [90%, 95%).
    if maximum * 95 <= price * 100 <= maximum * 100:
        return '🏆'
    return '⚠️' if near_maximum(price, maximum) else ''


def price_marks(price: int, maximum: int, is_best: bool) -> str:
    return ' '.join(mark for mark in ('✅' if is_best else '', reference_mark(price, maximum)) if mark)


def formatted_price(row, best_base=None, best_bonus=None):
    base_best = best_base is not None and row.base == best_base
    base = f'**${row.base}**' if base_best else f'${row.base}'
    base_marks = price_marks(row.base, LIMITS[row.name]['base'], base_best)
    if row.bonus is None:
        return base + (f' {base_marks}' if base_marks else '')
    bonus_best = best_bonus is not None and row.bonus == best_bonus
    bonus = f'**${row.bonus}**' if bonus_best else f'${row.bonus}'
    bonus_marks = price_marks(row.bonus, LIMITS[row.name]['bonus'], bonus_best)
    if base_marks == bonus_marks:
        suffix = base_marks
    else:
        suffix = '; '.join(mark for mark in (f'{base_marks} обычная' if base_marks else '',
                                             f'{bonus_marks} +20%' if bonus_marks else '') if mark)
    return f'{base} ({bonus})' + (f' {suffix}' if suffix else '')


def render_prices(report):
    best_base = {name: max(report.buyers[i][name].base for i in (1, 2, 3)) for name in MATERIALS}
    best_bonus = {name: max((report.buyers[i][name].bonus for i in (1, 2, 3) if report.buyers[i][name].bonus is not None), default=None) for name in MATERIALS}
    view = discord.ui.LayoutView(timeout=None)
    blocks = []
    for buyer in (1, 2, 3):
        # Нераспределённые товары автоматически попадают в «Остальное».
        assigned = {name for _, names in CATEGORIES for name in names}
        categories = (*CATEGORIES, ('Остальное', tuple(name for name in MATERIALS if name not in assigned)))
        sections = []
        for title, names in categories:
            if not names:
                continue
            lines = [f'• {name}: {formatted_price(report.buyers[buyer][name], best_base[name], best_bonus[name])}' for name in names]
            sections.append(f'**{title}**\n' + '\n'.join(lines))
        blocks.append((f'Скупщик сырья №{buyer}', '\n\n'.join(sections)))
    blocks.append(('Скупщик продуктов', '\n'.join(
        f'• {name}: {formatted_price(report.products[name])}' for name in PRODUCTS)))
    header = '**🔔 Цены у скупщиков — сравнение**'
    if report.stamp:
        header += f'\n{report.stamp}'
    header += ('\n✅ и **жирный шрифт** — лучшая цена среди трёх скупщиков (при равенстве выделяются все). '
               'В скобках — цена +20%.\n🏆 — от 95% до 100% справочного максимума; '
               '\n⚠️ — от 90% до 95% (не включая 95%). '
               '\nЛучшая цена может одновременно иметь ✅ и 🏆 или ⚠️. '
               'Если значки обычной и бонусной цены различаются, есть уточнение «обычная» / «+20%».\n'
               'Цены актуальны на момент публикации и могут снижаться в течение часа.')
    view.add_item(discord.ui.TextDisplay(header))
    for title, prices in blocks:
        # Container даёт рамку на ширину сообщения. TextDisplay поддерживает обычный Markdown.
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"### {title}"), discord.ui.TextDisplay(prices)))
    if view.content_length() > 4000:
        raise ValueError('Обработанный отчёт превышает ограничения компонентов Discord')
    # Для Components V2 нельзя отправлять обычные content/embeds вместе с компонентами.
    return {'view': view, 'allowed_mentions': discord.AllowedMentions.none()}
