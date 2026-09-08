"""Админ-команды partner service: /pay, /sub, /find, /add_traffic, /pay_to_owner, /info."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import sql
from config import ADMIN_IDS
from config_bd.db_utils import parse_user_profile
from config_bd.models import Users
from lexicon import LEXICON
from logging_config import logger
from services.notify import is_telegram_chat_id, notify_via_bot
from services.panel import get_panel_client
from services.tariff import (
    hwid_limit_from_device_slots,
    panel_username,
    parse_tg_bot_target,
    tier_from_device_slots,
    tier_label,
)
from services.wl_traffic import (
    any_pro_user_on_limited_squad,
    fetch_all_pro_panel_users,
    get_wl_used_gb_for_user,
    reassign_all_pro_to_active,
)

router = Router()

_INFO_TEXT = (
    "📖 <b>Справка по командам Partner Service</b>\n\n"
    "<b>Общие</b>\n"
    "/start — краткий список команд\n"
    "/info — эта справка (только админы)\n\n"
    "<b>Данные и экспорт</b>\n"
    "/bots — список активных партнёрских ботов с балансами\n"
    "Пример: <code>/bots</code>\n\n"
    "/export — выгрузка всей БД partner.db в Excel\n"
    "Пример: <code>/export</code>\n\n"
    "<b>Пользователи</b>\n"
    "/pay — подписки (БД + панель), WL-трафик и платежи по всем ботам\n"
    "Пример: <code>/pay 123456789</code>\n\n"
    "/find — карточка пользователя по всем ботам (с кнопками +дни/+ГБ)\n"
    "Пример: <code>/find 123456789</code>\n\n"
    "<b>Управление подписками</b>\n"
    "/sub — установить дату подписки в панели и БД бота\n"
    "• 5 устройств: <code>/sub 123456789_7 2026-09-15 07:48:05</code>\n"
    "• 3 устройства: <code>/sub 123456789_7_3 2026-09-15 07:48:05</code>\n"
    "• 10 устройств: <code>/sub 123456789_7_10 2026-09-15 07:48:05</code>\n\n"
    "<b>WL-трафик (Антиглушилка)</b>\n"
    "/add_traffic — добавить GB к лимиту пользователя\n"
    "Пример: <code>/add_traffic 123456789_7 10</code>\n\n"
    "<b>Выплаты партнёрам</b>\n"
    "/pay_to_owner — зафиксировать выплату владельцу бота\n"
    "Пример: <code>/pay_to_owner 7 3000</code>\n\n"
    "<i>Формат tg_id_bot_id: telegram_id и bot_id через подчёркивание.\n"
    "Уведомления пользователям и владельцам отправляются через TG_TOKEN "
    "из /root/{bot_id}/.env</i>"
)

_SUB_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
)

_DEVICE_SLOTS_LABEL = {3: "3 устройства", 5: "5 устройств", 10: "10 устройств"}


@dataclass(frozen=True)
class LongestSubscription:
    tg_id: int
    bot_id: int
    device_slots: int
    end_date: Optional[datetime]
    bot_username: str

    @property
    def label(self) -> str:
        uname = self.bot_username.lstrip("@")
        tier = _DEVICE_SLOTS_LABEL.get(self.device_slots, f"{self.device_slots} устройств")
        return f"{tier} · @{uname} (bot_id: {self.bot_id})"


def _admin_only_message(message: Message) -> bool:
    if message.from_user.id not in ADMIN_IDS:
        return False
    return True


def _admin_only_callback(callback: CallbackQuery) -> bool:
    if callback.from_user.id not in ADMIN_IDS:
        return False
    return True


def _normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_longest_subscription(users: List[Users]) -> Optional[LongestSubscription]:
    candidates: List[tuple[int, int, int, datetime]] = []
    for user in users:
        for slots, end_dt in (
            (3, user.subscription_3_end_date),
            (5, user.subscription_end_date),
            (10, user.subscription_10_end_date),
        ):
            normalized = _normalize_dt(end_dt)
            if normalized is not None:
                candidates.append((user.bot_id, user.user_id, slots, normalized))

    if not candidates:
        if not users:
            return None
        user = users[0]
        return LongestSubscription(
            tg_id=user.user_id,
            bot_id=user.bot_id,
            device_slots=5,
            end_date=None,
            bot_username="—",
        )

    candidates.sort(key=lambda x: x[3], reverse=True)
    bot_id, tg_id, slots, end_dt = candidates[0]
    return LongestSubscription(
        tg_id=tg_id,
        bot_id=bot_id,
        device_slots=slots,
        end_date=end_dt,
        bot_username="—",
    )


async def _longest_subscription_with_username(users: List[Users]) -> Optional[LongestSubscription]:
    base = _resolve_longest_subscription(users)
    if base is None:
        return None
    settings = await sql.get_bot_settings(base.bot_id)
    bot_username = "—"
    if settings:
        bot_username = settings.bot_username or settings.bot_display_name or "—"
    return LongestSubscription(
        tg_id=base.tg_id,
        bot_id=base.bot_id,
        device_slots=base.device_slots,
        end_date=base.end_date,
        bot_username=bot_username,
    )


def _find_keyboard(longest: LongestSubscription) -> InlineKeyboardMarkup:
    tg_id = longest.tg_id
    bot_id = longest.bot_id
    slots = longest.device_slots
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+ 7 дней", callback_data=f"find:d7:{tg_id}:{bot_id}:{slots}"),
                InlineKeyboardButton(text="+ 30 дней", callback_data=f"find:d30:{tg_id}:{bot_id}:{slots}"),
                InlineKeyboardButton(text="+ 90 дней", callback_data=f"find:d90:{tg_id}:{bot_id}:{slots}"),
            ],
            [
                InlineKeyboardButton(text="+ 10 ГБ", callback_data=f"find:g10:{tg_id}:{bot_id}"),
                InlineKeyboardButton(text="+ 50 ГБ", callback_data=f"find:g50:{tg_id}:{bot_id}"),
            ],
        ]
    )


def _dt_str(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _dt_str_find(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d.%m.%Y")


def _msk_dt_str(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    msk = dt.astimezone(ZoneInfo("Europe/Moscow"))
    return msk.strftime("%d.%m.%Y %H:%M")


def _split_long_text(text: str, limit: int = 3500) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def _parse_datetime(raw: str) -> Optional[datetime]:
    for fmt in _SUB_DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _subscription_active_until(dt: Optional[datetime]) -> tuple[str, Optional[int]]:
    if dt is None:
        return "не активна", None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if dt <= now:
        return "истекла", 0
    days_left = (dt.date() - now.date()).days
    return f"активна до {_dt_str_find(dt)} (осталось {days_left} дн.)", days_left


def _best_subscription(user: Users) -> tuple[int, Optional[datetime]]:
    """Возвращает (device_slots, end_date) для наиболее актуальной подписки."""
    candidates = [
        (3, user.subscription_3_end_date),
        (5, user.subscription_end_date),
        (10, user.subscription_10_end_date),
    ]
    now = datetime.now(timezone.utc)
    active = [(s, dt) for s, dt in candidates if dt and (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt) > now]
    if active:
        active.sort(key=lambda x: x[1], reverse=True)
        return active[0]
    non_null = [(s, dt) for s, dt in candidates if dt]
    if non_null:
        non_null.sort(key=lambda x: x[1], reverse=True)
        return non_null[0]
    return 5, None


def _device_display_name(device: dict) -> str:
    model = (device.get("deviceModel") or "").strip()
    platform = (device.get("platform") or "").strip()
    os_version = (device.get("osVersion") or "").strip()
    if model and platform:
        name = f"{model} · {platform}"
    elif model:
        name = model
    elif platform:
        name = platform
    else:
        hwid = (device.get("hwid") or "устройство")[:12]
        name = f"Устройство {hwid}"
    if os_version:
        name = f"{name} {os_version}"
    return name


async def _format_pay_block(user: Users, bot_settings) -> str:
    bot_id = user.bot_id
    tg_id = user.user_id
    bot_username = bot_settings.bot_username or bot_settings.bot_display_name or "—"

    panel = await get_panel_client(bot_id)
    panel_lines: dict[int, str] = {}
    if panel:
        for slots in (3, 5, 10):
            uname = panel_username(tg_id, bot_id, device_slots=slots)
            try:
                ar = await panel.activ(uname)
                panel_lines[slots] = ar.get("time") or "—"
            except Exception as e:
                logger.exception("/pay panel bot_id=%s slots=%s", bot_id, slots)
                panel_lines[slots] = f"Ошибка: {e}"
    else:
        for slots in (3, 5, 10):
            panel_lines[slots] = "Панель недоступна"

    db_dates = {
        3: user.subscription_3_end_date,
        5: user.subscription_end_date,
        10: user.subscription_10_end_date,
    }

    trafic_wl, limit_wl = await sql.get_wl_limits(bot_id, tg_id)
    used_gb = 0.0
    if panel:
        used_gb = await get_wl_used_gb_for_user(panel, tg_id, bot_id, trafic_wl)
    remaining_gb = max(0.0, round(limit_wl - used_gb, 2))

    pay_rows = await sql.get_user_subscription_payment_report(bot_id, tg_id)
    pay_lines = []
    for tc, kind, days_s in pay_rows:
        days_part = f" {days_s} дн." if days_s and days_s != "—" else ""
        pay_lines.append(f"• {_dt_str(tc)} — {kind}{days_part}")

    body = (
        f"Бот: @{bot_username}\n"
        f"Номер бота: {bot_id}\n"
        f"Подписка 3 уст в БД бота — {_dt_str(db_dates[3])}\n"
        f"Подписка 3 уст в панели — {panel_lines[3]}\n"
        f"Подписка 5 уст в БД бота — {_dt_str(db_dates[5])}\n"
        f"Подписка 5 уст в панели — {panel_lines[5]}\n"
        f"Подписка 10 уст в БД бота — {_dt_str(db_dates[10])}\n"
        f"Подписка 10 уст в панели — {panel_lines[10]}\n\n"
        f"📡 <b>Антиглушилка (WL-трафик)</b>\n"
        f"├ Лимит: {limit_wl:.2f} GB\n"
        f"├ Использовано: {used_gb:.2f} GB\n"
        f"└ Осталось: {remaining_gb:.2f} GB\n\n"
        f"<b>Платежи:</b>\n"
    )
    body += "\n".join(pay_lines) if pay_lines else "Нет"
    return body


async def _format_find_block(user: Users, bot_settings) -> str:
    bot_id = user.bot_id
    tg_id = user.user_id
    bot_username = bot_settings.bot_username or bot_settings.bot_display_name or "—"

    slots, sub_dt = _best_subscription(user)
    sub_status, _ = _subscription_active_until(sub_dt)

    device_lines: List[str] = []
    device_count = 0
    panel = await get_panel_client(bot_id)
    if panel:
        uname = panel_username(tg_id, bot_id, device_slots=slots)
        data = await panel.get_user_by_username(uname)
        panel_user = panel._panel_user_from_response(data)
        if panel_user:
            panel_user_id = panel._panel_user_id(panel_user)
            if panel_user_id is not None:
                devices, device_count = await panel.get_user_hwid_devices(str(panel_user_id))
                device_lines = [_device_display_name(d) for d in devices[:10]]

    trafic_wl, limit_wl = await sql.get_wl_limits(bot_id, tg_id)
    used_gb = 0.0
    if panel:
        used_gb = await get_wl_used_gb_for_user(panel, tg_id, bot_id, trafic_wl)
    remaining_gb = max(0.0, round(limit_wl - used_gb, 1))

    pay_rows = await sql.get_user_payments_with_amounts(bot_id, tg_id, limit=5)
    pay_lines = []
    for tc, days_s, device_label, amount in pay_rows:
        pay_lines.append(f"  {_dt_str_find(tc)}  {days_s} дн.  {device_label}  {amount} ₽")

    gift = await sql.get_activated_gift(bot_id, tg_id)
    gift_line = ""
    if gift:
        gift_line = "\nПодарочная ссылка: активирована"

    reg_dt = _dt_str_find(user.create_user)
    is_owner = "Да" if bot_settings.owner_tg_id == tg_id else "Нет"

    lines = [
        f"📦 Проект: @{bot_username} (bot_id: {bot_id})",
        "",
        f"Подписка {slots} устройства:   {sub_status}",
        f"Устройства: {device_count}",
    ]
    lines.extend(device_lines)
    lines.extend([
        "",
        f"Трафик:     {used_gb:.1f} / {limit_wl:.0f} ГБ  (осталось {remaining_gb:.1f} ГБ)",
        "",
        "Последние оплаты:",
    ])
    if pay_lines:
        lines.extend(pay_lines)
    else:
        lines.append("  —")
    if gift_line:
        lines.append(gift_line)
    lines.extend([
        f"Регистрация в боте: {reg_dt}",
        f"Владелец в боте: {is_owner}",
    ])
    return "\n".join(lines)


def _format_find_user_header(profile: dict) -> str:
    full_name = (profile.get("full_name") or "").strip()
    username = (profile.get("username") or "").strip().lstrip("@")
    if full_name and username:
        return f"{full_name} (@{username})"
    if full_name:
        return full_name
    if username:
        return f"@{username}"
    return ""


async def _build_find_message(tg_id: int, *, notice: str = "") -> tuple[str, Optional[InlineKeyboardMarkup]]:
    users = await sql.list_users_by_tg_id(tg_id)
    if not users:
        return f"❌ Пользователь {tg_id} не найден.", None

    profile = parse_user_profile(users[0].field_str_2)
    name = _format_find_user_header(profile)

    blocks: List[str] = [f"👤 {tg_id}"]
    if name:
        blocks[0] += f"\n{name}"

    for user in users:
        settings = await sql.get_bot_settings(user.bot_id)
        if not settings:
            continue
        blocks.append(await _format_find_block(user, settings))

    longest = await _longest_subscription_with_username(users)
    footer = ""
    keyboard = None
    if longest:
        footer = (
            f"\n\n<i>Добавление подписки и лимитов происходит к самой длинной подписке — "
            f"{longest.label}</i>"
        )
        keyboard = _find_keyboard(longest)

    notice_line = f"\n\n✅ {notice}" if notice else ""
    full_text = "\n\n".join(blocks) + footer + notice_line
    return full_text, keyboard


async def _apply_find_add_days(
    tg_id: int, bot_id: int, device_slots: int, days: int
) -> tuple[bool, str]:
    user = await sql.get_user(bot_id, tg_id)
    if not user:
        return False, "Пользователь не найден"

    panel = await get_panel_client(bot_id)
    if not panel:
        return False, f"Панель бота {bot_id} недоступна"

    username = panel_username(tg_id, bot_id, device_slots=device_slots)
    tier = tier_from_device_slots(device_slots)
    hw_lim = hwid_limit_from_device_slots(device_slots)

    success, new_date = await panel.add_subscription_days(
        username, tg_id, days, hwid_device_limit=hw_lim
    )
    if not success or new_date is None:
        return False, "Не удалось продлить подписку в панели"

    await sql.update_subscription_end_date(bot_id, tg_id, new_date, slot=tier)

    if is_telegram_chat_id(tg_id):
        sub_link = await panel.sublink(username)
        user_text = LEXICON["sub_granted_notify"].format(
            tier=tier_label(tier),
            end_date=_msk_dt_str(new_date),
        )
        kb = None
        if sub_link:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔑 Подключить VPN", url=sub_link)]]
            )
        await notify_via_bot(bot_id, tg_id, user_text, reply_markup=kb)

    return True, f"+{days} дн. → {_msk_dt_str(new_date)}"


async def _apply_find_add_traffic(tg_id: int, bot_id: int, gb: float) -> tuple[bool, str]:
    user = await sql.get_user(bot_id, tg_id)
    if not user:
        return False, "Пользователь не найден"

    panel = await get_panel_client(bot_id)
    trafic_wl, _ = await sql.get_wl_limits(bot_id, tg_id)
    used_gb = 0.0
    if panel:
        used_gb = await get_wl_used_gb_for_user(panel, tg_id, bot_id, trafic_wl)

    await sql.add_wl_limit(bot_id, tg_id, gb)
    _, limit_wl = await sql.get_wl_limits(bot_id, tg_id)
    remaining_gb = max(0.0, round(limit_wl - used_gb, 2))

    if panel and limit_wl > used_gb:
        panel_users = await fetch_all_pro_panel_users(panel, tg_id, bot_id)
        if any_pro_user_on_limited_squad(panel_users):
            await reassign_all_pro_to_active(panel, tg_id, bot_id)

    if is_telegram_chat_id(tg_id):
        text = LEXICON["wl_traffic_admin_grant"].format(
            gb=gb,
            limit_gb=limit_wl,
            used_gb=used_gb,
            remaining_gb=remaining_gb,
        )
        await notify_via_bot(bot_id, tg_id, text)

    return True, f"+{gb:g} ГБ → лимит {limit_wl:.1f} ГБ"


@router.message(Command(commands=["pay"]))
async def cmd_pay(message: Message):
    if not _admin_only_message(message):
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("❌ Использование: /pay <telegram_id>\nНапример: /pay 123456789")
        return

    try:
        tg_id = int(args[1].strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return

    users = await sql.list_users_by_tg_id(tg_id)
    if not users:
        await message.answer(f"❌ Пользователь {tg_id} не найден ни в одном боте.")
        return

    blocks: List[str] = []
    for user in users:
        settings = await sql.get_bot_settings(user.bot_id)
        if not settings:
            continue
        blocks.append(await _format_pay_block(user, settings))

    if not blocks:
        await message.answer(f"❌ Настройки ботов для пользователя {tg_id} не найдены.")
        return

    full_text = f"<b>/pay {tg_id}</b>\n\n" + "\n\n—\n\n".join(blocks)
    for chunk in _split_long_text(full_text):
        await message.answer(chunk, parse_mode="HTML")


@router.message(Command(commands=["find"]))
async def cmd_find(message: Message):
    if not _admin_only_message(message):
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("❌ Использование: /find <telegram_id>")
        return

    try:
        tg_id = int(args[1].strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return

    text, keyboard = await _build_find_message(tg_id)
    if keyboard is None:
        await message.answer(text, parse_mode="HTML")
        return

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("find:"))
async def find_action_callback(callback: CallbackQuery):
    if not _admin_only_callback(callback):
        await callback.answer("❌ Только для админов", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("❌ Некорректные данные", show_alert=True)
        return

    action = parts[1]
    try:
        tg_id = int(parts[2])
        bot_id = int(parts[3])
    except ValueError:
        await callback.answer("❌ Некорректный ID", show_alert=True)
        return

    notice = ""
    if action in ("d7", "d30", "d90"):
        if len(parts) < 5:
            await callback.answer("❌ Некорректные данные", show_alert=True)
            return
        days_map = {"d7": 7, "d30": 30, "d90": 90}
        days = days_map[action]
        try:
            device_slots = int(parts[4])
        except ValueError:
            await callback.answer("❌ Некорректный слот", show_alert=True)
            return
        ok, msg = await _apply_find_add_days(tg_id, bot_id, device_slots, days)
        if not ok:
            await callback.answer(msg, show_alert=True)
            return
        notice = msg
    elif action in ("g10", "g50"):
        gb_map = {"g10": 10.0, "g50": 50.0}
        gb = gb_map[action]
        ok, msg = await _apply_find_add_traffic(tg_id, bot_id, gb)
        if not ok:
            await callback.answer(msg, show_alert=True)
            return
        notice = msg
    else:
        await callback.answer("❌ Неизвестное действие", show_alert=True)
        return

    text, keyboard = await _build_find_message(tg_id, notice=notice)
    try:
        if keyboard is None:
            await callback.message.edit_text(text, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.warning("find callback edit_text failed: {}", e)
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer(notice)


@router.message(Command(commands=["sub"]))
async def cmd_sub(message: Message):
    if not _admin_only_message(message):
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    args = (message.text or "").split()
    if len(args) < 3:
        await message.answer(
            "❌ Использование:\n"
            "  /sub <tg_id>_<bot_id> <дата_время>           — 5 устройств\n"
            "  /sub <tg_id>_<bot_id>_3 <дата_время>         — 3 устройства\n"
            "  /sub <tg_id>_<bot_id>_10 <дата_время>        — 10 устройств\n"
            "Пример: /sub 123456789_7 2026-09-15 07:48:05"
        )
        return

    try:
        tg_id, bot_id, device_slots = parse_tg_bot_target(args[1])
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return

    date_str = " ".join(args[2:])
    target_date = _parse_datetime(date_str)
    if target_date is None:
        await message.answer(f"❌ Неверный формат даты: {date_str}")
        return

    user = await sql.get_user(bot_id, tg_id)
    if not user:
        await message.answer(f"❌ Пользователь {tg_id} не найден в боте {bot_id}.")
        return

    panel = await get_panel_client(bot_id)
    if not panel:
        await message.answer(f"❌ Панель бота {bot_id} недоступна (проверьте /root/{bot_id}/.env).")
        return

    username = panel_username(tg_id, bot_id, device_slots=device_slots)
    tier = tier_from_device_slots(device_slots)
    hw_lim = hwid_limit_from_device_slots(device_slots)

    try:
        success, actual_date = await panel.set_expiration_date(
            username, target_date, tg_id, hwid_device_limit=hw_lim
        )
    except Exception as e:
        logger.exception("/sub panel error")
        await message.answer(f"❌ Ошибка панели: {e}")
        return

    if not success or actual_date is None:
        panel_ok = await panel.test_connect()
        hint = (
            "Панель не отвечает — проверьте PANEL_URL."
            if not panel_ok
            else "Пользователь не найден и не удалось создать."
        )
        await message.answer(f"❌ Не удалось установить дату в панели.\n{hint}")
        return

    await sql.update_subscription_end_date(bot_id, tg_id, actual_date, slot=tier)

    notify_status = ""
    if is_telegram_chat_id(tg_id):
        sub_link = await panel.sublink(username)
        user_text = LEXICON["sub_granted_notify"].format(
            tier=tier_label(tier),
            end_date=_msk_dt_str(actual_date),
        )
        kb = None
        if sub_link:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔑 Подключить VPN", url=sub_link)]]
            )
        ok = await notify_via_bot(bot_id, tg_id, user_text, reply_markup=kb)
        notify_status = "\n📨 Пользователь уведомлён." if ok else "\n⚠️ Не удалось уведомить пользователя."

    await message.answer(
        f"✅ Подписка установлена\n\n"
        f"👤 {tg_id} · bot_id={bot_id}\n"
        f"🔑 Панель: {username}\n"
        f"📅 UTC: {actual_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📝 Тариф: {tier_label(tier)}\n"
        f"💾 БД обновлена.{notify_status}"
    )


@router.message(Command(commands=["add_traffic"]))
async def cmd_add_traffic(message: Message):
    if not _admin_only_message(message):
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    args = (message.text or "").split()
    if len(args) < 3:
        await message.answer(
            "❌ Использование: /add_traffic <tg_id>_<bot_id> <GB>\n"
            "Пример: /add_traffic 123456789_7 10"
        )
        return

    try:
        tg_id, bot_id, _ = parse_tg_bot_target(args[1])
        gb = float(args[2].strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: /add_traffic 123456789_7 10")
        return

    if gb <= 0:
        await message.answer("❌ Количество GB должно быть больше 0.")
        return

    user = await sql.get_user(bot_id, tg_id)
    if not user:
        await message.answer(f"❌ Пользователь {tg_id} не найден в боте {bot_id}.")
        return

    panel = await get_panel_client(bot_id)
    trafic_wl, _ = await sql.get_wl_limits(bot_id, tg_id)
    used_gb = 0.0
    if panel:
        used_gb = await get_wl_used_gb_for_user(panel, tg_id, bot_id, trafic_wl)

    await sql.add_wl_limit(bot_id, tg_id, gb)
    _, limit_wl = await sql.get_wl_limits(bot_id, tg_id)
    remaining_gb = max(0.0, round(limit_wl - used_gb, 2))

    squad_note = ""
    if panel and limit_wl > used_gb:
        panel_users = await fetch_all_pro_panel_users(panel, tg_id, bot_id)
        if any_pro_user_on_limited_squad(panel_users):
            n = await reassign_all_pro_to_active(panel, tg_id, bot_id)
            if n:
                squad_note = f"\n✅ Squad → active, {n} акк."

    await message.answer(
        f"✅ Добавлено {gb:g} GB для <code>{tg_id}</code> (bot_id={bot_id}){squad_note}\n\n"
        f"├ Использовано: <b>{used_gb:.2f} GB</b>\n"
        f"├ Лимит: <b>{limit_wl:.2f} GB</b>\n"
        f"└ Осталось: <b>{remaining_gb:.2f} GB</b>",
        parse_mode="HTML",
    )

    if is_telegram_chat_id(tg_id):
        text = LEXICON["wl_traffic_admin_grant"].format(
            gb=gb,
            limit_gb=limit_wl,
            used_gb=used_gb,
            remaining_gb=remaining_gb,
        )
        ok = await notify_via_bot(bot_id, tg_id, text)
        if not ok:
            await message.answer("⚠️ Лимит добавлен, но push пользователю не отправлен.")


@router.message(Command(commands=["pay_to_owner"]))
async def cmd_pay_to_owner(message: Message):
    if not _admin_only_message(message):
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    args = (message.text or "").split()
    if len(args) < 3:
        await message.answer(
            "❌ Использование: /pay_to_owner <bot_id> <сумма>\n"
            "Пример: /pay_to_owner 7 3000"
        )
        return

    try:
        bot_id = int(args[1].strip())
        amount = int(args[2].strip())
    except ValueError:
        await message.answer("❌ bot_id и сумма должны быть целыми числами.")
        return

    if amount == 0:
        await message.answer("❌ Сумма не может быть 0.")
        return

    settings = await sql.get_bot_settings(bot_id)
    if not settings:
        await message.answer(f"❌ Бот {bot_id} не найден.")
        return

    ok, err = await sql.add_owner_partner_pay(bot_id, amount)
    if not ok:
        await message.answer(f"❌ {err}")
        return

    updated = await sql.get_bot_settings(bot_id)
    total = updated.partner_balance or 0
    paid = updated.partner_pay or 0
    current = max(total - paid, 0)

    owner_tg_id = settings.owner_tg_id
    notify_text = LEXICON["pay_to_owner_notify"].format(
        amount=amount,
        total=total,
        paid=paid,
        current=current,
    )
    notified = await notify_via_bot(bot_id, owner_tg_id, notify_text)

    action = f"К partner_pay добавлено {amount} ₽" if amount > 0 else f"Коррекция partner_pay: {amount} ₽"
    notify_line = "\n📨 Владелец уведомлён." if notified else "\n⚠️ Не удалось уведомить владельца."
    await message.answer(
        f"✅ {action} (bot_id={bot_id}).\n\n"
        f"Всего заработано: {total} ₽\n"
        f"Выведено: {paid} ₽\n"
        f"Текущий баланс: {current} ₽{notify_line}"
    )


@router.message(Command(commands=["info"]))
async def cmd_info(message: Message):
    if not _admin_only_message(message):
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    for chunk in _split_long_text(_INFO_TEXT):
        await message.answer(chunk, parse_mode="HTML")
