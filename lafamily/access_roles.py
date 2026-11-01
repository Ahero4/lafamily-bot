"""Единое чтение наборов ролей, включая настройки старой версии с одной ролью."""


def role_ids(settings: dict | None, kind: str) -> list[str]:
    if not settings:
        return []
    if f"{kind}_role_ids" in settings:
        return settings[f"{kind}_role_ids"]
    value = settings.get(f"{kind}_role_id")
    return [value] if value else []
