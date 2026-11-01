"""Данные черновика отделены от интерфейса Discord и проверяются без подключения."""

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class Draft:
    guild_id: str
    organizer_id: str
    channel_id: str
    type_id: int
    type_name: str
    points: int
    occurred_at: str
    note: str = ""
    members: list[str] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: str(uuid4()))

    voice_channel_id: str | None = None
    # Снимок настроек: изменение ставок не меняет открытый черновик.
    outcome: str = ""
    win_bonus: int = 0
    mvp_id: str | None = None
    mvp_bonus: int = 0
    # Доступные награды фиксируются отдельно от выбранных начислений.
    win_enabled: bool = False
    available_win_bonus: int = 0
    available_mvp_bonus: int = 0
