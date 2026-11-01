"""Версия из метаданных проекта и короткое описание текущего выпуска."""
from pathlib import Path
import tomllib

# Один источник номера версии; файл входит в проект и развёртывается вместе с ботом.
with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as metadata:
    VERSION = tomllib.load(metadata)["project"]["version"]

def parse_releases(history, current):
    """Числовой порядок выпусков, без будущих версий и дубликатов."""
    import re
    entries = {}
    version = None
    lines = []
    def save():
        if version is not None:
            notes = "\n".join(lines).strip()
            if version in entries or not notes:
                raise ValueError("Повтор или пустое описание версии в CHANGELOG.md")
            entries[version] = notes
    for line in history.splitlines():
        match = re.fullmatch(r"## (\d+\.\d+\.\d+)", line)
        if match:
            save()
            version, lines = match[1], []
        elif version is not None:
            lines.append(line)
    save()
    key = lambda value: tuple(map(int, value.split('.')))
    return {v: entries[v] for v in sorted(entries, key=key) if key(v) <= key(current)}


history = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
RELEASES = parse_releases(history, VERSION)
RELEASE_NOTES = RELEASES[VERSION]
LATEST_UPDATE = RELEASE_NOTES.split("\n\n", 1)[0]
