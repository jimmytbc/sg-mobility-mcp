"""OneMap client with transparent JWT auto-refresh.

The access token is a JWT — rather than trust the API's advertised
TTL, we decode the token's own `exp` claim and refresh five minutes
ahead of it. An asyncio.Lock guards the refresh so concurrent tool
calls can't stampede the auth endpoint.

Author: Jimmy Tong
"""

import asyncio
import base64
import json
import time

import httpx

BASE_URL = "https://www.onemap.gov.sg/api"
TOKEN_URL = f"{BASE_URL}/auth/post/getToken"
SEARCH_URL = f"{BASE_URL}/common/elastic/search"
TOKEN_REFRESH_BUFFER_S = 300


def _parse_token_expiry(token: str) -> float:
    payload = token.split(".")[1]
    padding = (-len(payload)) % 4
    decoded = base64.urlsafe_b64decode(payload + ("=" * padding))
    return float(json.loads(decoded)["exp"])


class OneMapClient:
    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=20.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_token(self) -> str:
        async with self._token_lock:
            if (
                self._token
                and time.time() < self._token_expiry - TOKEN_REFRESH_BUFFER_S
            ):
                return self._token
            res = await self._client.post(
                TOKEN_URL,
                json={"email": self._email, "password": self._password},
            )
            if res.status_code != 200:
                raise RuntimeError(
                    f"OneMap auth failed ({res.status_code}): "
                    f"{res.text[:200]} — check ONEMAP_EMAIL / ONEMAP_PASSWORD."
                )
            token = res.json().get("access_token")
            if not token:
                raise RuntimeError("OneMap auth: no access_token in response.")
            self._token = token
            self._token_expiry = _parse_token_expiry(token)
            return token

    async def search(self, query: str) -> list[dict]:
        token = await self._get_token()
        res = await self._client.get(
            SEARCH_URL,
            params={
                "searchVal": query,
                "returnGeom": "Y",
                "getAddrDetails": "Y",
                "pageNum": 1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        if res.status_code != 200:
            raise RuntimeError(
                f"OneMap search failed ({res.status_code}): {res.text[:200]}"
            )
        return (res.json().get("results") or [])[:3]
