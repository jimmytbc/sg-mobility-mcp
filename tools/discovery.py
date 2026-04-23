"""Multimodal discovery — the Phase 5 `find_route` orchestrator.

`find_route` is a thin orchestrator over OneMap's Public Transport
routing endpoint per FR-7.1 — the tool does not plan routes, score
itineraries, or estimate durations. OneMap returns multimodal
time-ranked itineraries mixing walking, bus, and MRT/LRT; this tool
wraps them in the A1 envelope from `tools/_pt_routing.py`.

On OneMap failure (5xx, 429-after-backoff, or zero itineraries) the
orchestrator falls back to `find_bus_route_impl` per FR-7.4 and marks
the response with a `Note:` footer naming the reason. When the
fallback also fails, a `_TERMINAL` error prefix replaces the body.

Author: Jimmy Tong
"""

from __future__ import annotations

import re

from api.errors import (
    OneMapAuthFailed,
    OneMapRoutingRateLimited,
    OneMapRoutingServiceDown,
    OneMapSchemaDrift,
    OneMapTimeout,
    UpstreamError,
)
from api.lta import LTAClient
from api.onemap import OneMapClient
from cache import MobilityCache
from tools._format import (
    ERR_COORDINATES_OUT_OF_BOUNDS,
    ERR_NO_PT_ROUTE,
    ERR_ONEMAP_AUTH_FAILED,
    ERR_ONEMAP_TIMEOUT,
    ERR_ROUTING_RATE_LIMITED,
    ERR_ROUTING_SERVICE_DOWN,
    MSG_ERR_NO_PT_ROUTE,
    MSG_ERR_ONEMAP_AUTH_FAILED,
    MSG_ERR_ONEMAP_TIMEOUT,
    MSG_ROUTING_RATE_LIMITED,
    MSG_ROUTING_RATE_LIMITED_TERMINAL,
    MSG_ROUTING_SERVICE_DOWN,
    MSG_ROUTING_SERVICE_DOWN_TERMINAL,
    coords_in_sg,
    error,
    footer,
    header,
    msg_err_coords_out_of_bounds,
)
from tools._pt_routing import format_envelope, parse_itineraries
from tools.routing import find_bus_route_impl

# Calibrated against 5 representative endpoint pairs at 1000 / 1500 /
# 2000 / 2500 m (scratch/phase-5-calibration.log). 1000 m is the
# lowest value that returns the same 3 itineraries as 2500 m for all
# routeable pairs, with no itinerary flagged walkLimitExceeded.
MAX_WALK_DISTANCE_M = 1000


