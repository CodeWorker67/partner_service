"""Периодический консистентный бэкап SQLite и отправка в CHECKER_ID."""
import asyncio
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import FSInputFile

from config import CHECKER_ID, DATABASE_PATH
from logging_config import logger


def _make_sqlite_backup(src_path: Path, dst_path: Path) -> None:
    """Копия через sqlite3.backup (безопасно при открытой БД ботом)."""
    src_uri = f"file:{src_path.resolve().as_posix()}?mode=ro"
    src = sqlite3.connect(src_uri, uri=True, timeout=60)
    try:
        dst = sqlite3.connect(str(dst_path), timeout=60)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


async def send_db_backup_cron(bot: Optional[Bot] = None) -> None:
    if CHECKER_ID is None:
        logger.debug("backup_db: CHECKER_ID не задан, пропуск")
        return

    from bot import bot as global_bot

    tg_bot = bot or global_bot
    if tg_bot is None:
        logger.warning("backup_db: бот не инициализирован")
        return

    if not DATABASE_PATH.is_file():
        logger.error("backup_db: файл БД отсутствует: %s", DATABASE_PATH)
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    filename = f"partner_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
    tmp_path: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".db",
            prefix="partner_backup_",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)

        await asyncio.to_thread(_make_sqlite_backup, DATABASE_PATH, tmp_path)
        size_mb = tmp_path.stat().st_size / (1024 * 1024)

        await tg_bot.send_document(
            chat_id=CHECKER_ID,
            document=FSInputFile(tmp_path, filename=filename),
            caption=f"📦 Бэкап partner.db\n{size_mb:.2f} МБ · {ts}",
        )
        logger.info("backup_db: отправлено в CHECKER_ID (%.2f МБ)", size_mb)
    except Exception as e:
        logger.error("backup_db: %s", e, exc_info=True)
        try:
            await tg_bot.send_message(CHECKER_ID, f"❌ Ошибка бэкапа БД: {e}")
        except Exception as notify_err:
            logger.warning("backup_db: не удалось уведомить CHECKER_ID: %s", notify_err)
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError as unlink_err:
                logger.warning("backup_db: не удалось удалить %s: %s", tmp_path, unlink_err)
