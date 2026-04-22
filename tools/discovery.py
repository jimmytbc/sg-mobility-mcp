"""Unified transport discovery — the Phase 4 flagship tool.

find_route composes Phase 3's find_bus_route, a straight-line walking
estimate, and an MRT-suggestion hint (sourced from Phase 2's loaded
mrt_stations catalog) into a single ranked response. It is the
recommended starting point for "best route from A to B" questions —
the agent does not have to chain find_bus_route + walking calc + MRT
lookup itself.

find_route is a deterministic dispatcher, not an optimizing multi-modal
planner. Ranking is by estimated total time (bus + walk only; MRT has
no time estimate and appears last). Long-distance pairs (>25 km
straight-line) short-circuit the bus enumeration to keep latency
bounded per RISK-11.

Author: Jimmy Tong
"""

from __future__ import annotations

import math
import re

from api.lta import LTAClient
from cache import MobilityCache
from tools._format import (
    ERR_COORDINATES_OUT_OF_BOUNDS,
    MSG_MRT_SUGGESTION,
    MSG_NO_WALK_LONG,
    coords_in_sg,
    error,
    footer,
    header,
    msg_err_coords_out_of_bounds,
)
from tools.routing import find_bus_route_impl

WALK_M_PER_MIN = 80
WALK_MAX_MIN = 25
MRT_SUGGESTION_RADIUS_M = 800
LONG_DISTANCE_SHORTCIRCUIT_M = 25_000


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


# find_bus_route (routing.py) uses two header styles — legacy
# "<i>. Bus X → ..." for direct/1-transfer, and Phase 3's
# "OPTION <i> — 2-TRANSFER — <m> min total" for 2-transfer. Match both.
_DIRECT_RE = re.compile(
    r"^\d+\.\s+Bus\s+.*~(\d+)\s+min\s+total,\s+direct,",
    re.IGNORECASE,
)
_1T_RE = re.compile(
    r"^\d+\.\s+Bus\s+.*~(\d+)\s+min\s+total,\s+1\s+transfer,",
    re.IGNORECASE,
)
_2T_RE = re.compile(
    r"^OPTION\s+\d+\s+—\s+2-TRANSFER\s+—\s+(\d+)\s+min\s+total",
    re.IGNORECASE,
)


def _parse_bus_options(text: str, limit: int = 3) -> list[dict]:
    """Extract option blocks from a find_bus_route response.

    Returns a list of {kind_label, total_min, body_lines}. kind_label
    is BUS / BUS (1-TRANSFER) / BUS (2-TRANSFER) ready for a LABEL_OPTION_BUS
    substitution. body_lines includes the original find_bus_route option
    header line as its first entry — per Phase 4 Task 3, the per-leg
    body is delegated unchanged.
    """
    options: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("Note:"):
            if current is not None:
                options.append(current)
                current = None
            continue
        if not line.strip():
            if current is not None:
                options.append(current)
                current = None
            continue
        m = _DIRECT_RE.match(line)
        if m:
            if current is not None:
                options.append(current)
            current = {
                "kind_label": "BUS",
                "total_min": int(m.group(1)),
                "body_lines": [line],
            }
            continue
        m = _1T_RE.match(line)
        if m:
            if current is not None:
                options.append(current)
            current = {
                "kind_label": "BUS (1-TRANSFER)",
                "total_min": int(m.group(1)),
                "body_lines": [line],
            }
            continue
        m = _2T_RE.match(line)
        if m:
            if current is not None:
                options.append(current)
            current = {
                "kind_label": "BUS (2-TRANSFER)",
                "total_min": int(m.group(1)),
                "body_lines": [line],
            }
            continue
        if current is not None:
            current["body_lines"].append(line)
    if current is not None:
        options.append(current)
    return options[:limit]


def _parse_footers(text: str) -> list[str]:
    """Pick out Note: footer lines so find_route can propagate them
    (MSG_BUS_ROUTE_ESTIMATES, MSG_TRUNCATED_2T, MSG_FIRST_CALL_WARM).
    """
    return [ln for ln in text.splitlines() if ln.startswith("Note: ")]


def _nearest_station(
    mrt_stations: list[dict], lat: float, lng: float, radius_m: float
) -> tuple[dict, float] | None:
    best: tuple[dict, float] | None = None
    for st in mrt_stations:
        d = _haversine_m(lat, lng, st["latitude"], st["longitude"])
        if d > radius_m:
            continue
        if best is None or d < best[1]:
            best = (st, d)
    return best


def _format_station(st: dict) -> str:
    codes = "/".join(st["codes"])
    return f"{st['name']} ({codes})"


def _format_distance_m(d: float) -> str:
    return f"{int(round(d))}m"


def _build_mrt_suggestion(
    mrt_stations: list[dict],
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
) -> list[str] | None:
    """Return the body lines of the MRT SUGGESTION block, or None.

    The indented lines under `OPTION <n> — MRT SUGGESTION`. Caller adds
    the OPTION header and separating blank line.
    """
    origin = _nearest_station(
        mrt_stations, from_lat, from_lng, MRT_SUGGESTION_RADIUS_M
    )
    dest = _nearest_station(
        mrt_stations, to_lat, to_lng, MRT_SUGGESTION_RADIUS_M
    )
    if origin is None or dest is None:
        return None
    origin_st, origin_d = origin
    dest_st, dest_d = dest
    # Reject degenerate case: origin and destination resolve to the same
    # nearest station — the suggestion has no informational value.
    if origin_st["name"] == dest_st["name"]:
        return None

    shared = [l for l in origin_st["lines"] if l in set(dest_st["lines"])]
    board_line = shared[0] if shared else origin_st["lines"][0]
    alight_line = shared[0] if shared else dest_st["lines"][0]

    body: list[str] = []
    body.append(
        f"   Board candidate : {_format_station(origin_st)} on "
        f"{board_line} — {_format_distance_m(origin_d)} walk from origin"
    )
    body.append(
        f"   Alight candidate: {_format_station(dest_st)} on "
        f"{alight_line} — {_format_distance_m(dest_d)} walk to destination"
    )
    if shared:
        body.append(f"   Lines: {' · '.join(shared)} (direct)")
    else:
        body.append(
            f"   Lines: {origin_st['lines'][0]} · "
            f"{dest_st['lines'][0]} (transfer required)"
        )
    body.append(f"   {footer(MSG_MRT_SUGGESTION)}")
    return body