# find_bus_route_impl emits two option header styles:
#   - "N. Bus X → Terminus   (~M min total, direct, estimated)"
#   - "N. Bus X → Bus Y     (~M min total, 1 transfer, estimated)"
#   - "OPTION N — 2-TRANSFER — M min total"
# Match all three so the fallback path can re-wrap them with the
# LABEL_OPTION_DIRECT / _1T / _2T headers expected by §5.1.
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
    """Extract option blocks from a find_bus_route_impl response."""
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
                "kind": "DIRECT",
                "total_min": int(m.group(1)),
                "body_lines": [line],
            }
            continue
        m = _1T_RE.match(line)
        if m:
            if current is not None:
                options.append(current)
            current = {
                "kind": "1-TRANSFER",
                "total_min": int(m.group(1)),
                "body_lines": [line],
            }
            continue
        m = _2T_RE.match(line)
        if m:
            if current is not None:
                options.append(current)
            current = {
                "kind": "2-TRANSFER",
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
    return [ln for ln in text.splitlines() if ln.startswith("Note: ")]


def _place_display(
    name: str | None, lat: float, lng: float
) -> str:
    """User-supplied place name, or formatted coord per FR-7.6."""
    if name and name.strip():
        return name.strip()
    return f"({lat:.5f}, {lng:.5f})"


def _format_fallback_envelope(
    bus_text: str,
    origin_display: str,
    destination_display: str,
    footer_msg: str,
) -> str | None:
    """Wrap find_bus_route_impl output in a find_route envelope.

    Returns None when the underlying output contains no parsed options
    (e.g., it returned an ERR_* string or empty body). The caller uses
    this signal to switch to the `_TERMINAL` error path.
    """
    if not bus_text or bus_text.startswith("ERR_"):
        return None
    options = _parse_bus_options(bus_text)
    if not options:
        return None
    fastest = min(o["total_min"] for o in options)
    summary = (
        f"{len(options)} bus option{'s' if len(options) != 1 else ''}"
        f" · fastest {fastest} min"
    )
    out: list[str] = [
        header("find_route", f"{origin_display} → {destination_display}"),
        summary,
    ]
    for i, opt in enumerate(options, 1):
        out.append("")
        out.append(
            f"OPTION {i} — {opt['kind']} — {opt['total_min']} min total"
        )
        # Skip the first line (the original find_bus_route header for
        # this option) — we've replaced it with LABEL_OPTION_* above.
        out.extend(opt["body_lines"][1:])
    # Preserve MSG_BUS_ROUTE_ESTIMATES / MSG_TRUNCATED_2T / warm footer
    # that find_bus_route_impl emitted, then append the reason footer
    # that explains why the fallback fired.
    inherited_footers = _parse_footers(bus_text)
    if inherited_footers or footer_msg:
        out.append("")
    out.extend(inherited_footers)
    if footer_msg:
        out.append(footer(footer_msg))
    return "\n".join(out)


async def _fallback_or_terminal(
    lta: LTAClient,
    cache: MobilityCache,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    origin_display: str,
    destination_display: str,
    note_msg: str,
    terminal_err_id: str,
    terminal_err_msg: str,
) -> str:
    """Run find_bus_route_impl and dress its output as the fallback
    response, or collapse to a _TERMINAL error if the fallback also
    yields nothing."""
    try:
        bus_text = await find_bus_route_impl(
            lta, cache, from_lat, from_lng, to_lat, to_lng
        )
    except UpstreamError:
        bus_text = ""
    wrapped = _format_fallback_envelope(
        bus_text, origin_display, destination_display, note_msg
    )
    if wrapped is not None:
        return wrapped
    return error(terminal_err_id, terminal_err_msg)


async def find_route_impl(
    lta: LTAClient,
    cache: MobilityCache,
    onemap: OneMapClient,
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
    origin: str | None = None,
    destination: str | None = None,
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

    origin_display = _place_display(
        origin, from_latitude, from_longitude
    )
    destination_display = _place_display(
        destination, to_latitude, to_longitude
    )

    try:
        body = await onemap.route_pt(
            from_latitude,
            from_longitude,
            to_latitude,
            to_longitude,
            max_walk_distance_m=MAX_WALK_DISTANCE_M,
        )
    except OneMapRoutingServiceDown:
        return await _fallback_or_terminal(
            lta, cache,
            from_latitude, from_longitude,
            to_latitude, to_longitude,
            origin_display, destination_display,
            note_msg=MSG_ROUTING_SERVICE_DOWN,
            terminal_err_id=ERR_ROUTING_SERVICE_DOWN,
            terminal_err_msg=MSG_ROUTING_SERVICE_DOWN_TERMINAL,
        )
    except OneMapRoutingRateLimited:
        return await _fallback_or_terminal(
            lta, cache,
            from_latitude, from_longitude,
            to_latitude, to_longitude,
            origin_display, destination_display,
            note_msg=MSG_ROUTING_RATE_LIMITED,
            terminal_err_id=ERR_ROUTING_RATE_LIMITED,
            terminal_err_msg=MSG_ROUTING_RATE_LIMITED_TERMINAL,
        )
    except OneMapAuthFailed:
        return error(ERR_ONEMAP_AUTH_FAILED, MSG_ERR_ONEMAP_AUTH_FAILED)
    except (OneMapTimeout, OneMapSchemaDrift):
        return error(ERR_ONEMAP_TIMEOUT, MSG_ERR_ONEMAP_TIMEOUT)

    itineraries = parse_itineraries(body)
    if not itineraries:
        # FR-E.15: zero-itinerary 200 → fallback with ERR_NO_PT_ROUTE.
        # When fallback also yields nothing, the ERR_NO_PT_ROUTE prefix
        # becomes the full response (no body, per FR-7.4).
        wrapped = None
        try:
            bus_text = await find_bus_route_impl(
                lta, cache,
                from_latitude, from_longitude,
                to_latitude, to_longitude,
            )
        except UpstreamError:
            bus_text = ""
        wrapped = _format_fallback_envelope(
            bus_text,
            origin_display,
            destination_display,
            footer_msg="",
        )
        if wrapped is not None:
            return wrapped + "\n" + footer(MSG_ERR_NO_PT_ROUTE)
        return error(ERR_NO_PT_ROUTE, MSG_ERR_NO_PT_ROUTE)

    return format_envelope(itineraries, origin_display, destination_display)


def register_discovery_tools(
    mcp,
    lta: LTAClient,
    cache: MobilityCache,
    onemap: OneMapClient,
) -> None:
    @mcp.tool()
    async def find_route(
        from_latitude: float,
        from_longitude: float,
        to_latitude: float,
        to_longitude: float,
        origin: str | None = None,
        destination: str | None = None,
    ) -> str:
        """Plans the best public transport routes between two Singapore
        points using OneMap's multimodal routing. Returns up to 3
        time-ranked itineraries mixing walking, bus, and MRT/LRT, with
        per-leg duration, fare, and transfer count. Use whenever asked
        "how do I get from A to B?" — no need to chain bus or train
        tools. Falls back to bus-only if OneMap is unavailable.
        """
        return await find_route_impl(
            lta,
            cache,
            onemap,
            from_latitude,
            from_longitude,
            to_latitude,
            to_longitude,
            origin=origin,
            destination=destination,
        )
