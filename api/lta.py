"""LTA DataMall client.

Single long-lived httpx.AsyncClient, AccountKey injected at
construction so every request is pre-authenticated. Pagination is
transparent: LTA pages most collections at 500 rows, so we fetch in a
loop with $skip until a short page comes back.

429 responses are retried with 2s / 5s / 15s delays (per RISK-1
fallback in specs/11-risks.md). Upstream failures raise typed
exceptions from `api.errors` so the tool layer can map each to the
right `ERR_*` string without stringly-typed inspection.

Author: Jimmy Tong
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from api.errors import (
    LTAAuthFailed,
    LTAEndpointNotFound,
    LTARateLimited,
    LTATimeout,
    UpstreamError,
)

BASE_URL = "https://datamall2.mytransport.sg/ltaodataservice"
PAGE_SIZE = 500
RATE_LIMIT_BACKOFFS_S: tuple[float, ...] = (2.0, 5.0, 15.0)


class LTAClient:
    def __init__(self, account_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"AccountKey": account_key, "Accept": "application/json"},
            timeout=20.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        attempt = 0
        while True:
            try:
                res = await self._client.get(path, params=params or {})
            except httpx.TimeoutException as exc:
                raise LTATimeout(f"LTA {path} timed out") from exc
            except httpx.RequestError as exc:
                raise LTATimeout(f"LTA {path} request failed: {exc}") from exc

            if res.status_code == 200:
                return res.json()
            if res.status_code == 429:
                if attempt < len(RATE_LIMIT_BACKOFFS_S):
                    delay = RATE_LIMIT_BACKOFFS_S[attempt]
                    print(
                        f"[lta] 429 on {path}; backing off {delay}s "
                        f"(attempt {attempt + 1}/{len(RATE_LIMIT_BACKOFFS_S)})",
                        file=sys.stderr,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise LTARateLimited(f"LTA {path} rate-limited after retries")
            if res.status_code in (401, 403):
                raise LTAAuthFailed(f"LTA {path} auth failed ({res.status_code})")
            if res.status_code == 404:
                raise LTAEndpointNotFound(path)
            raise UpstreamError(
                f"LTA {path} returned {res.status_code}: {res.text[:200]}"
            )

    async def _get_paginated(self, path: str) -> list[dict]:
        results: list[dict] = []
        skip = 0
        while True:
            data = await self._get(path, params={"$skip": skip})
            batch = data.get("value", []) or []
            results.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
        return results

    async def get_bus_stops(self) -> list[dict]:
        return await self._get_paginated("/BusStops")

    async def get_bus_arrival(
        self, stop_code: str, service_no: str | None = None
    ) -> dict:
        params: dict[str, str] = {"BusStopCode": stop_code}
        if service_no:
            params["ServiceNo"] = service_no
        return await self._get("/v3/BusArrival", params=params)

    async def get_train_alerts(self) -> dict:
        return await self._get("/TrainServiceAlerts")

    async def get_carpark_availability(self) -> list[dict]:
        return await self._get_paginated("/CarParkAvailabilityv2")

    async def get_bus_routes(self) -> list[dict]:
        return await self._get_paginated("/BusRoutes")
