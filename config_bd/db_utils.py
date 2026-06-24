from typing import Any, Dict, List

from sqlalchemy import select

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
