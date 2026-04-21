"""Real-time carpark availability across HDB, URA, and LTA carparks.

LTA returns coordinates as a single 'lat lng' string, which is parsed
defensively — a handful of records in the feed have malformed
Location fields and we skip those silently rather than fail the whole
query.

Author: Jimmy Tong
"""

from __future__ import annotations

import math

from api.errors import (
    LTAAuthFailed,
    LTAEndpointNotFound,
    LTARateLimited,
    LTATimeout,
    UpstreamError,
)
from api.lta import LTAClient
from tools._format import (
    ERR_LTA_AUTH_FAILED,
    ERR_LTA_ENDPOINT_NOT_FOUND,
    ERR_LTA_RATE_LIMITED,
    ERR_LTA_TIMEOUT,
    MSG_CARPARK_FEED,
    MSG_ERR_LTA_AUTH_FAILED,
    MSG_ERR_LTA_RATE_LIMITED,
    MSG_ERR_LTA_TIMEOUT,
    error,
    footer,
    header,
    msg_err_lta_endpoint_not_found,
)

LOT_LABELS = {"C": "car", "Y": "motorcycle", "H": "heavy vehicle"}
DISPLAY_CAP = 20


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_location(loc: str) -> tuple[float, float] | None:
    if not isinstance(loc, str):
        return None
    parts = loc.split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _fmt_row(r: dict, distance: int | None = None) -> str:
    cpid = str(r.get("CarParkID", "?"))
    dev = str(r.get("Development", "") or "")
    lots = r.get("AvailableLots", 0)
    agency = str(r.get("Agency", "") or "")
    line = f"{cpid:<6s} {dev:<35s} {int(lots):>5} lots  [{agency}]"
    if distance is not None:
        line += f"  {distance}m"
    return line


def _lta_error(exc: UpstreamError) -> str:
    if isinstance(exc, LTAAuthFailed):
        return error(ERR_LTA_AUTH_FAILED, MSG_ERR_LTA_AUTH_FAILED)
    if isinstance(exc, LTARateLimited):
        return error(ERR_LTA_RATE_LIMITED, MSG_ERR_LTA_RATE_LIMITED)
    if isinstance(exc, LTAEndpointNotFound):
        return error(
            ERR_LTA_ENDPOINT_NOT_FOUND,
            msg_err_lta_endpoint_not_found(exc.path),
        )
    return error(ERR_LTA_TIMEOUT, MSG_ERR_LTA_TIMEOUT)


def register_carpark_tools(mcp, lta: LTAClient) -> None:
    @mcp.tool()
    async def get_carpark_availability(
        area: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_m: int = 500,
        lot_type: str = "C",
        min_lots: int = 0,
    ) -> str:
        """Real-time carpark lot availability (HDB, URA, LTA) in Singapore.

        Provide `area` for text search OR `latitude`+`longitude` for nearby
        carparks; geo search takes precedence. lot_type: C=car (default),
        Y=motorcycle, H=heavy vehicle. Geo mode sorts by distance; text
        mode sorts by available lots descending.
        """
        try:
            rows = await lta.get_carpark_availability()
        except UpstreamError as exc:
            return _lta_error(exc)

        rows = [
            r
            for r in rows
            if r.get("LotType") == lot_type
            and int(r.get("AvailableLots", 0) or 0) >= min_lots
        ]
        label = LOT_LABELS.get(lot_type, lot_type)

        if latitude is not None and longitude is not None:
            hits: list[tuple[float, dict]] = []
            for r in rows:
                coords = _parse_location(r.get("Location", ""))
                if coords is None:
                    continue
                d = _haversine_m(latitude, longitude, coords[0], coords[1])
                if d <= radius_m:
                    hits.append((d, r))
            hits.sort(key=lambda x: x[0])
            hits = hits[:DISPLAY_CAP]
            summary_tail = f"(lot type {lot_type}"
            if min_lots > 0:
                summary_tail += f", min {min_lots} lots"
            summary_tail += ")"
            if not hits:
                summary = (
                    f"0 carparks within {radius_m}m of "
                    f"{latitude:.5f}, {longitude:.5f} {summary_tail}"
                )
                return header("get_carpark_availability", summary)
            summary = (
                f"{len(hits)} carparks within {radius_m}m of "
                f"{latitude:.5f}, {longitude:.5f} {summary_tail}"
            )
            out = [header("get_carpark_availability", summary), ""]
            for d, r in hits:
                out.append(_fmt_row(r, distance=int(d)))
            out.append("")
            out.append(footer(MSG_CARPARK_FEED))
            return "\n".join(out)

        if area:
            q = area.lower()
            matched = [
                r
                for r in rows
                if q in (r.get("Area", "") or "").lower()
                or q in (r.get("Development", "") or "").lower()
            ]
        else:
            matched = list(rows)
        matched.sort(
            key=lambda r: int(r.get("AvailableLots", 0) or 0), reverse=True
        )
        matched = matched[:DISPLAY_CAP]
        tail = f"(lot type {lot_type})"
        if not matched:
            summary = (
                f'0 carparks matching "{area}" {tail}'
                if area
                else f"0 carparks available {tail}"
            )
            return header("get_carpark_availability", summary)
        summary = (
            f'{len(matched)} carparks matching "{area}" {tail}'
            if area
            else f"{len(matched)} carparks available {tail}"
        )
        out = [header("get_carpark_availability", summary), ""]
        for r in matched:
            out.append(_fmt_row(r))
        out.append("")
        out.append(footer(MSG_CARPARK_FEED))
        return "\n".join(out)
