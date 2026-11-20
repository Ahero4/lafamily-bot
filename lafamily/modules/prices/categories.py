"""Порядок разделов и товаров для публичных цен и личных уведомлений."""
from .parser import LIMITS, MATERIALS, PRODUCTS


CATEGORIES = (
    ('Охота', ('Шкура', 'Идеальное мясо', 'Хорошее мясо')),
    ('Горщик', ('Медь', 'Изумруд', 'Рубин', 'Алмаз')),
    ('Рыбалка', ('Окунь', 'Карп', 'Форель', 'Лосось')),
    ('Нефтекачка', ('Бензин', 'Солярка', 'Керосин')),
)
_ASSIGNED = {name for _, names in CATEGORIES for name in names}
OTHER = 'Остальное'
MATERIAL_CATEGORIES = (*CATEGORIES, (OTHER, tuple(name for name in MATERIALS if name not in _ASSIGNED)))
PRODUCT_CATEGORY = 'Скупщик продуктов'
PRICE_CATEGORIES = (*MATERIAL_CATEGORIES, (PRODUCT_CATEGORY, PRODUCTS))
ITEM_ORDER = {name: (section, position) for section, (_, names) in enumerate(PRICE_CATEGORIES)
              for position, name in enumerate(names)}


def grouped_price_items(items):
    """Значения словаря по непустым разделам в порядке публичной карточки."""
    for title, names in PRICE_CATEGORIES:
        rows = [items[name] for name in names if name in items]
        if title == OTHER:
            # Ранее сохранённая задача может содержать товар из старого справочника.
            rows.extend(value for name, value in items.items() if name not in LIMITS)
        if rows:
            yield title, rows


def grouped_price_text(lines):
    return '\n\n'.join(f'**{title}**\n' + '\n'.join(rows) for title, rows in grouped_price_items(lines))
