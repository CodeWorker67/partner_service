from typing import Any, Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from services.bot_env import load_bot_env


def is_telegram_chat_id(user_id: int) -> bool:
    return user_id > 0


async def notify_via_bot(
    bot_id: int,
    chat_id: int,
    text: str,
    *,
    reply_markup: Any = None,
) -> bool:
    if not is_telegram_chat_id(chat_id):
        return False
    env = load_bot_env(bot_id)
    if not env:
        return False
    child = Bot(token=env.tg_token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await child.send_message(
            chat_id,
            text,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        return True
    finally:
        await child.session.close()
