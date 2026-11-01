"""Разбор только полного отчёта: непонятное сообщение нельзя удалять."""
import json
import re
from dataclasses import dataclass
from pathlib import Path

LIMITS = json.loads(Path(__file__).with_name('limits.json').read_text(encoding='utf-8-sig'))
MATERIALS = tuple(name for name, cap in LIMITS.items() if cap['bonus'] is not None)
PRODUCTS = tuple(name for name, cap in LIMITS.items() if cap['bonus'] is None)
CANONICAL = {name.casefold(): name for name in LIMITS}
BUYER = re.compile(r'Скупщик\s+сырья\s*№\s*([123])', re.IGNORECASE)
FOOD = re.compile(r'Скупщик\s+продуктов', re.IGNORECASE)
AMOUNT = r'[0-9]+(?:[ \u00a0\u202f,][0-9]{3})*'
ROW = re.compile(rf'^\s*[-•]\s*(?P<name>[^:]+):\s*\$(?P<base>{AMOUNT})(?:\s*\(\$(?P<bonus>{AMOUNT})\))?\s*$')
STAMP = re.compile(r'\b[0-9]{2}\.[0-9]{2}\.[0-9]{4}\s+[0-9]{2}:[0-9]{2}\s+МСК\b')


@dataclass(frozen=True)
class Price:
    name: str
    base: int
    bonus: int | None


@dataclass
class PriceReport:
    buyers: dict[int, dict[str, Price]]
    products: dict[str, Price]
    stamp: str | None


def amount(value):
    return int(re.sub(r'[^0-9]', '', value))


def parse_prices(text: str) -> PriceReport:
    # Закрывающий ``` в примере стоит сразу после цены, без переноса строки.
    cleaned = re.sub(r'```[a-zA-Z]*', '', text)
    buyers, products = {}, None
    current = None
    for line in cleaned.splitlines():
        heading = BUYER.search(line)
        if heading:
            buyer = int(heading.group(1))
            if buyer in buyers:
                raise ValueError('Повтор раздела скупщика')
            buyers[buyer] = {}
            current = buyers[buyer]
            continue
        if FOOD.search(line):
            if products is not None:
                raise ValueError('Повтор раздела продуктов')
            products = {}
            current = products
            continue
        if current is None or not re.match(r'^\s*[-•]', line):
            continue
        match = ROW.fullmatch(line)
        if not match:
            raise ValueError('Не удалось прочитать строку цены')
        name = CANONICAL.get(match['name'].strip().casefold())
        if name is None or name in current:
            raise ValueError('Неизвестный или повторный товар')
        current[name] = Price(name, amount(match['base']), amount(match['bonus']) if match['bonus'] else None)
    if set(buyers) != {1, 2, 3} or products is None:
        raise ValueError('Нужны три скупщика сырья и скупщик продуктов')
    if any(set(rows) != set(MATERIALS) for rows in buyers.values()) or set(products) != set(PRODUCTS):
        raise ValueError('Отчёт неполный или товары попали не в тот раздел')
    for name in MATERIALS:
        if len({buyers[i][name].bonus is None for i in (1, 2, 3)}) != 1:
            raise ValueError('Бонусные цены указаны не для всех скупщиков')
    if any(row.bonus is not None for row in products.values()):
        raise ValueError('Неожиданные бонусные цены продуктов')
    stamp = STAMP.search(text)
    return PriceReport(buyers, products, stamp.group(0) if stamp else None)


def message_candidates(message):
    # Поддерживаем обычный текст, webhook embeds и нативную пересылку Discord (snapshots).
    for source in [*getattr(message, 'message_snapshots', []), message]:
        parts = [getattr(source, 'content', '')]
        for embed in getattr(source, 'embeds', []):
            parts.extend([embed.title or '', embed.description or ''])
            for field in embed.fields:
                parts.extend([field.name, field.value])
        yield '\n'.join(dict.fromkeys(part for part in parts if part))


def extract_prices(message):
    for text in message_candidates(message):
        try:
            return parse_prices(text)
        except ValueError:
            continue
    return None
