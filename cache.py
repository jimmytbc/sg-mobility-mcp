"""In-memory caches for LTA's heavier static datasets.

Two independent caches — bus stops (~5k rows) and bus routes (~27k
rows) — each lazy-warmed on first use and held for 24 hours. An
asyncio.Lock around each warm ensures concurrent tool calls don't
double-fetch the same dataset.

Author: Jimmy Tong
"""

import asyncio
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.lta import LTAClient


class MobilityCache:
    TTL_SECONDS = 86_400  # 24h

    def __init__(self) -> None:
        # Bus stops
        self.bus_stops: list[dict] = []
        self._stops_warmed_at: float = 0.0
        self._stops_lock = asyncio.Lock()

        # Bus routes — indexed
        # routes_by_service: (ServiceNo, Direction) -> [(stop_code, stop_seq, distance_km)] sorted by seq
        self.routes_by_service: dict[
            tuple[str, int], list[tuple[str, int, float]]
        ] = {}
        # routes_by_stop: stop_code -> [(ServiceNo, Direction, stop_seq)]
        self.routes_by_stop: dict[str, list[tuple[str, int, int]]] = {}
        self._routes_warmed_at: float = 0.0
        self._routes_lock = asyncio.Lock()

    async def ensure_stops_warm(self, lta: "LTAClient") -> None:
        async with self._stops_lock:
            if (
                self.bus_stops
                and time.time() - self._stops_warmed_at < self.TTL_SECONDS
            ):
                return
            stops = await lta.get_bus_stops()
            self.bus_stops = stops
            self._stops_warmed_at = time.time()
            print(f"[cache] warmed {len(stops)} bus stops", file=sys.stderr)

    async def ensure_routes_warm(self, lta: "LTAClient") -> None:
        async with self._routes_lock:
            if (
                self.routes_by_service
                and time.time() - self._routes_warmed_at < self.TTL_SECONDS
            ):
                return
            rows = await lta.get_bus_routes()
            by_service: dict[tuple[str, int], list[tuple[str, int, float]]] = {}
            by_stop: dict[str, list[tuple[str, int, int]]] = {}
            for r in rows:
                svc = r.get("ServiceNo")
                stop = r.get("BusStopCode")
                if not svc or not stop:
                    continue
                try:
                    direction = int(r.get("Direction", 0) or 0)
                    seq = int(r.get("StopSequence", 0) or 0)
                    dist = float(r.get("Distance", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
                by_service.setdefault((svc, direction), []).append(
                    (stop, seq, dist)
                )
                by_stop.setdefault(stop, []).append((svc, direction, seq))
            for key in by_service:
                by_service[key].sort(key=lambda x: x[1])
            self.routes_by_service = by_service
            self.routes_by_stop = by_stop
            self._routes_warmed_at = time.time()
            print(
                f"[cache] warmed {len(rows)} bus route rows, "
                f"{len(by_service)} service-directions",
                file=sys.stderr,
            )
