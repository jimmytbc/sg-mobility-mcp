"""LTA DataMall client.

Single long-lived httpx.AsyncClient, AccountKey injected at
construction so every request is pre-authenticated. Pagination is
transparent: LTA pages most collections at 500 rows, so we fetch in a
loop with $skip until a short page comes back.

Author: Jimmy Tong
"""

import httpx

BASE_URL = "https://datamall2.mytransport.sg/ltaodataservice"
PAGE_SIZE = 500


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
        res = await self._client.get(path, params=params or {})
        if res.status_code != 200:
            raise RuntimeError(
                f"LTA {path} returned {res.status_code}: {res.text[:200]}"
            )
        return res.json()

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
