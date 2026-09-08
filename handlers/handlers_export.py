from datetime import date, datetime
import asyncio
import os
import tempfile
from typing import Any, Iterable, List

import openpyxl
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from openpyxl.styles import Alignment, Border, Side
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy.orm import DeclarativeBase

from bot import sql
from config import ADMIN_IDS
from config_bd.models import (
    Gifts,
    Online,
    PartnerBotSettings,
    PaymentsCryptobot,
    PaymentsFkSBP,
    PaymentsStars,
    Users,
)
from logging_config import logger

router = Router()

_EXCEL_COL_WIDTH_MAX = 255

_EXPORT_SHEETS: list[tuple[str, type[DeclarativeBase]]] = [
    ("partner_bot_settings", PartnerBotSettings),
    ("users", Users),
    ("gifts", Gifts),
    ("payments_fk_sbp", PaymentsFkSBP),
    ("payments_stars", PaymentsStars),
    ("payments_cryptobot", PaymentsCryptobot),
    ("online", Online),
]

_HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center")
_THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)


def _excel_scalar(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return value


def _column_names(model: type[DeclarativeBase]) -> list[str]:
    return [c.key for c in model.__table__.columns]


_SETTINGS_LEADING_COLUMNS = (
    "bot_id",
    "bot_username",
    "bot_display_name",
    "source_bot_id",
    "owner_tg_id",
    "partner_username",
)


def _export_columns(sheet_key: str, model: type[DeclarativeBase]) -> list[str]:
    default = _column_names(model)
    if sheet_key == "partner_bot_settings":
        placed = set(_SETTINGS_LEADING_COLUMNS)
        rest = [c for c in default if c not in placed]
        return list(_SETTINGS_LEADING_COLUMNS) + rest

    if "bot_id" in default and "source_bot_id" not in default:
        cols = list(default)
        cols.insert(cols.index("bot_id") + 1, "source_bot_id")
        return cols

    return default


def _source_bot_map(settings_rows: list[Any]) -> dict[int, Any]:
    return {row.bot_id: row.source_bot_id for row in settings_rows}


def _cell_value(row: Any, column: str, source_bot_by_bot_id: dict[int, Any]) -> Any:
    if column == "source_bot_id" and not hasattr(row, "source_bot_id"):
        bot_id = getattr(row, "bot_id", None)
        if bot_id is None:
            return None
        return source_bot_by_bot_id.get(bot_id)
    return getattr(row, column, None)


def _write_sheet(
    wb: openpyxl.Workbook,
    sheet_title: str,
    rows: Iterable[Any],
    model: type[DeclarativeBase],
    *,
    source_bot_by_bot_id: dict[int, Any] | None = None,
) -> Worksheet:
    ws = wb.create_sheet(title=sheet_title)
    columns = _export_columns(sheet_title, model)
    source_map = source_bot_by_bot_id or {}

    for col_num, title in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_num, value=title)
        cell.alignment = _HEADER_ALIGNMENT
        cell.border = _THIN_BORDER

    for row_num, row in enumerate(rows, 2):
        row_data = [_excel_scalar(_cell_value(row, name, source_map)) for name in columns]
        for col_num, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_num, column=col_num, value=value)
            cell.border = _THIN_BORDER

    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, _EXCEL_COL_WIDTH_MAX)

    ws.freeze_panes = ws["A2"]
    return ws


def _build_export_workbook(snapshot: dict[str, list[Any]]) -> str:
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        wb.remove(wb["Sheet"])

    source_bot_by_bot_id = _source_bot_map(snapshot["partner_bot_settings"])

    for sheet_key, model in _EXPORT_SHEETS:
        _write_sheet(
            wb,
            sheet_key,
            snapshot[sheet_key],
            model,
            source_bot_by_bot_id=source_bot_by_bot_id,
        )

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    return path


def _count_confirmed(rows: list[Any], status_field: str = "status", ok_values: tuple[str, ...] = ("confirmed",)) -> int:
    return sum(1 for r in rows if getattr(r, status_field, None) in ok_values)


def _at(username: str | None) -> str:
    if not username:
        return "—"
    u = username.lstrip("@")
    return f"@{u}" if u else "—"


