from __future__ import annotations

import re
from typing import Literal, Tuple

DeviceSlots = Literal[3, 5, 10]
TierKey = Literal["main", "3", "10"]

_TIER_LABELS = {
    "main": "5 устройств",
    "3": "3 устройства",
    "10": "10 устройств",
}

_TARGET_RE = re.compile(r"^(\d+)_(\d+)(?:_(3|10))?$")


def panel_username(tg_id: int, bot_id: int, *, device_slots: DeviceSlots = 5) -> str:
    base = f"{tg_id}_{bot_id}"
    if device_slots == 3:
        return f"{base}_3"
    if device_slots == 10:
        return f"{base}_10"
    return base


def parse_tg_bot_target(raw: str) -> Tuple[int, int, DeviceSlots]:
    """
    123456789_7      -> (123456789, 7, 5)
    123456789_7_3    -> (123456789, 7, 3)
    123456789_7_10   -> (123456789, 7, 10)
    """
    m = _TARGET_RE.fullmatch(raw.strip())
    if not m:
        raise ValueError(f"Неверный формат: {raw}")
    tg_id = int(m.group(1))
    bot_id = int(m.group(2))
    suffix = m.group(3)
    if suffix == "3":
        return tg_id, bot_id, 3
    if suffix == "10":
        return tg_id, bot_id, 10
    return tg_id, bot_id, 5


def tier_from_device_slots(device_slots: DeviceSlots) -> TierKey:
    if device_slots == 3:
        return "3"
    if device_slots == 10:
        return "10"
    return "main"


def tier_label(tier: TierKey) -> str:
    return _TIER_LABELS[tier]


def hwid_limit_from_device_slots(device_slots: DeviceSlots) -> int:
    return device_slots


def slot_from_panel_username(username: str) -> TierKey:
    if username.endswith("_3"):
        return "3"
    if username.endswith("_10"):
        return "10"
    return "main"
