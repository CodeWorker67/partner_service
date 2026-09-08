import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select, update

from config_bd.models import (
    AsyncSessionLocal,
    Gifts,
    Online,
    PartnerBotSettings,
    PaymentsCryptobot,
    PaymentsFkSBP,
    PaymentsStars,
    Users,
)


def parse_user_profile(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


class DbReader:
    async def get_export_snapshot(self) -> Dict[str, List[Any]]:
        """Одна сессия БД: все SELECT для /export подряд."""
        async with AsyncSessionLocal() as session:
            settings_list = (await session.execute(select(PartnerBotSettings))).scalars().all()
            users_list = (await session.execute(select(Users))).scalars().all()
            gifts_list = (await session.execute(select(Gifts))).scalars().all()
            payments_fk_sbp_list = (await session.execute(select(PaymentsFkSBP))).scalars().all()
            payments_stars_list = (await session.execute(select(PaymentsStars))).scalars().all()
            payments_cryptobot_list = (await session.execute(select(PaymentsCryptobot))).scalars().all()
            online_list = (await session.execute(select(Online))).scalars().all()
        return {
            "partner_bot_settings": settings_list,
            "users": users_list,
            "gifts": gifts_list,
            "payments_fk_sbp": payments_fk_sbp_list,
            "payments_stars": payments_stars_list,
            "payments_cryptobot": payments_cryptobot_list,
            "online": online_list,
        }

    async def list_bot_settings(self) -> List[PartnerBotSettings]:
        async with AsyncSessionLocal() as session:
            stmt = select(PartnerBotSettings).order_by(PartnerBotSettings.bot_id.asc())
            return list((await session.execute(stmt)).scalars().all())

    async def get_bot_settings(self, bot_id: int) -> Optional[PartnerBotSettings]:
        async with AsyncSessionLocal() as session:
            return await session.get(PartnerBotSettings, bot_id)

    async def list_users_by_tg_id(self, tg_id: int) -> List[Users]:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(Users)
                .where(Users.user_id == tg_id, Users.is_delete == False)
                .order_by(Users.bot_id.asc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def get_user(self, bot_id: int, user_id: int) -> Optional[Users]:
        async with AsyncSessionLocal() as session:
            stmt = select(Users).where(Users.bot_id == bot_id, Users.user_id == user_id)
            return (await session.execute(stmt)).scalar_one_or_none()

    async def update_subscription_end_date(
        self, bot_id: int, user_id: int, end_date: datetime, slot: str = "main"
    ) -> None:
        col = {
            "main": Users.subscription_end_date,
            "3": Users.subscription_3_end_date,
            "10": Users.subscription_10_end_date,
        }.get(slot, Users.subscription_end_date)
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Users)
                .where(Users.bot_id == bot_id, Users.user_id == user_id)
                .values({col.key: end_date})
            )
            await session.commit()

    async def get_wl_limits(self, bot_id: int, user_id: int) -> Tuple[float, float]:
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(Users.trafic_wl, Users.limit_wl).where(
                        Users.bot_id == bot_id, Users.user_id == user_id
                    )
                )
            ).one_or_none()
            if row is None:
                return 0.0, 0.0
            return float(row[0] or 0.0), float(row[1] or 0.0)

    async def add_wl_limit(self, bot_id: int, user_id: int, gb: float) -> None:
        async with AsyncSessionLocal() as session:
            user = (
                await session.execute(
                    select(Users).where(Users.bot_id == bot_id, Users.user_id == user_id)
                )
            ).scalar_one_or_none()
            if user is None:
                return
            current = float(user.limit_wl or 0.0)
            user.limit_wl = round(current + gb, 2)
            if gb > 0:
                user.field_bool_2 = False
            await session.commit()

    async def update_field_bool_2(self, bot_id: int, user_id: int, value: bool) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Users)
                .where(Users.bot_id == bot_id, Users.user_id == user_id)
                .values(field_bool_2=value)
            )
            await session.commit()

    async def add_owner_partner_pay(self, bot_id: int, amount: int) -> Tuple[bool, str]:
        if amount == 0:
            return False, "Сумма не может быть 0"
        async with AsyncSessionLocal() as session:
            row = await session.get(PartnerBotSettings, bot_id)
            if not row:
                return False, f"Бот {bot_id} не найден"
            total = row.partner_balance or 0
            paid = row.partner_pay or 0
            current = max(total - paid, 0)
            if amount > 0:
                if current < amount:
                    return False, f"Недостаточно на балансе: {current} ₽, запрошено {amount} ₽"
            else:
                if paid + amount < 0:
                    return (
                        False,
                        f"Нельзя уменьшить partner_pay ниже 0: выведено {paid} ₽, коррекция {amount} ₽",
                    )
            await session.execute(
                update(PartnerBotSettings)
                .where(PartnerBotSettings.bot_id == bot_id)
                .values(partner_pay=func.coalesce(PartnerBotSettings.partner_pay, 0) + amount)
            )
            await session.commit()
            return True, ""

    async def get_user_subscription_payment_report(
        self, bot_id: int, user_id: int
    ) -> List[Tuple[datetime, str, str]]:
        rows_acc: List[Tuple[datetime, str, str]] = []

        def _parse_map(payload: Optional[str]) -> Dict[str, str]:
            if not payload:
                return {}
            out: Dict[str, str] = {}
            for part in payload.split(","):
                if ":" not in part:
                    continue
                k, _, v = part.partition(":")
                out[k.strip()] = v.strip()
            return out

        def _device_label(m: Dict[str, str]) -> str:
            raw = m.get("device")
            try:
                n = int(raw) if raw is not None else 5
            except (TypeError, ValueError):
                n = 5
            if n == 3:
                return "3 устройства"
            if n == 10:
                return "10 устройств"
            return "5 устройств"

        def _row_kind(payload: Optional[str], is_gift: bool) -> Tuple[str, str]:
            m = _parse_map(payload)
            dur = m.get("duration", "0")
            if dur.startswith("traffic"):
                return f"Трафик {dur.replace('traffic', '')} GB", "—"
            if is_gift:
                return f"Подарок · {_device_label(m)}", dur
            return _device_label(m), dur

        async with AsyncSessionLocal() as session:
            fk = await session.execute(
                select(PaymentsFkSBP.time_created, PaymentsFkSBP.payload, PaymentsFkSBP.is_gift).where(
                    PaymentsFkSBP.bot_id == bot_id,
                    PaymentsFkSBP.user_id == user_id,
                    PaymentsFkSBP.status == "confirmed",
                )
            )
            for tc, payload, is_gift in fk.all():
                kind, days_s = _row_kind(payload, bool(is_gift))
                rows_acc.append((tc, kind, days_s))

            st = await session.execute(
                select(PaymentsStars.time_created, PaymentsStars.payload, PaymentsStars.is_gift).where(
                    PaymentsStars.bot_id == bot_id,
                    PaymentsStars.user_id == user_id,
                    PaymentsStars.status == "confirmed",
                )
            )
            for tc, payload, is_gift in st.all():
                kind, days_s = _row_kind(payload, bool(is_gift))
                rows_acc.append((tc, kind, days_s))

            cr = await session.execute(
                select(PaymentsCryptobot.time_created, PaymentsCryptobot.payload, PaymentsCryptobot.is_gift).where(
                    PaymentsCryptobot.bot_id == bot_id,
                    PaymentsCryptobot.user_id == user_id,
                    PaymentsCryptobot.status == "paid",
                )
            )
            for tc, payload, is_gift in cr.all():
                kind, days_s = _row_kind(payload, bool(is_gift))
                rows_acc.append((tc, kind, days_s))

        rows_acc.sort(key=lambda x: x[0] or datetime.min)
        return rows_acc

    async def get_user_payments_with_amounts(
        self, bot_id: int, user_id: int, *, limit: int = 5
    ) -> List[Tuple[datetime, str, str, int]]:
        """(time_created, days, device_label, amount_rub) newest first."""
        rows: List[Tuple[datetime, str, str, int]] = []

        def _parse_map(payload: Optional[str]) -> Dict[str, str]:
            if not payload:
                return {}
            out: Dict[str, str] = {}
            for part in payload.split(","):
                if ":" not in part:
                    continue
                k, _, v = part.partition(":")
                out[k.strip()] = v.strip()
            return out

        def _device_label(m: Dict[str, str]) -> str:
            raw = m.get("device")
            try:
                n = int(raw) if raw is not None else 5
            except (TypeError, ValueError):
                n = 5
            if n == 3:
                return "3 устройства"
            if n == 10:
                return "10 устройств"
            return "5 устройств"

        async with AsyncSessionLocal() as session:
            fk = await session.execute(
                select(
                    PaymentsFkSBP.time_created,
                    PaymentsFkSBP.payload,
                    PaymentsFkSBP.amount,
                ).where(
                    PaymentsFkSBP.bot_id == bot_id,
                    PaymentsFkSBP.user_id == user_id,
                    PaymentsFkSBP.status == "confirmed",
                )
            )
            for tc, payload, amount in fk.all():
                m = _parse_map(payload)
                dur = m.get("duration", "0")
                if dur.startswith("traffic"):
                    continue
                rows.append((tc, dur, _device_label(m), int(amount or 0)))

            st = await session.execute(
                select(
                    PaymentsStars.time_created,
                    PaymentsStars.payload,
                    PaymentsStars.amount,
                ).where(
                    PaymentsStars.bot_id == bot_id,
                    PaymentsStars.user_id == user_id,
                    PaymentsStars.status == "confirmed",
                )
            )
            for tc, payload, amount in st.all():
                m = _parse_map(payload)
                dur = m.get("duration", "0")
                if dur.startswith("traffic"):
                    continue
                rows.append((tc, dur, _device_label(m), int(amount or 0)))

            cr = await session.execute(
                select(
                    PaymentsCryptobot.time_created,
                    PaymentsCryptobot.payload,
                    PaymentsCryptobot.amount,
                ).where(
                    PaymentsCryptobot.bot_id == bot_id,
                    PaymentsCryptobot.user_id == user_id,
                    PaymentsCryptobot.status == "paid",
                )
            )
            for tc, payload, amount in cr.all():
                m = _parse_map(payload)
                dur = m.get("duration", "0")
                if dur.startswith("traffic"):
                    continue
                rows.append((tc, dur, _device_label(m), int(float(amount or 0))))

        rows.sort(key=lambda x: x[0] or datetime.min, reverse=True)
        return rows[:limit]

    async def get_activated_gift(self, bot_id: int, user_id: int) -> Optional[Gifts]:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(Gifts)
                .where(
                    Gifts.bot_id == bot_id,
                    Gifts.recepient_id == user_id,
                    Gifts.flag == True,
                )
                .order_by(Gifts.duration.desc())
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()
