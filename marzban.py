import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class MarzbanClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None
        self._http = httpx.AsyncClient(timeout=30)

    async def authenticate(self) -> None:
        resp = await self._http.post(
            f"{self._base_url}/api/admin/token",
            data={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        logger.info("Marzban: authenticated successfully")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self._token:
            await self.authenticate()

        resp = await self._http.request(
            method, f"{self._base_url}{path}", headers=self._headers(), **kwargs
        )

        if resp.status_code == 401:
            logger.info("Marzban: token expired, re-authenticating")
            self._token = None
            await self.authenticate()
            resp = await self._http.request(
                method, f"{self._base_url}{path}", headers=self._headers(), **kwargs
            )

        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def get_user(self, username: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/user/{username}")

    async def create_user(
        self,
        username: str,
        data_limit: int = 0,
        expire: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "username": username,
            "proxies": {"vless": {}},
            "inbounds": {"vless": ["VLESS TCP REALITY"]},
            "data_limit": data_limit,
            "expire": expire,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
        }
        return await self._request("POST", "/api/user", json=payload)

    def vless_link(self, user: dict[str, Any]) -> str:
        for link in user.get("links", []):
            if link.startswith("vless://"):
                return link
        return "N/A"

    async def delete_user(self, username: str) -> None:
        await self._request("DELETE", f"/api/user/{username}")

    async def reset_traffic(self, username: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/user/{username}/reset")

    async def list_users(self, offset: int = 0, limit: int = 10) -> dict[str, Any]:
        return await self._request(
            "GET", "/api/users", params={"offset": offset, "limit": limit}
        )

    async def close(self) -> None:
        await self._http.aclose()
