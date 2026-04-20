"""Direct-bus trip planner — the flagship routing tool.

Given origin and destination coordinates, this returns the best bus
journeys. It first looks for direct single-bus routes, then (if time
permits or no direct bus exists) for 1-transfer journeys — bus A,
short walk, bus B. Each candidate is scored by total walk + wait +
ride + walk and ranked against the others.

Time estimates are rough — 80 m/min walking, ~1.8 min per in-vehicle
stop, and a 10 min assumed wait at the transfer point since we don't
have scheduled intervals — and that is flagged in the tool's output.

Author: Jimmy Tong
"""

import math
from datetime import datetime, timezone

from api.lta import LTAClient
from cache import MobilityCache

WALK_M_PER_MIN = 80
RIDE_MIN_PER_STOP = 1.8
TRANSFER_WAIT_MIN = 10
NO_LIVE_ETA_WAIT_MIN = 20
MAX_TOTAL_MIN_DEFAULT = 90


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


def _candidate_stops(
    cache: MobilityCache,
    lat: float,
    lng: float,
    radius_m: int,
) -> list[tuple[str, str, float]]:
    hits: list[tuple[str, str, float]] = []
    for s in cache.bus_stops:
        try:
            slat = float(s["Latitude"])
            slng = float(s["Longitude"])
        except (KeyError, ValueError, TypeError):
            continue
        d = _haversine_m(lat, lng, slat, slng)
        if d <= radius_m:
            hits.append(
                (
                    str(s.get("BusStopCode", "")),
                    str(s.get("Description", "")),
                    d,
                )
            )
    return hits


