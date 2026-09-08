"""VPN panel API client (per-bot credentials)."""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import random
import string
import uuid
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from logging_config import logger
from services.bot_env import BotEnv

WL_SQUAD_ACTIVE = (
    "6b8943e0-dc8b-4323-871c-3b2d017c56c5",
    "7d1024ee-e8b2-4f78-aa9b-51eb23b3bac1",
)


class PanelClient:
    def __init__(self, env: BotEnv):
        self.env = env
        self.target_url = env.panel_url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {env.panel_api_token}",
        }
        self.params = {"vyWdoTBH": "VmsLiQrN"}
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(headers=self.headers, connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def generate_client_id(self, tg_id: int, panel_username: str) -> str:
        secret = self.env.short_uuid_secret
        if not secret:
            raise ValueError("SHORT_UUID_SECRET не задан в .env бота")
        key = secret.encode("utf-8")
        if "_white" in panel_username:
            msg = str(int(tg_id) * 100).encode("utf-8")
        else:
            msg = panel_username.encode("utf-8")
        digest = hmac.new(key, msg, hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return token[:15]

    @staticmethod
    def _panel_user_from_response(users: Optional[dict]) -> Optional[dict]:
        if not users or "response" not in users or not users["response"]:
            return None
        raw = users["response"]
        return raw[0] if isinstance(raw, list) else raw

    @staticmethod
    def _panel_user_id(user: Optional[dict]) -> Optional[int]:
        if not user:
            return None
        pid = user.get("id")
        if pid is None:
            return None
        try:
            return int(pid)
        except (TypeError, ValueError):
            return None

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.target_url}/api/users/by-username/{username}",
                params=self.params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    try:
                        return await resp.json()
                    except Exception:
                        return None
                return None
        except Exception as e:
            logger.error("get_user_by_username %s: %s", username, e)
            return None

    async def activ(self, username: str) -> dict:
        result = {"activ": "🔎 - Не подключён", "time": "-", "expire_at": None}
        try:
            users = await self.get_user_by_username(username)
            user = self._panel_user_from_response(users)
            if not user:
                return result

            expiry_time_str = user.get("expireAt")
            if not expiry_time_str:
                return result

            expiry_dt = datetime.datetime.fromisoformat(expiry_time_str.replace("Z", "+00:00"))
            result["expire_at"] = expiry_dt
            result["time"] = expiry_dt.strftime("%Y-%m-%d %H:%M:%S")

            now = datetime.datetime.now(datetime.timezone.utc)
            if user.get("status") == "ACTIVE" and expiry_dt > now:
                result["activ"] = "✅ - Активен"
            else:
                result["activ"] = "❌ - Не Активен"

            true_sublink = user.get("subscriptionUrl", "")
            if true_sublink and self.env.true_sub_link and self.env.mirror_sub_link:
                result["url"] = true_sublink.replace(self.env.true_sub_link, self.env.mirror_sub_link)
            elif true_sublink:
                result["url"] = true_sublink
            return result
        except Exception as e:
            logger.error("activ %s: %s", username, e)
            result["activ"] = "❌ - Ошибка"
            return result

    async def sublink(self, username: str) -> str:
        try:
            users = await self.get_user_by_username(username)
            user = self._panel_user_from_response(users)
            if not user:
                return ""
            true_sublink = user.get("subscriptionUrl", "")
            if true_sublink and self.env.true_sub_link and self.env.mirror_sub_link:
                return true_sublink.replace(self.env.true_sub_link, self.env.mirror_sub_link)
            return true_sublink or ""
        except Exception as e:
            logger.error("sublink %s: %s", username, e)
            return ""

    async def test_connect(self) -> bool:
        if not self.target_url or not self.env.panel_api_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.target_url}/api/auth/status",
                params=self.params,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                return response.status == 200
        except Exception:
            return False

    def _generate_password(self, length: int = 12) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    async def add_client(
        self,
        username: str,
        user_id: int,
        *,
        hwid_device_limit: int = 5,
    ) -> bool:
        try:
            client_id = self.generate_client_id(user_id, username)
            current_time = datetime.datetime.now(datetime.timezone.utc)
            expire_time = current_time + datetime.timedelta(minutes=1)
            data = {
                "username": username,
                "status": "ACTIVE",
                "shortUuid": client_id,
                "trojanPassword": self._generate_password(),
                "vlessUuid": str(uuid.uuid1()),
                "ssPassword": self._generate_password(),
                "trafficLimitStrategy": "NO_RESET",
                "trafficLimitBytes": 0,
                "expireAt": expire_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "createdAt": current_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "hwidDeviceLimit": hwid_device_limit,
                "telegramId": int(user_id),
                "description": "VPN for friends",
                "activeInternalSquads": [random.choice(WL_SQUAD_ACTIVE)],
            }
            session = await self._get_session()
            async with session.post(
                f"{self.target_url}/api/users",
                json=data,
                params=self.params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                return response.status in (200, 201)
        except Exception as e:
            logger.error("add_client %s: %s", username, e)
            return False

    async def set_expiration_date(
        self,
        username: str,
        target_date: datetime.datetime,
        user_id: int,
        *,
        hwid_device_limit: int = 5,
    ) -> Tuple[bool, Optional[datetime.datetime]]:
        now = datetime.datetime.now(datetime.timezone.utc)
        effective_date = target_date if target_date > now else now + datetime.timedelta(minutes=1)

        user_data = await self.get_user_by_username(username)
        if not user_data or "response" not in user_data:
            if not await self.add_client(username, user_id, hwid_device_limit=hwid_device_limit):
                return False, None
            user_data = await self.get_user_by_username(username)
            if not user_data or "response" not in user_data:
                return False, None

        user = self._panel_user_from_response(user_data)
        panel_user_id = self._panel_user_id(user)
        if not user or panel_user_id is None:
            return False, None

        raw_squads = user.get("activeInternalSquads", [])
        squads = [s["uuid"] if isinstance(s, dict) else s for s in raw_squads]

        data = {
            "id": panel_user_id,
            "expireAt": effective_date.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "status": "ACTIVE",
            "trafficLimitBytes": user.get("trafficLimitBytes", 0),
            "trafficLimitStrategy": user.get("trafficLimitStrategy", "NO_RESET"),
            "activeInternalSquads": squads,
        }

        try:
            session = await self._get_session()
            async with session.patch(
                f"{self.target_url}/api/users",
                json=data,
                params=self.params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 200:
                    return True, effective_date
                return False, None
        except Exception as e:
            logger.error("set_expiration_date %s: %s", username, e)
            return False, None

    async def add_subscription_days(
        self,
        username: str,
        user_id: int,
        days: int,
        *,
        hwid_device_limit: int = 5,
    ) -> Tuple[bool, Optional[datetime.datetime]]:
        """Добавляет дни к подписке в панели (или создаёт клиента при отсутствии)."""
        user_data = await self.get_user_by_username(username)
        if not user_data or "response" not in user_data:
            if not await self.add_client(username, user_id, hwid_device_limit=hwid_device_limit):
                return False, None
            user_data = await self.get_user_by_username(username)
            if not user_data or "response" not in user_data:
                return False, None

        user = self._panel_user_from_response(user_data)
        panel_user_id = self._panel_user_id(user)
        if not user or panel_user_id is None:
            return False, None

        now = datetime.datetime.now(datetime.timezone.utc)
        expire_at_str = user.get("expireAt")
        if expire_at_str:
            current_expire_at = datetime.datetime.fromisoformat(expire_at_str.replace("Z", "+00:00"))
        else:
            current_expire_at = now

        if current_expire_at < now:
            new_expire_at = now + datetime.timedelta(days=days)
            status = "ACTIVE"
        else:
            new_expire_at = current_expire_at + datetime.timedelta(days=days)
            status = user.get("status", "ACTIVE")

        raw_squads = user.get("activeInternalSquads", [])
        squads = [s["uuid"] if isinstance(s, dict) else s for s in raw_squads]

        data = {
            "id": panel_user_id,
            "status": status,
            "expireAt": new_expire_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "trafficLimitBytes": user.get("trafficLimitBytes", 0),
            "trafficLimitStrategy": user.get("trafficLimitStrategy", "NO_RESET"),
            "activeInternalSquads": squads,
        }

        try:
            session = await self._get_session()
            async with session.patch(
                f"{self.target_url}/api/users",
                json=data,
                params=self.params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 200:
                    return True, new_expire_at
                return False, None
        except Exception as e:
            logger.error("add_subscription_days %s: %s", username, e)
            return False, None

    async def get_user_hwid_devices(self, panel_user_id: str) -> Tuple[List[Dict[str, Any]], int]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.target_url}/api/hwid/devices/{panel_user_id}",
                params=self.params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return [], 0
                data = await resp.json()
                devices = data.get("response") or data.get("devices") or []
                if isinstance(devices, dict):
                    devices = devices.get("devices") or []
                return list(devices), len(devices)
        except Exception as e:
            logger.error("get_user_hwid_devices %s: %s", panel_user_id, e)
            return [], 0

    async def update_user_squads(
        self,
        panel_user_id: Optional[int],
        squads: list,
        username: Optional[str] = None,
    ) -> bool:
        username = (username or "").strip() or None
        payload: dict = {"activeInternalSquads": squads}
        if panel_user_id is not None:
            payload["id"] = int(panel_user_id)
        elif username:
            payload["username"] = username
        else:
            return False
        try:
            session = await self._get_session()
            async with session.patch(
                f"{self.target_url}/api/users",
                json=payload,
                params=self.params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                return response.status == 200
        except Exception as e:
            logger.error("update_user_squads: %s", e)
            return False

    async def list_nodes(self) -> List[dict]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.target_url}/api/nodes",
                params=self.params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    return []
                data = await response.json()
                resp = data.get("response") or data
                if isinstance(resp, list):
                    return resp
                if isinstance(resp, dict):
                    return resp.get("nodes") or []
                return []
        except Exception as e:
            logger.error("list_nodes: %s", e)
            return []

    async def get_node_uuid_by_name(self, node_name: str) -> Optional[str]:
        for node in await self.list_nodes():
            name = node.get("name") or node.get("nodeName") or ""
            if name != node_name:
                continue
            node_uuid = node.get("uuid") or node.get("nodeUuid")
            if node_uuid:
                return str(node_uuid)
        return None

    async def get_node_users_bandwidth_legacy(
        self,
        node_uuid: str,
        start: str,
        end: str,
        *,
        top_users_limit: int = 5000,
    ) -> Optional[List[dict]]:
        params = {
            **self.params,
            "start": start,
            "end": end,
            "topUsersLimit": str(top_users_limit),
        }
        url = f"{self.target_url}/api/bandwidth-stats/nodes/{node_uuid}/users"
        try:
            session = await self._get_session()
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=20)
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                resp = data.get("response") or data
                if isinstance(resp, list):
                    return resp
                if isinstance(resp, dict):
                    top_users = resp.get("topUsers")
                    if isinstance(top_users, list):
                        return top_users
                return None
        except Exception as e:
            logger.error("get_node_users_bandwidth_legacy: %s", e)
            return None


_panel_cache: dict[int, PanelClient] = {}


async def get_panel_client(bot_id: int) -> Optional[PanelClient]:
    from services.bot_env import load_bot_env

    if bot_id in _panel_cache:
        return _panel_cache[bot_id]
    env = load_bot_env(bot_id)
    if not env or not env.panel_url or not env.panel_api_token:
        return None
    client = PanelClient(env)
    _panel_cache[bot_id] = client
    return client


async def close_all_panels() -> None:
    for client in _panel_cache.values():
        await client.close()
    _panel_cache.clear()