async def find_route_impl(
    lta: LTAClient,
    cache: MobilityCache,
    mrt_stations: list[dict],
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> str:
    if not coords_in_sg(from_latitude, from_longitude):
        return error(
            ERR_COORDINATES_OUT_OF_BOUNDS,
            msg_err_coords_out_of_bounds(from_latitude, from_longitude),
        )
    if not coords_in_sg(to_latitude, to_longitude):
        return error(
            ERR_COORDINATES_OUT_OF_BOUNDS,
            msg_err_coords_out_of_bounds(to_latitude, to_longitude),
        )

    direct_m = _haversine_m(
        from_latitude, from_longitude, to_latitude, to_longitude
    )
    walk_min = direct_m / WALK_M_PER_MIN
    walk_km = direct_m / 1000.0
    long_distance = direct_m > LONG_DISTANCE_SHORTCIRCUIT_M

    bus_options: list[dict] = []
    bus_footers: list[str] = []
    if not long_distance:
        bus_text = await find_bus_route_impl(
            lta,
            cache,
            from_latitude,
            from_longitude,
            to_latitude,
            to_longitude,
        )
        if bus_text and not bus_text.startswith("ERR_"):
            bus_options = _parse_bus_options(bus_text)
            bus_footers = _parse_footers(bus_text)

    mrt_body = _build_mrt_suggestion(
        mrt_stations,
        from_latitude,
        from_longitude,
        to_latitude,
        to_longitude,
    )

    # FR-6.3 walking inclusion. Walking-under-25-min always shows.
    # Walking-over-25-min is included only as a fallback when neither
    # bus nor MRT is available.
    walk_included = walk_min < WALK_MAX_MIN or (
        not bus_options and mrt_body is None
    )
    walk_omitted = not walk_included

    # Empty-everything case: per Task 7, give a clean explanation.
    # With the FR-6.3 fallback above, this only fires when walking
    # itself was somehow not produced (e.g., zero-distance degenerate).
    if not bus_options and mrt_body is None and not walk_included:
        summary = (
            f"0 options from ({from_latitude:.5f}, {from_longitude:.5f}) "
            f"to ({to_latitude:.5f}, {to_longitude:.5f})"
        )
        return (
            header("find_route", summary)
            + "\n\nNo bus route within reach, no MRT station within "
            f"{MRT_SUGGESTION_RADIUS_M}m of either endpoint, and "
            "walking was not produced. Try widening the search or a "
            "different mode."
        )

    # Rank bus + walk by ascending time. MRT goes last (no time).
    ranked: list[tuple[str, float, dict | None]] = []
    for b in bus_options:
        ranked.append(("bus", float(b["total_min"]), b))
    if walk_included:
        ranked.append(("walk", walk_min, None))
    ranked.sort(key=lambda x: x[1])

    total_options = len(ranked) + (1 if mrt_body else 0)
    summary = (
        f"{total_options} options from "
        f"({from_latitude:.5f}, {from_longitude:.5f}) to "
        f"({to_latitude:.5f}, {to_longitude:.5f})"
    )
    out: list[str] = [header("find_route", summary), ""]

    idx = 0
    for kind, _t, payload in ranked:
        idx += 1
        if kind == "bus":
            assert payload is not None
            out.append(
                f"OPTION {idx} — {payload['kind_label']} — "
                f"{payload['total_min']} min total"
            )
            out.extend(payload["body_lines"])
            out.append("")
        else:  # walk
            out.append(
                f"OPTION {idx} — WALK — {int(round(walk_min))} min "
                f"({walk_km:.1f} km)"
            )
            out.append("")

    if mrt_body:
        idx += 1
        out.append(f"OPTION {idx} — MRT SUGGESTION")
        out.extend(mrt_body)
        out.append("")

    while out and not out[-1].strip():
        out.pop()

    footers_out: list[str] = []
    footers_out.extend(bus_footers)
    if walk_omitted:
        footers_out.append(footer(MSG_NO_WALK_LONG))

    if footers_out:
        out.append("")
        out.extend(footers_out)

    return "\n".join(out)


def register_discovery_tools(
    mcp,
    lta: LTAClient,
    cache: MobilityCache,
    mrt_stations: list[dict],
) -> None:
    @mcp.tool()
    async def find_route(
        from_latitude: float,
        from_longitude: float,
        to_latitude: float,
        to_longitude: float,
    ) -> str:
        """Recommended entry point for "best route from A to B".

        Returns ranked bus options (delegated to find_bus_route, up to
        2 transfers), a walking estimate when under 25 min, and an
        MRT suggestion when both endpoints are within 800m of a
        station. OPTION 1 is the fastest — preserve this ordering
        when recommending to the user; MRT suggestion appears last.
        Call resolve_location first for place names. Prefer this
        over chaining find_bus_route + walk + MRT lookup.
        """
        return await find_route_impl(
            lta,
            cache,
            mrt_stations,
            from_latitude,
            from_longitude,
            to_latitude,
            to_longitude,
        )