def register_routing_tools(
    mcp, lta: LTAClient, cache: MobilityCache
) -> None:
    @mcp.tool()
    async def find_bus_route(
        from_latitude: float,
        from_longitude: float,
        to_latitude: float,
        to_longitude: float,
        max_walk_m: int = 600,
        max_transfer_walk_m: int = 200,
        max_total_min: int = MAX_TOTAL_MIN_DEFAULT,
        limit: int = 3,
    ) -> str:
        """Find the best bus journeys between two Singapore coordinates.

        Tries direct single-bus routes first, then 1-transfer journeys
        (bus A, short walk, bus B). Each candidate is scored by total
        walk + wait + ride + walk; returns the top `limit` across direct
        and transfer options together.

        Use resolve_location first if you only have place names. Use
        this BEFORE manually chaining search_bus_stops + get_bus_arrivals
        — this tool already does the comparison server-side.

        If no bus route (direct or 1-transfer) exists within the walk
        radii, the tool says so; fall back to MRT or multi-leg planning.
        Does not plan MRT or 2+ transfer journeys.
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

        origins = _candidate_stops(
            cache, from_latitude, from_longitude, max_walk_m
        )
        dests = _candidate_stops(
            cache, to_latitude, to_longitude, max_walk_m
        )

        if not origins or not dests:
            return (
                f"No bus stops within {max_walk_m}m of "
                f"{'origin' if not origins else 'destination'}. "
                "Increase max_walk_m or choose different points."
            )

        stop_coords: dict[str, tuple[float, float]] = {}
        stop_names: dict[str, str] = {}
        for s in cache.bus_stops:
            code = s.get("BusStopCode")
            if not code:
                continue
            try:
                stop_coords[code] = (float(s["Latitude"]), float(s["Longitude"]))
            except (KeyError, ValueError, TypeError):
                continue
            stop_names[code] = str(s.get("Description", ""))

        # ---------- Direct candidates ----------
        dest_info = {d[0]: d for d in dests}
        dest_codes = set(dest_info.keys())
        direct_candidates: list[dict] = []
        for o_code, o_desc, o_walk in origins:
            for o_svc, o_dir, o_seq in cache.routes_by_stop.get(o_code, []):
                route = cache.routes_by_service.get((o_svc, o_dir), [])
                for stop_code, stop_seq, _dist in route:
                    if stop_seq <= o_seq:
                        continue
                    if stop_code in dest_codes:
                        d_code, d_desc, d_walk = dest_info[stop_code]
                        direct_candidates.append(
                            {
                                "kind": "direct",
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

        # ---------- Transfer candidates ----------
        # Forward reach: for each origin bus, every stop it passes after the origin
        # is a potential alight (transfer start) point.
        # forward_reach: alight_stop_code -> list of leg1 dicts
        forward_reach: dict[str, list[dict]] = {}
        for o_code, o_desc, o_walk in origins:
            for o_svc, o_dir, o_seq in cache.routes_by_stop.get(o_code, []):
                route = cache.routes_by_service.get((o_svc, o_dir), [])
                for stop_code, stop_seq, _dist in route:
                    if stop_seq <= o_seq:
                        continue
                    forward_reach.setdefault(stop_code, []).append(
                        {
                            "origin_code": o_code,
                            "origin_desc": o_desc,
                            "origin_walk": o_walk,
                            "service_A": o_svc,
                            "direction_A": o_dir,
                            "origin_seq_A": o_seq,
                            "alight_code": stop_code,
                            "alight_seq_A": stop_seq,
                            "ride_stops_A": stop_seq - o_seq,
                        }
                    )

        # Backward reach: for each dest bus, every stop it passes BEFORE the dest
        # is a potential board (transfer end) point.
        backward_reach: dict[str, list[dict]] = {}
        for d_code, d_desc, d_walk in dests:
            for d_svc, d_dir, d_seq in cache.routes_by_stop.get(d_code, []):
                route = cache.routes_by_service.get((d_svc, d_dir), [])
                for stop_code, stop_seq, _dist in route:
                    if stop_seq >= d_seq:
                        continue
                    backward_reach.setdefault(stop_code, []).append(
                        {
                            "dest_code": d_code,
                            "dest_desc": d_desc,
                            "dest_walk": d_walk,
                            "service_B": d_svc,
                            "direction_B": d_dir,
                            "board_code": stop_code,
                            "board_seq_B": stop_seq,
                            "dest_seq_B": d_seq,
                            "ride_stops_B": d_seq - stop_seq,
                        }
                    )

        # Intersect: for each alight stop in forward_reach, check board stops in
        # backward_reach within transfer-walk range.
        transfer_candidates: list[dict] = []
        backward_items = list(backward_reach.items())
        for alight_code, leg1_list in forward_reach.items():
            a_coords = stop_coords.get(alight_code)
            if a_coords is None:
                continue
            for board_code, leg2_list in backward_items:
                if board_code == alight_code:
                    transfer_walk = 0.0
                else:
                    b_coords = stop_coords.get(board_code)
                    if b_coords is None:
                        continue
                    transfer_walk = _haversine_m(
                        a_coords[0], a_coords[1], b_coords[0], b_coords[1]
                    )
                    if transfer_walk > max_transfer_walk_m:
                        continue
                for leg1 in leg1_list:
                    for leg2 in leg2_list:
                        # Skip pointless same-service-same-direction transfer
                        if (
                            leg1["service_A"] == leg2["service_B"]
                            and leg1["direction_A"] == leg2["direction_B"]
                        ):
                            continue
                        transfer_candidates.append(
                            {
                                "kind": "transfer",
                                **leg1,
                                **leg2,
                                "transfer_walk_m": transfer_walk,
                            }
                        )

        if not direct_candidates and not transfer_candidates:
            return (
                f"No direct or 1-transfer bus route found from "
                f"({from_latitude:.4f}, {from_longitude:.4f}) to "
                f"({to_latitude:.4f}, {to_longitude:.4f}) within the walk "
                f"radii. Consider MRT or a multi-leg route."
            )

        # ---------- Live ETAs for all unique origin stops ----------
        origin_stops = {c["origin_code"] for c in direct_candidates}
        origin_stops |= {c["origin_code"] for c in transfer_candidates}
        eta_by_stop_service: dict[tuple[str, str], int | None] = {}
        for o_code in origin_stops:
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

        # ---------- Score ----------
        for c in direct_candidates:
            walk_to = c["origin_walk"] / WALK_M_PER_MIN
            walk_from = c["dest_walk"] / WALK_M_PER_MIN
            ride = c["ride_stops"] * RIDE_MIN_PER_STOP
            eta = eta_by_stop_service.get((c["origin_code"], c["service"]))
            wait = eta if eta is not None else NO_LIVE_ETA_WAIT_MIN
            c["eta"] = eta
            c["walk_to_min"] = walk_to
            c["walk_from_min"] = walk_from
            c["ride_min"] = ride
            c["total_min"] = walk_to + wait + ride + walk_from

        for c in transfer_candidates:
            walk_to = c["origin_walk"] / WALK_M_PER_MIN
            ride_A = c["ride_stops_A"] * RIDE_MIN_PER_STOP
            transfer_walk_min = c["transfer_walk_m"] / WALK_M_PER_MIN
            ride_B = c["ride_stops_B"] * RIDE_MIN_PER_STOP
            walk_from = c["dest_walk"] / WALK_M_PER_MIN
            eta_A = eta_by_stop_service.get(
                (c["origin_code"], c["service_A"])
            )
            wait_A = eta_A if eta_A is not None else NO_LIVE_ETA_WAIT_MIN
            wait_B = TRANSFER_WAIT_MIN
            c["eta_A"] = eta_A
            c["walk_to_min"] = walk_to
            c["ride_A_min"] = ride_A
            c["transfer_walk_min"] = transfer_walk_min
            c["ride_B_min"] = ride_B
            c["walk_from_min"] = walk_from
            c["total_min"] = (
                walk_to + wait_A + ride_A + transfer_walk_min
                + wait_B + ride_B + walk_from
            )

        # ---------- Filter by max_total_min ----------
        all_candidates = [
            c
            for c in (direct_candidates + transfer_candidates)
            if c["total_min"] <= max_total_min
        ]
        if not all_candidates:
            return (
                f"Bus options exist but all exceed the {max_total_min}-minute "
                "cap. Consider MRT or raise max_total_min."
            )

        # ---------- Dedupe ----------
        # Direct: best per service
        # Transfer: best per (service_A, service_B) pair
        best: dict[tuple, dict] = {}
        for c in all_candidates:
            if c["kind"] == "direct":
                key = ("direct", c["service"])
            else:
                key = ("transfer", c["service_A"], c["service_B"])
            if key not in best or c["total_min"] < best[key]["total_min"]:
                best[key] = c
        ranked = sorted(best.values(), key=lambda x: x["total_min"])[:limit]

        # ---------- Format ----------
        lines = [
            f"Best bus routes from ({from_latitude:.4f}, "
            f"{from_longitude:.4f}) to ({to_latitude:.4f}, "
            f"{to_longitude:.4f}):",
            "",
        ]
        for i, c in enumerate(ranked, 1):
            if c["kind"] == "direct":
                route = cache.routes_by_service.get(
                    (c["service"], c["direction"]), []
                )
                terminus_code = route[-1][0] if route else ""
                terminus_name = (
                    stop_names.get(terminus_code, "") or terminus_code
                )
                eta_str = (
                    f"{c['eta']} min (live)"
                    if c["eta"] is not None
                    else "no live ETA"
                )
                lines.append(
                    f"{i}. Bus {c['service']} → {terminus_name}   "
                    f"(~{int(round(c['total_min']))} min total, direct, "
                    "estimated)"
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
            else:
                route_A = cache.routes_by_service.get(
                    (c["service_A"], c["direction_A"]), []
                )
                term_A_code = route_A[-1][0] if route_A else ""
                term_A_name = (
                    stop_names.get(term_A_code, "") or term_A_code
                )
                route_B = cache.routes_by_service.get(
                    (c["service_B"], c["direction_B"]), []
                )
                term_B_code = route_B[-1][0] if route_B else ""
                term_B_name = (
                    stop_names.get(term_B_code, "") or term_B_code
                )
                eta_A_str = (
                    f"{c['eta_A']} min (live)"
                    if c["eta_A"] is not None
                    else "no live ETA"
                )
                alight_name = (
                    stop_names.get(c["alight_code"], "") or c["alight_code"]
                )
                board_name = (
                    stop_names.get(c["board_code"], "") or c["board_code"]
                )
                lines.append(
                    f"{i}. Bus {c['service_A']} → Bus {c['service_B']}   "
                    f"(~{int(round(c['total_min']))} min total, 1 transfer, "
                    "estimated)"
                )
                lines.append(
                    f"   Walk    : {int(c['origin_walk'])}m to Stop "
                    f"{c['origin_code']} ({c['origin_desc']})  "
                    f"[{int(round(c['walk_to_min']))} min]"
                )
                lines.append(
                    f"   Leg 1   : Bus {c['service_A']} → {term_A_name}, "
                    f"ETA {eta_A_str}, {c['ride_stops_A']} stops "
                    f"(~{int(round(c['ride_A_min']))} min)"
                )
                lines.append(
                    f"   Transfer: alight Stop {c['alight_code']} "
                    f"({alight_name}); walk {int(c['transfer_walk_m'])}m "
                    f"[{int(round(c['transfer_walk_min']))} min] to Stop "
                    f"{c['board_code']} ({board_name})"
                )
                lines.append(
                    f"   Leg 2   : Bus {c['service_B']} → {term_B_name}, "
                    f"wait ~{TRANSFER_WAIT_MIN} min (estimated), "
                    f"{c['ride_stops_B']} stops "
                    f"(~{int(round(c['ride_B_min']))} min)"
                )
                lines.append(
                    f"   Alight  : Stop {c['dest_code']} ({c['dest_desc']}), "
                    f"{int(c['dest_walk'])}m walk"
                )
                lines.append("")

        lines.append(
            "Totals are estimates (walk 80 m/min, ~1.8 min per in-vehicle "
            f"stop, ~{TRANSFER_WAIT_MIN} min assumed wait at any transfer). "
            "Live ETA is from LTA; actual times vary with traffic."
        )
        return "\n".join(lines)
