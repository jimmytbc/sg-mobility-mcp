"""Direct-bus trip planner — the flagship routing tool.

Given origin and destination coordinates, this returns the best bus
journeys. It first looks for direct single-bus routes, then (if time
permits or no direct bus exists) for 1-transfer journeys — bus A,
short walk, bus B. Each candidate is scored by total walk + wait +
ride + walk and ranked against the others.

Time estimates are rough — 80 m/min walking, ~1.8 min per in-vehicle
stop, and a 10 min assumed wait at the transfer point since we don't
have scheduled intervals — and that is flagged in the tool's footer.

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
    ERR_LTA_AUTH_FAILED,
    ERR_LTA_ENDPOINT_NOT_FOUND,
    ERR_LTA_RATE_LIMITED,
    ERR_LTA_TIMEOUT,
    ERR_NO_BUS_ROUTE,
    MSG_BUS_ROUTE_ESTIMATES,
    MSG_ERR_LTA_AUTH_FAILED,
    MSG_ERR_LTA_RATE_LIMITED,
    MSG_ERR_LTA_TIMEOUT,
    MSG_FIRST_CALL_WARM,
    MSG_TRUNCATED_2T,
    error,
    footer,
    header,
    msg_err_lta_endpoint_not_found,
    msg_err_no_bus_route,
)

WALK_M_PER_MIN = 80
RIDE_MIN_PER_STOP = 1.8
TRANSFER_WAIT_MIN = 10
NO_LIVE_ETA_WAIT_MIN = 20
# Direct + 1-transfer keep the v0.1.0 90-minute ceiling. 2-transfer uses
# max_total_min (default raised to 120 per FR-5.2 to accommodate the
# extra transfer wait).
DIRECT_TRANSFER_MAX_MIN = 90
MAX_TOTAL_MIN_DEFAULT = 120
# 500 candidate triples per call (FR-5.5 / RISK-6). Prevents naive
# enumeration from scanning billions of combinations.
TWO_TRANSFER_CANDIDATE_CAP = 500


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


def _stop_label(s: dict) -> str:
    """'<description>, <road>' — ensures stops like 'Blk 220C' aren't
    ambiguous across estates by pairing the block with its road name.
    Falls back to whichever side is present if the other is blank."""
    desc = str(s.get("Description", "") or "").strip()
    road = str(s.get("RoadName", "") or "").strip()
    if desc and road:
        return f"{desc}, {road}"
    return desc or road


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


def _walk_neighbors(
    stop_code: str,
    stop_coords: dict[str, tuple[float, float]],
    max_walk_m: int,
) -> list[tuple[str, float]]:
    """Return (other_code, walk_metres) for every bus stop within
    max_walk_m of stop_code. Bounding-box pre-filter first so this
    stays fast even over the full ~5,200-stop catalog."""
    center = stop_coords.get(stop_code)
    if center is None:
        return []
    lat0, lng0 = center
    # 1 deg lat ≈ 111 km; Singapore lng-per-deg is comparable at ~111 km.
    # 1.5× safety factor on the bounding box to cover Haversine edge cases.
    delta_deg = (max_walk_m / 111_000.0) * 1.5
    out: list[tuple[str, float]] = []
    for other_code, (olat, olng) in stop_coords.items():
        if other_code == stop_code:
            out.append((other_code, 0.0))
            continue
        if abs(olat - lat0) > delta_deg or abs(olng - lng0) > delta_deg:
            continue
        d = _haversine_m(lat0, lng0, olat, olng)
        if d <= max_walk_m:
            out.append((other_code, d))
    return out


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
                    _stop_label(s),
                    d,
                )
            )
    return hits


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


async def find_bus_route_impl(
    lta: LTAClient,
    cache: MobilityCache,
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
    max_walk_m: int = 600,
    max_transfer_walk_m: int = 200,
    max_total_min: int = MAX_TOTAL_MIN_DEFAULT,
    limit: int = 3,
) -> str:
    """Plain-async implementation of find_bus_route.

    Module-level so the Phase 0 probe and Phase 4's `find_route` can
    call it directly without going through the MCP tool indirection.
    The registered `@mcp.tool()` in `register_routing_tools` is a thin
    wrapper around this.
    """
    try:
        did_warm_stops = await cache.ensure_stops_warm(lta)
        did_warm_routes = await cache.ensure_routes_warm(lta)
    except UpstreamError as exc:
        return _lta_error(exc)
    did_warm = did_warm_stops or did_warm_routes

    def _err_no_route() -> str:
        return error(
            ERR_NO_BUS_ROUTE,
            msg_err_no_bus_route(
                from_latitude,
                from_longitude,
                to_latitude,
                to_longitude,
                max_walk_m,
                max_transfer_walk_m,
                max_total_min,
            ),
        )

    # Short-circuit trivial distance
    direct_m = _haversine_m(
        from_latitude, from_longitude, to_latitude, to_longitude
    )
    if direct_m < 300:
        body = (
            f"The origin and destination are only {int(direct_m)}m apart "
            "— walking is likely faster than taking a bus."
        )
        lines = [
            header(
                "find_bus_route",
                f"walking suggested from "
                f"({from_latitude:.5f}, {from_longitude:.5f}) to "
                f"({to_latitude:.5f}, {to_longitude:.5f})",
            ),
            "",
            body,
        ]
        if did_warm:
            lines.extend(["", footer(MSG_FIRST_CALL_WARM)])
        return "\n".join(lines)

    origins = _candidate_stops(
        cache, from_latitude, from_longitude, max_walk_m
    )
    dests = _candidate_stops(
        cache, to_latitude, to_longitude, max_walk_m
    )

    # Sort by walk distance ascending for cost-promising enumeration
    # order — 2-transfer's 500-candidate cap means early candidates
    # bias the final ranking, so we want the shortest-walk options
    # enumerated first (FR-5.5).
    origins.sort(key=lambda o: o[2])
    dests.sort(key=lambda d: d[2])

    if not origins or not dests:
        return _err_no_route()

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
        stop_names[code] = _stop_label(s)

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

    # ---------- 2-transfer candidates (Phase 3, FR-5.1 / FR-5.5) ----------
    # Meet-in-the-middle: forward_reach already gives leg1 (origin →
    # alight_1 via service_A). Extend by a walk to build forward_board
    # (boardable stops for leg 2). backward_reach already gives leg3
    # (board_2 → dest via service_C). Extend by a walk backwards to
    # build backward_alight (stops we can alight leg 2 at). For each
    # middle service, pair a forward_board stop (earlier seq) with a
    # backward_alight stop (later seq) — that's a valid 2-transfer.
    forward_board: dict[str, list[dict]] = {}
    for alight_1_code, leg1_list in forward_reach.items():
        for neighbour_code, walk_1 in _walk_neighbors(
            alight_1_code, stop_coords, max_transfer_walk_m
        ):
            for leg1 in leg1_list:
                forward_board.setdefault(neighbour_code, []).append(
                    {
                        **leg1,
                        "alight_1_code": leg1["alight_code"],
                        "alight_1_seq_A": leg1["alight_seq_A"],
                        "board_1_code": neighbour_code,
                        "transfer_walk_1_m": walk_1,
                    }
                )

    backward_alight: dict[str, list[dict]] = {}
    for board_2_code, leg3_list in backward_reach.items():
        for neighbour_code, walk_2 in _walk_neighbors(
            board_2_code, stop_coords, max_transfer_walk_m
        ):
            for leg3 in leg3_list:
                # Rename leg3's "service_B" (from 1-transfer context) to
                # service_C to avoid collision with the 2-transfer middle
                # service we're about to enumerate.
                backward_alight.setdefault(neighbour_code, []).append(
                    {
                        "dest_code": leg3["dest_code"],
                        "dest_desc": leg3["dest_desc"],
                        "dest_walk": leg3["dest_walk"],
                        "service_C": leg3["service_B"],
                        "direction_C": leg3["direction_B"],
                        "board_2_code": board_2_code,
                        "board_2_seq_C": leg3["board_seq_B"],
                        "dest_seq_C": leg3["dest_seq_B"],
                        "ride_stops_C": leg3["ride_stops_B"],
                        "transfer_walk_2_m": walk_2,
                        "alight_2_code": neighbour_code,
                    }
                )

    two_transfer_candidates: list[dict] = []
    truncated_2t = False
    if forward_board and backward_alight:
        # Cost-promising order for the middle service (FR-5.5): the
        # 500-candidate cap will fire on cross-island pairs, so we sort
        # candidate middle services by the shortest ride they can
        # produce (min board→alight sequence span over all forward_board
        # / backward_alight pairs on the route). Services with the
        # shortest middle leg get evaluated first.
        ranked_middle: list[tuple[int, tuple[str, int], list]] = []
        for (svc_B, dir_B), route_B in cache.routes_by_service.items():
            board_seqs = [
                s[1] for s in route_B if s[0] in forward_board
            ]
            alight_seqs = [
                s[1] for s in route_B if s[0] in backward_alight
            ]
            if not board_seqs or not alight_seqs:
                continue
            best_span = min(
                (a - b for b in board_seqs for a in alight_seqs if a > b),
                default=None,
            )
            if best_span is None:
                continue
            ranked_middle.append((best_span, (svc_B, dir_B), route_B))
        ranked_middle.sort(key=lambda x: x[0])

        for _best_span, (svc_B, dir_B), route_B in ranked_middle:
            if truncated_2t:
                break
            # Walk this service once, collecting (seq, stop, role, info)
            # at positions that match either endpoint of a 2-transfer leg.
            board_positions: list[tuple[int, str, list[dict]]] = []
            alight_positions: list[tuple[int, str, list[dict]]] = []
            for stop_code, stop_seq, _dist in route_B:
                if stop_code in forward_board:
                    board_positions.append(
                        (stop_seq, stop_code, forward_board[stop_code])
                    )
                if stop_code in backward_alight:
                    alight_positions.append(
                        (stop_seq, stop_code, backward_alight[stop_code])
                    )
            if not board_positions or not alight_positions:
                continue
            for b_seq, board_1_on_svc_B, board_entries in board_positions:
                if truncated_2t:
                    break
                for a_seq, alight_2_on_svc_B, alight_entries in alight_positions:
                    if a_seq <= b_seq:
                        continue
                    if truncated_2t:
                        break
                    for leg1 in board_entries:
                        # Ensure leg1's board_1_code matches this route-B stop
                        if leg1["board_1_code"] != board_1_on_svc_B:
                            continue
                        # Reject if service_A == service_B (same service, not a transfer)
                        if (
                            leg1["service_A"] == svc_B
                            and leg1["direction_A"] == dir_B
                        ):
                            continue
                        if truncated_2t:
                            break
                        for leg3 in alight_entries:
                            if leg3["alight_2_code"] != alight_2_on_svc_B:
                                continue
                            # Reject if service_B == service_C (same service)
                            if (
                                leg3["service_C"] == svc_B
                                and leg3["direction_C"] == dir_B
                            ):
                                continue
                            # Reject if service_A == service_C (redundant)
                            if (
                                leg1["service_A"] == leg3["service_C"]
                                and leg1["direction_A"] == leg3["direction_C"]
                            ):
                                continue
                            two_transfer_candidates.append(
                                {
                                    "kind": "2_transfer",
                                    # Leg 1
                                    "origin_code": leg1["origin_code"],
                                    "origin_desc": leg1["origin_desc"],
                                    "origin_walk": leg1["origin_walk"],
                                    "service_A": leg1["service_A"],
                                    "direction_A": leg1["direction_A"],
                                    "origin_seq_A": leg1["origin_seq_A"],
                                    "alight_1_code": leg1["alight_1_code"],
                                    "alight_1_seq_A": leg1["alight_1_seq_A"],
                                    "ride_stops_A": leg1["ride_stops_A"],
                                    # Transfer 1
                                    "board_1_code": leg1["board_1_code"],
                                    "transfer_walk_1_m": leg1[
                                        "transfer_walk_1_m"
                                    ],
                                    # Leg 2 (middle)
                                    "service_B": svc_B,
                                    "direction_B": dir_B,
                                    "board_1_seq_B": b_seq,
                                    "alight_2_code": alight_2_on_svc_B,
                                    "alight_2_seq_B": a_seq,
                                    "ride_stops_B": a_seq - b_seq,
                                    # Transfer 2
                                    "board_2_code": leg3["board_2_code"],
                                    "transfer_walk_2_m": leg3[
                                        "transfer_walk_2_m"
                                    ],
                                    # Leg 3
                                    "service_C": leg3["service_C"],
                                    "direction_C": leg3["direction_C"],
                                    "board_2_seq_C": leg3["board_2_seq_C"],
                                    "dest_seq_C": leg3["dest_seq_C"],
                                    "ride_stops_C": leg3["ride_stops_C"],
                                    # Dest
                                    "dest_code": leg3["dest_code"],
                                    "dest_desc": leg3["dest_desc"],
                                    "dest_walk": leg3["dest_walk"],
                                }
                            )
                            if (
                                len(two_transfer_candidates)
                                >= TWO_TRANSFER_CANDIDATE_CAP
                            ):
                                truncated_2t = True
                                break

    if (
        not direct_candidates
        and not transfer_candidates
        and not two_transfer_candidates
    ):
        return _err_no_route()

    # ---------- Live ETAs for all unique origin stops ----------
    origin_stops = {c["origin_code"] for c in direct_candidates}
    origin_stops |= {c["origin_code"] for c in transfer_candidates}
    origin_stops |= {c["origin_code"] for c in two_transfer_candidates}
    eta_by_stop_service: dict[tuple[str, str], int | None] = {}
    for o_code in origin_stops:
        try:
            data = await lta.get_bus_arrival(o_code)
        except UpstreamError:
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

    for c in two_transfer_candidates:
        walk_to = c["origin_walk"] / WALK_M_PER_MIN
        ride_A = c["ride_stops_A"] * RIDE_MIN_PER_STOP
        transfer_walk_1_min = c["transfer_walk_1_m"] / WALK_M_PER_MIN
        ride_B = c["ride_stops_B"] * RIDE_MIN_PER_STOP
        transfer_walk_2_min = c["transfer_walk_2_m"] / WALK_M_PER_MIN
        ride_C = c["ride_stops_C"] * RIDE_MIN_PER_STOP
        walk_from = c["dest_walk"] / WALK_M_PER_MIN
        eta_A = eta_by_stop_service.get(
            (c["origin_code"], c["service_A"])
        )
        wait_A = eta_A if eta_A is not None else NO_LIVE_ETA_WAIT_MIN
        wait_B = TRANSFER_WAIT_MIN
        wait_C = TRANSFER_WAIT_MIN
        c["eta_A"] = eta_A
        c["walk_to_min"] = walk_to
        c["ride_A_min"] = ride_A
        c["transfer_walk_1_min"] = transfer_walk_1_min
        c["ride_B_min"] = ride_B
        c["transfer_walk_2_min"] = transfer_walk_2_min
        c["ride_C_min"] = ride_C
        c["walk_from_min"] = walk_from
        c["total_min"] = (
            walk_to
            + wait_A + ride_A + transfer_walk_1_min
            + wait_B + ride_B + transfer_walk_2_min
            + wait_C + ride_C + walk_from
        )

    # Per FR-5.2: direct + 1-transfer stay bounded by 90 min (never
    # expanded by the raised max_total_min default); only 2-transfer
    # uses the full max_total_min. If the caller passes a tighter
    # max_total_min, it applies to all kinds.
    direct_transfer_cap = min(DIRECT_TRANSFER_MAX_MIN, max_total_min)
    all_candidates: list[dict] = []
    for c in direct_candidates + transfer_candidates:
        if c["total_min"] <= direct_transfer_cap:
            all_candidates.append(c)
    for c in two_transfer_candidates:
        if c["total_min"] <= max_total_min:
            all_candidates.append(c)

    if not all_candidates:
        return _err_no_route()

    best: dict[tuple, dict] = {}
    for c in all_candidates:
        if c["kind"] == "direct":
            key = ("direct", c["service"])
        elif c["kind"] == "transfer":
            key = ("transfer", c["service_A"], c["service_B"])
        else:
            key = (
                "2_transfer",
                c["service_A"],
                c["service_B"],
                c["service_C"],
            )
        if key not in best or c["total_min"] < best[key]["total_min"]:
            best[key] = c
    ranked = sorted(best.values(), key=lambda x: x["total_min"])[:limit]

    summary = (
        f"{len(ranked)} options from "
        f"({from_latitude:.5f}, {from_longitude:.5f}) to "
        f"({to_latitude:.5f}, {to_longitude:.5f})"
    )
    lines = [header("find_bus_route", summary), ""]
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
        elif c["kind"] == "transfer":
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
        else:
            # 2-transfer option — three buses, two transfer points.
            route_A = cache.routes_by_service.get(
                (c["service_A"], c["direction_A"]), []
            )
            term_A_name = (
                stop_names.get(route_A[-1][0], "") or (route_A[-1][0] if route_A else "")
            )
            route_B = cache.routes_by_service.get(
                (c["service_B"], c["direction_B"]), []
            )
            term_B_name = (
                stop_names.get(route_B[-1][0], "") or (route_B[-1][0] if route_B else "")
            )
            route_C = cache.routes_by_service.get(
                (c["service_C"], c["direction_C"]), []
            )
            term_C_name = (
                stop_names.get(route_C[-1][0], "") or (route_C[-1][0] if route_C else "")
            )
            eta_A_str = (
                f"{c['eta_A']} min (live)"
                if c["eta_A"] is not None
                else "no live ETA"
            )
            alight_1_name = (
                stop_names.get(c["alight_1_code"], "")
                or c["alight_1_code"]
            )
            board_1_name = (
                stop_names.get(c["board_1_code"], "")
                or c["board_1_code"]
            )
            alight_2_name = (
                stop_names.get(c["alight_2_code"], "")
                or c["alight_2_code"]
            )
            board_2_name = (
                stop_names.get(c["board_2_code"], "")
                or c["board_2_code"]
            )
            lines.append(
                f"OPTION {i} — 2-TRANSFER — "
                f"{int(round(c['total_min']))} min total"
            )
            lines.append(
                f"   Walk     : {int(c['origin_walk'])}m to Stop "
                f"{c['origin_code']} ({c['origin_desc']})  "
                f"[{int(round(c['walk_to_min']))} min]"
            )
            lines.append(
                f"   Leg 1    : Bus {c['service_A']} → {term_A_name}, "
                f"ETA {eta_A_str}, {c['ride_stops_A']} stops "
                f"(~{int(round(c['ride_A_min']))} min)"
            )
            lines.append(
                f"   Transfer : alight Stop {c['alight_1_code']} "
                f"({alight_1_name}); walk "
                f"{int(c['transfer_walk_1_m'])}m "
                f"[{int(round(c['transfer_walk_1_min']))} min] to Stop "
                f"{c['board_1_code']} ({board_1_name})"
            )
            lines.append(
                f"   Leg 2    : Bus {c['service_B']} → {term_B_name}, "
                f"wait ~{TRANSFER_WAIT_MIN} min (estimated), "
                f"{c['ride_stops_B']} stops "
                f"(~{int(round(c['ride_B_min']))} min)"
            )
            lines.append(
                f"   Transfer : alight Stop {c['alight_2_code']} "
                f"({alight_2_name}); walk "
                f"{int(c['transfer_walk_2_m'])}m "
                f"[{int(round(c['transfer_walk_2_min']))} min] to Stop "
                f"{c['board_2_code']} ({board_2_name})"
            )
            lines.append(
                f"   Leg 3    : Bus {c['service_C']} → {term_C_name}, "
                f"wait ~{TRANSFER_WAIT_MIN} min (estimated), "
                f"{c['ride_stops_C']} stops "
                f"(~{int(round(c['ride_C_min']))} min)"
            )
            lines.append(
                f"   Alight   : Stop {c['dest_code']} ({c['dest_desc']}), "
                f"{int(c['dest_walk'])}m walk"
            )
            lines.append("")

    lines.append(footer(MSG_BUS_ROUTE_ESTIMATES))
    # FR-E.13: surface the truncation footer only when the partial
    # result actually included a 2-transfer option. If the ranked
    # output is all direct/1-transfer, the 500-cap didn't affect what
    # the caller sees, and the note would be misleading.
    if truncated_2t and any(c["kind"] == "2_transfer" for c in ranked):
        lines.append(footer(MSG_TRUNCATED_2T))
    if did_warm:
        lines.append(footer(MSG_FIRST_CALL_WARM))
    return "\n".join(lines)


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
        """Finds best bus journeys (direct, 1-transfer, or 2-transfer)
        between two Singapore coordinates. Returns options ranked by
        total walk + wait + ride — option 1 is the fastest; preserve
        this ordering when recommending to the user. Direct/1-transfer
        capped at 90 min; 2-transfer uses max_total_min (default 120)
        and caps at 500 candidates per call — truncation is noted.

        Use resolve_location first for place names. For unified bus +
        walk + MRT-hint discovery, prefer find_route.
        """
        return await find_bus_route_impl(
            lta,
            cache,
            from_latitude,
            from_longitude,
            to_latitude,
            to_longitude,
            max_walk_m=max_walk_m,
            max_transfer_walk_m=max_transfer_walk_m,
            max_total_min=max_total_min,
            limit=limit,
        )
