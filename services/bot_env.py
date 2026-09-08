from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

from config import BOTS_ROOT
from logging_config import logger


@dataclass(frozen=True)
class BotEnv:
    bot_id: int
    tg_token: str
    panel_url: str
    panel_api_token: str
    short_uuid_secret: str
    true_sub_link: str
    mirror_sub_link: str


def bot_env_path(bot_id: int) -> Path:
    return BOTS_ROOT / str(bot_id) / ".env"


def is_bot_deployed(bot_id: int) -> bool:
    """Бот развёрнут: есть /root/{bot_id}/.env с TG_TOKEN."""
    path = bot_env_path(bot_id)
    if not path.is_file():
        return False
    raw = dotenv_values(path)
    return bool((raw.get("TG_TOKEN") or "").strip())


def is_active_bot_settings(row) -> bool:
    """Активный бот: задан @username в БД или есть развёрнутый .env."""
    if (row.bot_username or "").strip():
        return True
    return is_bot_deployed(row.bot_id)


def load_bot_env(bot_id: int) -> Optional[BotEnv]:
    path = bot_env_path(bot_id)
    if not path.is_file():
        logger.warning("bot_env: .env не найден: %s", path)
        return None
    raw = dotenv_values(path)
    tg_token = (raw.get("TG_TOKEN") or "").strip()
    panel_url = (raw.get("PANEL_URL") or "").strip()
    panel_api_token = (raw.get("PANEL_API_TOKEN") or "").strip()
    if not tg_token:
        logger.warning("bot_env: TG_TOKEN пуст для bot_id=%s", bot_id)
        return None
    return BotEnv(
        bot_id=bot_id,
        tg_token=tg_token,
        panel_url=panel_url,
        panel_api_token=panel_api_token,
        short_uuid_secret=(raw.get("SHORT_UUID_SECRET") or "").strip(),
        true_sub_link=(raw.get("TRUE_SUB_LINK") or "").strip(),
        mirror_sub_link=(raw.get("MIRROR_SUB_LINK") or "").strip(),
    )
