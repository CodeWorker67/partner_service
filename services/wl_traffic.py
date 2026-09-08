"""WL-трафик Антиглушилка — упрощённый расчёт использованного трафика."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from services.panel import PanelClient, WL_SQUAD_ACTIVE
from services.tariff import panel_username

WL_NODE_NAME = "YANDEX-RU-001"
WL_TIMEZONE = ZoneInfo("Europe/Moscow")
WL_DAY_RESET_HOUR = 3
WL_SQUAD_LIMITED = (
    "e34716de-9526-4ad6-96c8-e00d6259285f",
    "87f868a6-33e7-44ff-aa1b-97ee9fa91a1b",
)
_BYTES_PER_GB = 1024**3


def wl_traffic_day(now: datetime | None = None) -> date:
    if now is None:
        now = datetime.now(WL_TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=WL_TIMEZONE)
    else:
        now = now.astimezone(WL_TIMEZONE)
    day = now.date()
    if now.hour < WL_DAY_RESET_HOUR:
        day -= timedelta(days=1)
    return day


def bytes_to_gb(value: float) -> float:
    return round(value / _BYTES_PER_GB, 2)


def compute_wl_used_gb(trafic_wl_db: float, day_gb: float) -> float:
    return round(float(trafic_wl_db or 0.0) + float(day_gb or 0.0), 2)


def pro_panel_usernames(tg_id: int, bot_id: int) -> list[str]:
    return [
        panel_username(tg_id, bot_id, device_slots=3),
        panel_username(tg_id, bot_id, device_slots=5),
        panel_username(tg_id, bot_id, device_slots=10),
    ]


async def fetch_all_pro_panel_users(panel: PanelClient, tg_id: int, bot_id: int) -> list[dict]:
    result: list[dict] = []
    for uname in pro_panel_usernames(tg_id, bot_id):
        data = await panel.get_user_by_username(uname)
        user = panel._panel_user_from_response(data)
        if user:
            result.append(user)
    return result


def extract_squad_uuids(panel_user: dict) -> list[str]:
    squads = panel_user.get("activeInternalSquads") or []
    out: list[str] = []
    for s in squads:
        if isinstance(s, dict) and s.get("uuid"):
            out.append(str(s["uuid"]))
        elif isinstance(s, str):
            out.append(s)
    return out


def user_on_limited_squad(panel_user: dict) -> bool:
    squads = set(extract_squad_uuids(panel_user))
    return bool(squads & set(WL_SQUAD_LIMITED))


def any_pro_user_on_limited_squad(panel_users: list[dict]) -> bool:
    return any(user_on_limited_squad(pu) for pu in panel_users)


async def fetch_wl_traffic_gb_for_day(
    panel: PanelClient, day: date | None = None
) -> tuple[dict[str, float], dict[str, float]]:
    day = day or wl_traffic_day()
    day_str = day.isoformat()
    node_uuid = await panel.get_node_uuid_by_name(WL_NODE_NAME)
    if not node_uuid:
        return {}, {}

    records = await panel.get_node_users_bandwidth_legacy(node_uuid, day_str, day_str)
    if not records:
        return {}, {}

    by_username: dict[str, float] = {}
    by_uuid: dict[str, float] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        uname = str(item.get("username") or "")
        user_uuid = str(item.get("userId") or item.get("userUuid") or "")
        gb = bytes_to_gb(float(item.get("total") or 0))
        if uname:
            by_username[uname] = by_username.get(uname, 0.0) + gb
        if user_uuid:
            by_uuid[user_uuid] = by_uuid.get(user_uuid, 0.0) + gb
    return by_username, by_uuid


def wl_traffic_gb_for_panel_user(
    panel_user: dict,
    traffic_by_username: dict[str, float],
    traffic_by_uuid: dict[str, float],
) -> float:
    uname = str(panel_user.get("username") or "")
    user_uuid = str(panel_user.get("uuid") or panel_user.get("id") or "")
    if uname and uname in traffic_by_username:
        return traffic_by_username[uname]
    if user_uuid and user_uuid in traffic_by_uuid:
        return traffic_by_uuid[user_uuid]
    return 0.0


async def get_wl_used_gb_for_user(
    panel: PanelClient,
    tg_id: int,
    bot_id: int,
    trafic_wl_db: float,
) -> float:
    panel_users = await fetch_all_pro_panel_users(panel, tg_id, bot_id)
    if not panel_users:
        return round(float(trafic_wl_db or 0.0), 2)

    by_username, by_uuid = await fetch_wl_traffic_gb_for_day(panel)
    day_gb = 0.0
    for pu in panel_users:
        day_gb += wl_traffic_gb_for_panel_user(pu, by_username, by_uuid)
    return compute_wl_used_gb(trafic_wl_db, day_gb)


async def reassign_to_active_squad(panel: PanelClient, panel_user: dict) -> bool:
    import random

    panel_user_id = panel._panel_user_id(panel_user)
    username = str(panel_user.get("username") or "").strip() or None
    squad = [random.choice(WL_SQUAD_ACTIVE)]
    return await panel.update_user_squads(panel_user_id, squad, username=username)


async def reassign_all_pro_to_active(panel: PanelClient, tg_id: int, bot_id: int) -> int:
    panel_users = await fetch_all_pro_panel_users(panel, tg_id, bot_id)
    n = 0
    for pu in panel_users:
        if user_on_limited_squad(pu):
            if await reassign_to_active_squad(panel, pu):
                n += 1
    return n
