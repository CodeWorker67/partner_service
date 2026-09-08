import asyncio

from aiogram import Dispatcher
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot import bot
from config import DATABASE_PATH, TG_TOKEN
from config_bd.models import engine
from handlers import handlers_admin, handlers_export
from logging_config import logger
from services.panel import close_all_panels
from sheduler.backup_db import send_db_backup_cron


async def set_commands() -> None:
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="info", description="Справка по командам (админы)"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    if not TG_TOKEN:
        raise RuntimeError("TG_TOKEN не задан в .env")

    if not DATABASE_PATH.is_file():
        logger.warning("Файл БД пока отсутствует: %s", DATABASE_PATH)

    dp = Dispatcher()
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_export.router)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        send_db_backup_cron,
        trigger="interval",
        minutes=30,
        args=[bot],
        id="db_backup_checker",
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.start()

    await set_commands()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Partner service bot started. DB: %s", DATABASE_PATH)
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logger.error("Polling was cancelled. Cleaning up...")
    finally:
        await bot.session.close()
        await close_all_panels()
        await engine.dispose()
        logger.info("Bot session closed, DB pool disposed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
