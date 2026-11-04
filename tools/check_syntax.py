"""Проверка исходников без импорта бота, подключения к Discord и открытия БД."""
import ast
from pathlib import Path

root=Path(__file__).resolve().parents[1]
files=[*root.glob('*.py'),*root.joinpath('lafamily').rglob('*.py'),*root.joinpath('tests').rglob('*.py')]
for path in files:ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
print(f'Синтаксис: {len(files)} файлов OK')
