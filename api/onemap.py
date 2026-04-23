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
import sys
import time
from datetime import datetime

import httpx

from api.errors import (
    OneMapAuthFailed,
    OneMapRoutingRateLimited,
    OneMapRoutingServiceDown,
    OneMapSchemaDrift,
    OneMapTimeout,
)
from api.lta import RATE_LIMIT_BACKOFFS_S

BASE_URL = "https://www.onemap.gov.sg/api"
TOKEN_URL = f"{BASE_URL}/auth/post/getToken"
SEARCH_URL = f"{BASE_URL}/common/elastic/search"
REVGEOCODE_URL = f"{BASE_URL}/public/revgeocode"
ROUTE_URL = f"{BASE_URL}/public/routingsvc/route"
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

    async def route_pt(
        self,
        from_lat: float,
        from_lng: float,
        to_lat: float,
        to_lng: float,
        max_walk_distance_m: int,
        num_itineraries: int = 3,
        now: datetime | None = None,
    ) -> dict:
        """Call OneMap PT routing endpoint (FR-7.1).

        5xx raises OneMapRoutingServiceDown (no retry per FR-E.16).
        429 is retried with the LTA backoff schedule per FR-7.5; when
        exhausted, raises OneMapRoutingRateLimited per FR-E.17.
        """
        token = await self._get_token()
        call_time = now or datetime.now()
        params = {
            "start": f"{from_lat},{from_lng}",
            "end": f"{to_lat},{to_lng}",
            "routeType": "pt",
            "mode": "TRANSIT",
            "date": call_time.strftime("%m-%d-%Y"),
            "time": call_time.strftime("%H:%M:%S"),
            "maxWalkDistance": max_walk_distance_m,
            "numItineraries": num_itineraries,
        }
        headers = {"Authorization": f"Bearer {token}"}
        attempt = 0
        while True:
            try:
                res = await self._client.get(
                    ROUTE_URL, params=params, headers=headers
                )
            except httpx.TimeoutException as exc:
                raise OneMapTimeout("OneMap PT routing timed out") from exc
            except httpx.RequestError as exc:
                raise OneMapTimeout(
                    f"OneMap PT routing request failed: {exc}"
                ) from exc

            if res.status_code == 200:
                try:
                    return res.json()
                except ValueError as exc:
                    raise OneMapSchemaDrift(
                        "OneMap PT routing: non-JSON body"
                    ) from exc
            # OneMap returns 404 with `{"error": "No route found ..."}`
            # for genuinely unrouteable endpoints (e.g., airport-side
            # service roads). The spec (FR-E.15) anticipated a 200 with
            # zero itineraries; live behaviour is 404. Normalise to the
            # empty-itinerary shape so the caller's zero-itinerary
            # branch triggers the FR-7.4 fallback + ERR_NO_PT_ROUTE.
            if res.status_code == 404:
                return {"plan": {"itineraries": []}}
            if res.status_code == 429:
                if attempt < len(RATE_LIMIT_BACKOFFS_S):
                    delay = RATE_LIMIT_BACKOFFS_S[attempt]
                    print(
                        f"[onemap] 429 on PT routing; backing off {delay}s "
                        f"(attempt {attempt + 1}/{len(RATE_LIMIT_BACKOFFS_S)})",
                        file=sys.stderr,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise OneMapRoutingRateLimited(
                    "OneMap PT routing rate-limited after retries"
                )
            if res.status_code in (401, 403):
                raise OneMapAuthFailed(
                    f"OneMap PT routing auth rejected ({res.status_code})"
                )
            if 500 <= res.status_code < 600:
                raise OneMapRoutingServiceDown(
                    f"OneMap PT routing returned {res.status_code}"
                )
            raise OneMapTimeout(
                f"OneMap PT routing returned {res.status_code}"
            )
