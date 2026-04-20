"""Direct-bus trip planner — the flagship routing tool.

Given origin and destination coordinates, this matches (service,
direction) pairs where both points lie on the route in the correct
order (origin stop sequence < destination stop sequence), fetches
live ETAs at each candidate origin stop, then ranks the matches by
total walk + wait + ride + walk.

Time estimates are rough — 80 m/min walking, ~1.8 min per in-vehicle
stop — and that is flagged in the tool's output so downstream callers
don't mistake them for scheduled times.

Author: Jimmy Tong
"""

import math
from datetime import datetime, timezone

from api.lta import LTAClient
from cache import MobilityCache

WALK_M_PER_MIN = 80  # average walking speed ~4.8 km/h
RIDE_MIN_PER_STOP = 1.8  # rough in-vehicle estimate


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


def _parse_eta_min(iso: str) -> int | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int((t - datetime.now(timezone.utc)).total_seconds() // 60))


def register_routing_tools(
    mcp, lta: LTAClient, cache: MobilityCache
) -> None:
    @mcp.tool()
    async def find_direct_bus(
        from_latitude: float,
        from_longitude: float,
        to_latitude: float,
        to_longitude: float,
        max_walk_m: int = 600,
        limit: int = 3,
    ) -> str:
        """Find the best direct bus options between two Singapore coordinates.

        Evaluates candidate origin and destination stops within max_walk_m,
        matches services that serve both (with the correct direction),
        fetches live ETAs, and ranks options by estimated total time
        (walk + wait + in-vehicle + walk). Returns the top `limit`
        single-service journeys — no transfers.

        Use resolve_location first if you only have place names. Use this
        BEFORE manually chaining search_bus_stops + get_bus_arrivals —
        this tool already does the comparison server-side.

        If no direct bus exists within the walk radius, the tool says so;
        fall back to MRT / multi-leg planning.
        """
        try:
            await cache.ensure_stops_warm(lta)
            await cache.ensure_routes_warm(lta)
        except RuntimeError as e:
            return f"Could not load route data: {e}"

        # Short-circuit trivial distance
        direct_m = _haversine_m(
            from_latitude, from_longitude, to_latitude, to_longitude
        )
        if direct_m < 300:
            return (
                f"The origin and destination are only {int(direct_m)}m apart "
                "— walking is likely faster than taking a bus."
            )

        # Candidate origin stops
        origins: list[tuple[str, str, float]] = []
        for s in cache.bus_stops:
            try:
                slat = float(s["Latitude"])
                slng = float(s["Longitude"])
            except (KeyError, ValueError, TypeError):
                continue
            d = _haversine_m(from_latitude, from_longitude, slat, slng)
            if d <= max_walk_m:
                origins.append(
                    (str(s.get("BusStopCode", "")), str(s.get("Description", "")), d)
                )
        # Candidate destination stops
        dests: list[tuple[str, str, float]] = []
        for s in cache.bus_stops:
            try:
                slat = float(s["Latitude"])
                slng = float(s["Longitude"])
            except (KeyError, ValueError, TypeError):
                continue
            d = _haversine_m(to_latitude, to_longitude, slat, slng)
            if d <= max_walk_m:
                dests.append(
                    (str(s.get("BusStopCode", "")), str(s.get("Description", "")), d)
                )

        if not origins or not dests:
            return (
                f"No bus stops within {max_walk_m}m of "
                f"{'origin' if not origins else 'destination'}. "
                "Increase max_walk_m or choose different points."
            )

        # Match services serving both origin and destination, correct direction
        # Key: service -> best candidate (lowest walk sum as a prefilter)
        candidates: list[dict] = []
        dest_codes = {d[0] for d in dests}
        dest_info = {d[0]: d for d in dests}
        for o_code, o_desc, o_walk in origins:
            for o_svc, o_dir, o_seq in cache.routes_by_stop.get(o_code, []):
                route = cache.routes_by_service.get((o_svc, o_dir), [])
                # Scan stops that come AFTER the origin in this route
                for stop_code, stop_seq, _dist in route:
                    if stop_seq <= o_seq:
                        continue
                    if stop_code in dest_codes:
                        d_code, d_desc, d_walk = dest_info[stop_code]
                        candidates.append(
                            {
                                "service": o_svc,
                                "direction": o_dir,
                                "origin_code": o_code,
                                "origin_desc": o_desc,
                                "origin_walk": o_walk,
                                "origin_seq": o_seq,
                                "dest_code": d_code,
                                "dest_desc": d_desc,
                                "dest_walk": d_walk,
                                "dest_seq": stop_seq,
                                "ride_stops": stop_seq - o_seq,
                            }
                        )

        if not candidates:
            return (
                f"No direct bus found from ({from_latitude:.4f}, "
                f"{from_longitude:.4f}) to ({to_latitude:.4f}, "
                f"{to_longitude:.4f}) within {max_walk_m}m walk. "
                "Consider MRT or a multi-leg route."
            )

        # Fetch live ETAs — one API call per unique origin stop
        eta_by_stop_service: dict[tuple[str, str], int | None] = {}
        for o_code in {c["origin_code"] for c in candidates}:
            try:
                data = await lta.get_bus_arrival(o_code)
            except RuntimeError:
                continue
            for svc in data.get("Services", []) or []:
                svc_no = svc.get("ServiceNo")
                bus = svc.get("NextBus") or {}
                eta = _parse_eta_min(bus.get("EstimatedArrival", ""))
                if svc_no:
                    eta_by_stop_service[(o_code, svc_no)] = eta

        # Score each candidate
        for c in candidates:
            walk_to = c["origin_walk"] / WALK_M_PER_MIN
            walk_from = c["dest_walk"] / WALK_M_PER_MIN
            ride = c["ride_stops"] * RIDE_MIN_PER_STOP
            eta = eta_by_stop_service.get((c["origin_code"], c["service"]))
            c["walk_to_min"] = walk_to
            c["walk_from_min"] = walk_from
            c["ride_min"] = ride
            c["eta"] = eta
            # If no live ETA, assume worst-case 20-min wait so it ranks lower
            wait = eta if eta is not None else 20
            c["total_min"] = walk_to + wait + ride + walk_from

        # Dedup: for each service, keep the best (lowest total) candidate
        best: dict[str, dict] = {}
        for c in candidates:
            key = c["service"]
            if key not in best or c["total_min"] < best[key]["total_min"]:
                best[key] = c
        ranked = sorted(best.values(), key=lambda x: x["total_min"])[:limit]

        # Resolve terminus names for display
        stop_names = {
            s.get("BusStopCode"): s.get("Description", "")
            for s in cache.bus_stops
        }

        lines = [
            f"Best direct buses from ({from_latitude:.4f}, "
            f"{from_longitude:.4f}) to ({to_latitude:.4f}, "
            f"{to_longitude:.4f}):",
            "",
        ]
        for i, c in enumerate(ranked, 1):
            # terminus = last stop on this route in this direction
            route = cache.routes_by_service.get((c["service"], c["direction"]), [])
            terminus_code = route[-1][0] if route else ""
            terminus_name = stop_names.get(terminus_code, "") or terminus_code

            eta_str = (
                f"{c['eta']} min (live)"
                if c["eta"] is not None
                else "no live ETA"
            )
            lines.append(
                f"{i}. Bus {c['service']} → {terminus_name}   "
                f"(~{int(round(c['total_min']))} min total, estimated)"
            )
            lines.append(
                f"   Walk  : {int(c['origin_walk'])}m to Stop "
                f"{c['origin_code']} ({c['origin_desc']})  "
                f"[{int(round(c['walk_to_min']))} min]"
            )
            lines.append(f"   ETA   : {eta_str}")
            lines.append(
                f"   Ride  : {c['ride_stops']} stops "
                f"(~{int(round(c['ride_min']))} min, estimated)"
            )
            lines.append(
                f"   Alight: Stop {c['dest_code']} ({c['dest_desc']}), "
                f"{int(c['dest_walk'])}m walk"
            )
            lines.append("")
        lines.append(
            "Totals are estimates (walk 80 m/min, ~1.8 min per in-vehicle "
            "stop). Live ETA is from LTA; actual ride time may vary with "
            "traffic."
        )
        return "\n".join(lines)