def _format_bot_card(row: PartnerBotSettings) -> str:
    balance = row.partner_balance or 0
    own = row.balance_own_bot or 0
    child = row.balance_child_bots or 0
    paid = row.partner_pay or 0
    source = row.source_bot_id if row.source_bot_id is not None else "—"
    name = row.bot_display_name or "—"
    return (
        f"<b>#{row.bot_id}</b> {_at(row.bot_username)} · {name}\n"
        f"├ партнёр: {_at(row.partner_username)} (<code>{row.owner_tg_id}</code>)\n"
        f"├ source_bot_id: <code>{source}</code>\n"
        f"└ баланс: {balance} (own {own} / child {child}), выплачено {paid}"
    )


def _chunk_html(parts: list[str], *, limit: int = 3500) -> list[str]:
    chunks: list[str] = []
    buf = ""
    for part in parts:
        add = part if not buf else f"\n\n{part}"
        if buf and len(buf) + len(add) > limit:
            chunks.append(buf)
            buf = part
        else:
            buf = buf + add if buf else part
    if buf:
        chunks.append(buf)
    return chunks


@router.message(Command(commands=["start"]))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🤖 Partner Service Bot\n\n"
        "Доступные команды:\n"
        "/bots — список партнёрских ботов (identity + балансы)\n"
        "/export — экспорт всех таблиц БД в Excel (только админы)\n"
        "/info — полная справка по командам (только админы)"
    )


@router.message(Command(commands=["bots"]))
async def cmd_bots(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    rows = await sql.list_bot_settings()
    if not rows:
        await message.answer("В partner_bot_settings пока нет записей.")
        return

    header = f"⚙️ Партнёрские боты: <b>{len(rows)}</b>"
    chunks = _chunk_html([header] + [_format_bot_card(r) for r in rows])

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


@router.message(Command(commands=["export"]))
async def export_database_to_excel(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    try:
        await message.answer("🔄 Начинаю экспорт базы данных (все таблицы, все столбцы)...")

        snapshot = await sql.get_export_snapshot()
        export_path = await asyncio.to_thread(_build_export_workbook, snapshot)

        users_count = len(snapshot["users"])
        gifts_count = len(snapshot["gifts"])
        settings_count = len(snapshot["partner_bot_settings"])
        fk_count = len(snapshot["payments_fk_sbp"])
        stars_count = len(snapshot["payments_stars"])
        crypto_count = len(snapshot["payments_cryptobot"])
        online_count = len(snapshot["online"])

        fk_ok = _count_confirmed(snapshot["payments_fk_sbp"])
        stars_ok = _count_confirmed(snapshot["payments_stars"])
        crypto_ok = _count_confirmed(snapshot["payments_cryptobot"], ok_values=("paid", "confirmed"))

        try:
            now_s = datetime.now().strftime("%d.%m.%Y %H:%M")
            caption = (
                "📊 Экспорт partner.db\n"
                "🧾 Все таблицы и все столбцы.\n"
                f"📅 Создано: {now_s}\n\n"
                "📊 Статистика:\n"
                f"├ ⚙️ Настроек ботов: {settings_count}\n"
                f"├ 👥 Пользователей: {users_count}\n"
                f"├ 🎁 Подарков: {gifts_count}\n"
                f"├ 💳 Платежей FreeKassa: {fk_ok}/{fk_count}\n"
                f"├ ⭐ Платежей Stars: {stars_ok}/{stars_count}\n"
                f"├ 💎 Платежей Cryptobot: {crypto_ok}/{crypto_count}\n"
                f"└ 📈 Записей online: {online_count}"
            )
            await message.answer_document(
                document=FSInputFile(
                    export_path,
                    filename=f"partner_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                ),
                caption=caption,
                parse_mode="HTML",
            )
        finally:
            try:
                os.remove(export_path)
            except OSError:
                pass

        logger.info("Администратор %s экспортировал базу данных в Excel", message.from_user.id)

    except Exception as e:
        error_message = f"❌ Ошибка при экспорте базы данных: {e}"
        logger.error(error_message)
        logger.exception("Детали ошибки:")
        await message.answer(error_message)
