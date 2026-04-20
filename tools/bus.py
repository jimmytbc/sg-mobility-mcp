"""Bus-related MCP tools.

search_bus_stops — text or geo search against the cached stop list.
get_bus_arrivals — live ETAs enriched with the destination terminal
    (resolved via the bus-stops cache), which gives the LLM enough
    directional context to avoid recommending buses heading the wrong
    way.

Author: Jimmy Tong
"""

import math
from datetime import datetime, timezone

from api.lta import LTAClient
from cache import MobilityCache

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


def register_bus_tools(mcp, lta: LTAClient, cache: MobilityCache) -> None:
    @mcp.tool()
    async def search_bus_stops(
        query: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_m: int = 500,
        limit: int = 10,
    ) -> str:
        """Find Singapore bus stops by name or road, or by proximity to
        coordinates.

        Provide query for text search OR latitude+longitude for nearby stops.
        If both are provided, geo search takes precedence. Returns stop code,
        description, road name, and distance in geo mode. Use
        resolve_location first if you only have a place name.

        Trip-planning guidance: when using this for route planning, do NOT
        assume the nearest stop is the best. A stop 100–300m further may
        serve a bus that goes directly to the destination, while the
        closest stop only serves indirect / loop routes. Evaluate
        get_bus_arrivals at the top 2–3 returned stops and compare each
        service's destination terminal before recommending a route.
        """
        if query is None and (latitude is None or longitude is None):
            return "Provide either `query` or both `latitude` and `longitude`."

        try:
            await cache.ensure_stops_warm(lta)
        except RuntimeError as e:
            return f"Could not load bus stop data: {e}"

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
                return (
                    f"No bus stops within {radius_m}m of "
                    f"({latitude:.4f}, {longitude:.4f})."
                )
            lines = [
                f"Bus stops within {radius_m}m of "
                f"({latitude:.4f}, {longitude:.4f}):",
                "",
            ]
            for d, s in hits:
                code = str(s.get("BusStopCode", "?"))
                desc = str(s.get("Description", ""))
                road = str(s.get("RoadName", ""))
                lines.append(f"{code:<6s} {desc:<22s} {road:<22s} {int(d)}m")
            return "\n".join(lines)

        q = query.lower()  # type: ignore[union-attr]
        hits_text = [
            s
            for s in cache.bus_stops
            if q in (s.get("Description", "") or "").lower()
            or q in (s.get("RoadName", "") or "").lower()
        ][:limit]
        if not hits_text:
            return f"No bus stops matching '{query}'."
        lines = [f"Bus stops matching '{query}':", ""]
        for s in hits_text:
            code = str(s.get("BusStopCode", "?"))
            desc = str(s.get("Description", ""))
            road = str(s.get("RoadName", ""))
            lines.append(f"{code:<6s} {desc:<22s} {road}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_bus_arrivals(
        bus_stop_code: str,
        service_no: str | None = None,
    ) -> str:
        """Get real-time bus arrival times at a Singapore bus stop.

        Returns next 3 buses per service with ETA, GPS or scheduled
        indicator, passenger load, bus type, wheelchair accessibility,
        and — importantly — the destination terminal each service heads
        to, so you can tell which direction the bus is going.

        This tool returns only arrivals AT the given stop, not the full
        route. Do not infer intermediate stops a bus passes through;
        use the destination terminal shown to judge direction.

        Trip-planning guidance: when planning a trip from a location,
        call search_bus_stops to get 2–3 nearby stops, then call this
        tool on EACH of those stops and compare services. A slightly
        further walk to a stop with a direct bus is usually better than
        the nearest stop on a loop / indirect route. Do not settle on
        the nearest stop without checking the alternatives.

        Use search_bus_stops to find the stop code if unknown.
        """
        try:
            data = await lta.get_bus_arrival(bus_stop_code, service_no)
        except RuntimeError as e:
            return f"Could not fetch bus arrivals for stop {bus_stop_code}: {e}"

        services = data.get("Services", []) or []
        if not services:
            return f"No buses currently arriving at stop {bus_stop_code}."

        try:
            await cache.ensure_stops_warm(lta)
        except RuntimeError:
            pass  # fall back to showing codes only
        stop_names = {
            s.get("BusStopCode"): s.get("Description", "")
            for s in cache.bus_stops
        }

        lines = [f"Bus arrivals — Stop {bus_stop_code}", ""]
        for svc in services:
            operator = svc.get("Operator", "")
            svc_no = svc.get("ServiceNo", "?")
            dest_code = ""
            for key in ("NextBus", "NextBus2", "NextBus3"):
                bus = svc.get(key) or {}
                if bus.get("DestinationCode"):
                    dest_code = str(bus["DestinationCode"])
                    break

            header = f"Service {svc_no}"
            if operator:
                header += f" ({operator})"
            if dest_code:
                name = stop_names.get(dest_code, "")
                header += (
                    f" → {name} ({dest_code})"
                    if name
                    else f" → terminates at stop {dest_code}"
                )
            lines.append(header)

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
        return "\n".join(lines).rstrip()
