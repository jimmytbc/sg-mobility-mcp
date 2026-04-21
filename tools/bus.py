"""Bus-related MCP tools.

search_bus_stops — text or geo search against the cached stop list.
get_bus_arrivals — live ETAs enriched with the destination terminal
    (resolved via the bus-stops cache), which gives the LLM enough
    directional context to avoid recommending buses heading the wrong
    way.

Author: Jimmy Tong
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from api.errors import (
    LTAAuthFailed,
    LTAEndpointNotFound,
    LTARateLimited,
    LTATimeout,
    UpstreamError,
)
from api.lta import LTAClient
from cache import MobilityCache
from tools._format import (
    ERR_BUS_STOP_NOT_FOUND,
    ERR_LTA_AUTH_FAILED,
    ERR_LTA_ENDPOINT_NOT_FOUND,
    ERR_LTA_RATE_LIMITED,
    ERR_LTA_TIMEOUT,
    MSG_BUS_ARRIVALS_LIVE,
    MSG_ERR_LTA_AUTH_FAILED,
    MSG_ERR_LTA_RATE_LIMITED,
    MSG_ERR_LTA_TIMEOUT,
    MSG_FIRST_CALL_WARM,
    error,
    footer,
    header,
    msg_err_bus_stop_not_found,
    msg_err_lta_endpoint_not_found,
)

LOAD_LABELS = {
    "SEA": "Seats available",
    "SDA": "Standing",
    "LSD": "Limited standing",
}
TYPE_LABELS = {"SD": "Single deck", "DD": "Double deck", "BD": "Bendy"}


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


def _fmt_eta(iso: str) -> str:
    if not iso:
        return "—"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "—"
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    mins = int((t - datetime.now(timezone.utc)).total_seconds() // 60)
    return "Arr" if mins <= 0 else f"{mins} min"


def _fmt_bus(bus: dict) -> str:
    eta = _fmt_eta(bus.get("EstimatedArrival", ""))
    source = "GPS" if bus.get("Monitored") == 1 else "Scheduled"
    load = LOAD_LABELS.get(bus.get("Load", ""), bus.get("Load", ""))
    btype = TYPE_LABELS.get(bus.get("Type", ""), bus.get("Type", ""))
    accessible = " ♿" if bus.get("Feature") == "WAB" else ""
    details = " · ".join(x for x in [load, btype] if x)
    if source == "Scheduled":
        return f"{eta:<6s} ({source})"
    return f"{eta:<6s} ({source})       — {details}{accessible}"


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
    # Default: treat generic upstream failures as timeouts.
    return error(ERR_LTA_TIMEOUT, MSG_ERR_LTA_TIMEOUT)


def register_bus_tools(mcp, lta: LTAClient, cache: MobilityCache) -> None:
    @mcp.tool()
    async def search_bus_stops(
        query: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_m: int = 500,
        limit: int = 10,
    ) -> str:
        """Find Singapore bus stops by name/road or proximity to coordinates.

        Provide `query` for text search OR `latitude`+`longitude` for nearby
        stops; geo wins if both given. Returns stop code, description, road
        name, and (geo mode) distance. Use resolve_location first for place
        names.

        The nearest stop is not always best — check get_bus_arrivals at the
        top 2-3 stops and compare destination terminals before recommending.
        """
        if query is None and (latitude is None or longitude is None):
            return "Provide either `query` or both `latitude` and `longitude`."

        try:
            did_warm = await cache.ensure_stops_warm(lta)
        except UpstreamError as exc:
            return _lta_error(exc)

        warm_footer = footer(MSG_FIRST_CALL_WARM) if did_warm else None

        if latitude is not None and longitude is not None:
            hits: list[tuple[float, dict]] = []
            for s in cache.bus_stops:
                try:
                    slat = float(s["Latitude"])
                    slng = float(s["Longitude"])
                except (KeyError, ValueError, TypeError):
                    continue
                d = _haversine_m(latitude, longitude, slat, slng)
                if d <= radius_m:
                    hits.append((d, s))
            hits.sort(key=lambda x: x[0])
            hits = hits[:limit]
            if not hits:
                summary = (
                    f"0 stops within {radius_m}m of "
                    f"{latitude:.5f}, {longitude:.5f}"
                )
                parts = [header("search_bus_stops", summary)]
                if warm_footer:
                    parts.extend(["", warm_footer])
                return "\n".join(parts)
            summary = (
                f"{len(hits)} stops within {radius_m}m of "
                f"{latitude:.5f}, {longitude:.5f}"
            )
            lines = [header("search_bus_stops", summary), ""]
            for d, s in hits:
                code = str(s.get("BusStopCode", "?"))
                desc = str(s.get("Description", ""))
                road = str(s.get("RoadName", ""))
                lines.append(f"{code:<6s} {desc:<22s} {road:<22s} {int(d)}m")
            if warm_footer:
                lines.extend(["", warm_footer])
            return "\n".join(lines)

        q = query.lower()  # type: ignore[union-attr]
        hits_text = [
            s
            for s in cache.bus_stops
            if q in (s.get("Description", "") or "").lower()
            or q in (s.get("RoadName", "") or "").lower()
        ][:limit]
        if not hits_text:
            summary = f'0 stops matching "{query}"'
            parts = [header("search_bus_stops", summary)]
            if warm_footer:
                parts.extend(["", warm_footer])
            return "\n".join(parts)
        summary = f'{len(hits_text)} stops matching "{query}"'
        lines = [header("search_bus_stops", summary), ""]
        for s in hits_text:
            code = str(s.get("BusStopCode", "?"))
            desc = str(s.get("Description", ""))
            road = str(s.get("RoadName", ""))
            lines.append(f"{code:<6s} {desc:<22s} {road}")
        if warm_footer:
            lines.extend(["", warm_footer])
        return "\n".join(lines)

    @mcp.tool()
    async def get_bus_arrivals(
        bus_stop_code: str,
        service_no: str | None = None,
    ) -> str:
        """Get real-time bus arrival times at a Singapore bus stop.

        Returns next 3 buses per service with ETA, GPS/scheduled flag,
        passenger load, bus type, accessibility, and destination terminal —
        so you can tell direction. Arrivals are AT the stop; do not infer
        intermediate stops.

        Use search_bus_stops first for a stop code, or to compare 2-3 nearby
        stops before picking one.
        """
        # Validate stop code against the cache before calling LTA (FR-E.9).
        try:
            did_warm = await cache.ensure_stops_warm(lta)
        except UpstreamError as exc:
            return _lta_error(exc)

        stop_names = {
            s.get("BusStopCode"): s.get("Description", "")
            for s in cache.bus_stops
        }
        if bus_stop_code not in stop_names:
            return error(
                ERR_BUS_STOP_NOT_FOUND,
                msg_err_bus_stop_not_found(bus_stop_code),
            )

        try:
            data = await lta.get_bus_arrival(bus_stop_code, service_no)
        except UpstreamError as exc:
            return _lta_error(exc)

        services = data.get("Services", []) or []
        stop_name = stop_names.get(bus_stop_code, "")
        stop_label = (
            f"{bus_stop_code} ({stop_name})" if stop_name else str(bus_stop_code)
        )

        if not services:
            summary = f"No buses currently arriving at {stop_label}"
            body_lines = [
                header("get_bus_arrivals", summary),
                "",
                "Possible reasons: outside operating hours, or stop closed.",
                "Verify the stop code via search_bus_stops if unexpected.",
            ]
            if did_warm:
                body_lines.extend(["", footer(MSG_FIRST_CALL_WARM)])
            return "\n".join(body_lines)

        summary = f"{len(services)} services arriving at {stop_label}"
        lines = [header("get_bus_arrivals", summary), ""]
        for svc in services:
            operator = svc.get("Operator", "")
            svc_no = svc.get("ServiceNo", "?")
            dest_code = ""
            for key in ("NextBus", "NextBus2", "NextBus3"):
                bus = svc.get(key) or {}
                if bus.get("DestinationCode"):
                    dest_code = str(bus["DestinationCode"])
                    break

            svc_header = f"Service {svc_no}"
            if operator:
                svc_header += f" ({operator})"
            if dest_code:
                name = stop_names.get(dest_code, "")
                svc_header += (
                    f" → {name} ({dest_code})"
                    if name
                    else f" → terminates at stop {dest_code}"
                )
            lines.append(svc_header)

            slots = [
                ("Next", svc.get("NextBus")),
                ("2nd ", svc.get("NextBus2")),
                ("3rd ", svc.get("NextBus3")),
            ]
            for label, bus in slots:
                if not bus or not bus.get("EstimatedArrival"):
                    continue
                lines.append(f"  {label} : {_fmt_bus(bus)}")
            lines.append("")
        lines.append(footer(MSG_BUS_ARRIVALS_LIVE))
        if did_warm:
            lines.append(footer(MSG_FIRST_CALL_WARM))
        return "\n".join(lines).rstrip()
