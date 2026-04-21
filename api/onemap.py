"""OneMap client with transparent JWT auto-refresh.

The access token is a JWT — rather than trust the API's advertised
TTL, we decode the token's own `exp` claim and refresh five minutes
ahead of it. An asyncio.Lock guards the refresh so concurrent tool
calls can't stampede the auth endpoint.

Upstream failures raise typed exceptions from `api.errors` so the tool
layer can map each to the right `ERR_*` string without stringly-typed
inspection.

Author: Jimmy Tong
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx

from api.errors import OneMapAuthFailed, OneMapSchemaDrift, OneMapTimeout

BASE_URL = "https://www.onemap.gov.sg/api"
TOKEN_URL = f"{BASE_URL}/auth/post/getToken"
SEARCH_URL = f"{BASE_URL}/common/elastic/search"
REVGEOCODE_URL = f"{BASE_URL}/public/revgeocode"
TOKEN_REFRESH_BUFFER_S = 300

EXPECTED_REVGEOCODE_KEYS = (
    "BUILDINGNAME",
    "ROAD",
    "POSTALCODE",
    "LATITUDE",
    "LONGITUDE",
)


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
        self._client = httpx.AsyncClient(timeout=10.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_token(self) -> str:
        async with self._token_lock:
            if (
                self._token
                and time.time() < self._token_expiry - TOKEN_REFRESH_BUFFER_S
            ):
                return self._token
            try:
                res = await self._client.post(
                    TOKEN_URL,
                    json={"email": self._email, "password": self._password},
                )
            except httpx.TimeoutException as exc:
                raise OneMapTimeout("OneMap auth timed out") from exc
            except httpx.RequestError as exc:
                raise OneMapTimeout(f"OneMap auth request failed: {exc}") from exc
            if res.status_code in (401, 403):
                raise OneMapAuthFailed(
                    f"OneMap auth rejected ({res.status_code})"
                )
            if res.status_code != 200:
                raise OneMapAuthFailed(
                    f"OneMap auth failed ({res.status_code})"
                )
            token = res.json().get("access_token")
            if not token:
                raise OneMapAuthFailed("OneMap auth: no access_token in response")
            self._token = token
            self._token_expiry = _parse_token_expiry(token)
            return token

    async def search(self, query: str) -> list[dict]:
        token = await self._get_token()
        try:
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
        except httpx.TimeoutException as exc:
            raise OneMapTimeout("OneMap search timed out") from exc
        except httpx.RequestError as exc:
            raise OneMapTimeout(f"OneMap search request failed: {exc}") from exc
        if res.status_code in (401, 403):
            raise OneMapAuthFailed(
                f"OneMap search auth rejected ({res.status_code})"
            )
        if res.status_code != 200:
            raise OneMapTimeout(
                f"OneMap search returned {res.status_code}"
            )
        return (res.json().get("results") or [])[:3]

    async def reverse_geocode(
        self, latitude: float, longitude: float, buffer_m: int = 50
    ) -> list[dict]:
        """Return up to 3 addresses near the given coordinates.

        Raises OneMapSchemaDrift if the response shape diverges from
        the GeocodeInfo array documented in specs/07-stack.md.
        """
        token = await self._get_token()
        try:
            res = await self._client.get(
                REVGEOCODE_URL,
                params={
                    "location": f"{latitude},{longitude}",
                    "buffer": buffer_m,
                    "addressType": "All",
                    "otherFeatures": "N",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise OneMapTimeout("OneMap reverse geocode timed out") from exc
        except httpx.RequestError as exc:
            raise OneMapTimeout(
                f"OneMap reverse geocode request failed: {exc}"
            ) from exc
        if res.status_code in (401, 403):
            raise OneMapAuthFailed(
                f"OneMap reverse geocode auth rejected ({res.status_code})"
            )
        if res.status_code != 200:
            raise OneMapTimeout(
                f"OneMap reverse geocode returned {res.status_code}"
            )
        try:
            body = res.json()
        except ValueError as exc:
            raise OneMapSchemaDrift("OneMap reverse geocode: non-JSON body") from exc
        if not isinstance(body, dict):
            raise OneMapSchemaDrift(
                f"OneMap reverse geocode: top-level {type(body).__name__}, expected object"
            )
        geocode_info = body.get("GeocodeInfo")
        if geocode_info is None:
            return []
        if not isinstance(geocode_info, list):
            raise OneMapSchemaDrift(
                f"OneMap reverse geocode: GeocodeInfo is {type(geocode_info).__name__}"
            )
        if not geocode_info:
            return []
        first = geocode_info[0]
        if not isinstance(first, dict) or not all(
            k in first for k in EXPECTED_REVGEOCODE_KEYS
        ):
            raise OneMapSchemaDrift(
                "OneMap reverse geocode: GeocodeInfo entry missing expected keys"
            )
        return geocode_info[:3]
