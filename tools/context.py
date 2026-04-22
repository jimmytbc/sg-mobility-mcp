"""Location-context aggregation.

get_location_context — given Singapore coordinates and a radius,
returns the nearest bus stops, carparks (with available car lots),
MRT/LRT stations, and the current alert state for the lines among
those stations. Single-call alternative to chaining search_bus_stops
+ get_carpark_availability + get_train_alerts for "what's near X?"
queries.

The LINE STATUS section degrades to a one-liner if the LTA alerts
feed is slow or unreachable; the rest of the response still renders.

Author: Jimmy Tong
"""

from __future__ import annotations

import math
from typing import Any

from api.errors import (
    LTAAuthFailed,
    LTAEndpointNotFound,
    LTARateLimited,
    UpstreamError,
)
from api.lta import LTAClient
from cache import MobilityCache
from tools._format import (
    ERR_COORDINATES_OUT_OF_BOUNDS,
    ERR_LTA_AUTH_FAILED,
    ERR_LTA_ENDPOINT_NOT_FOUND,
    ERR_LTA_RATE_LIMITED,
    ERR_LTA_TIMEOUT,
    MSG_ERR_LTA_AUTH_FAILED,
    MSG_ERR_LTA_RATE_LIMITED,
    MSG_ERR_LTA_TIMEOUT,
    coords_in_sg,
    error,
    header,
    msg_err_coords_out_of_bounds,
    msg_err_lta_endpoint_not_found,
)

MAX_BUS_STOPS = 5
MAX_CARPARKS = 5
MAX_STATIONS = 3
RADIUS_CAP_M = 5000
EMPTY_SECTION_LINE = "   (none within radius)"


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


def _parse_carpark_location(loc: Any) -> tuple[float, float] | None:
    if not isinstance(loc, str):
        return None
    parts = loc.split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


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


def register_context_tools(
    mcp,
    lta: LTAClient,
    cache: MobilityCache,
    mrt_stations: list[dict],
) -> None:
    @mcp.tool()
    async def get_location_context(
        latitude: float,
        longitude: float,
        radius_m: int = 500,
    ) -> str:
        """Aggregate nearby Singapore transport infrastructure in one call.

        Returns up to 5 bus stops, 5 carparks with available car lots,
        3 MRT/LRT stations, and current alert status for their lines —
        all within radius_m of the coordinates. radius_m clamps at 5000m.

        Use after resolve_location when the user asks "what's near X?".
        Answers in one call what would otherwise chain search_bus_stops,
        get_carpark_availability, and get_train_alerts.
        """
        if not coords_in_sg(latitude, longitude):
            return error(
                ERR_COORDINATES_OUT_OF_BOUNDS,
                msg_err_coords_out_of_bounds(latitude, longitude),
            )

        effective_radius = min(max(int(radius_m), 0), RADIUS_CAP_M)

        # --- Bus stops (via warm cache) ---
        try:
            await cache.ensure_stops_warm(lta)
        except UpstreamError as exc:
            return _lta_error(exc)

        nearby_stops: list[tuple[float, dict]] = []
        for s in cache.bus_stops:
            try:
                slat = float(s["Latitude"])
                slng = float(s["Longitude"])
            except (KeyError, ValueError, TypeError):
                continue
            d = _haversine_m(latitude, longitude, slat, slng)
            if d <= effective_radius:
                nearby_stops.append((d, s))
        nearby_stops.sort(key=lambda x: x[0])
        nearby_stops = nearby_stops[:MAX_BUS_STOPS]

        # --- Carparks (live LTA feed) ---
        try:
            rows = await lta.get_carpark_availability()
        except UpstreamError as exc:
            return _lta_error(exc)

        nearby_carparks: list[tuple[float, dict]] = []
        for r in rows:
            if r.get("LotType") != "C":
                continue
            try:
                lots = int(r.get("AvailableLots", 0) or 0)
            except (TypeError, ValueError):
                continue
            if lots < 1:
                continue
            coords = _parse_carpark_location(r.get("Location", ""))
            if coords is None:
                continue
            d = _haversine_m(latitude, longitude, coords[0], coords[1])
            if d <= effective_radius:
                nearby_carparks.append((d, r))
        nearby_carparks.sort(key=lambda x: x[0])
        nearby_carparks = nearby_carparks[:MAX_CARPARKS]

        # --- MRT/LRT stations (from in-memory catalog) ---
        nearby_mrts: list[tuple[float, dict]] = []
        for st in mrt_stations:
            d = _haversine_m(
                latitude, longitude, st["latitude"], st["longitude"]
            )
            if d <= effective_radius:
                nearby_mrts.append((d, st))
        nearby_mrts.sort(key=lambda x: x[0])
        nearby_mrts = nearby_mrts[:MAX_STATIONS]

        # --- Empty-case shortcut (FR-4.7) ---
        if not nearby_stops and not nearby_carparks and not nearby_mrts:
            summary = (
                f"No transport infrastructure within {effective_radius}m of "
                f"{latitude:.5f}, {longitude:.5f}."
            )
            return (
                header("get_location_context", summary)
                + "\n\nTry a larger radius or verify coordinates are "
                "within Singapore."
            )

        # --- Area hint for header ---
        if nearby_mrts:
            area_hint = str(nearby_mrts[0][1]["name"])
        elif nearby_stops:
            area_hint = str(nearby_stops[0][1].get("Description", "")).title()
        else:
            area_hint = ""

        summary = (
            f"within {effective_radius}m of "
            f"{latitude:.5f}, {longitude:.5f}"
        )
        if area_hint:
            summary += f" ({area_hint} area)"
        out: list[str] = [header("get_location_context", summary), ""]

        # --- BUS STOPS section ---
        if nearby_stops:
            out.append(f"BUS STOPS ({len(nearby_stops)} nearest):")
            for d, s in nearby_stops:
                code = str(s.get("BusStopCode", "?"))
                desc = str(s.get("Description", ""))
                road = str(s.get("RoadName", ""))
                out.append(
                    f"   {code:<6s} {desc:<22s} {road:<22s} {int(d)}m"
                )
        else:
            out.append("BUS STOPS:")
            out.append(EMPTY_SECTION_LINE)
        out.append("")

        # --- CARPARKS section ---
        if nearby_carparks:
            out.append(
                f"CARPARKS ({len(nearby_carparks)} with lots available):"
            )
            for d, r in nearby_carparks:
                dev = str(r.get("Development", "") or "")
                lots = int(r.get("AvailableLots", 0) or 0)
                out.append(
                    f"   {dev:<33s} {lots:>4} lots   {int(d):>4}m"
                )
        else:
            out.append("CARPARKS:")
            out.append(EMPTY_SECTION_LINE)
        out.append("")

        # --- MRT/LRT STATIONS section ---
        if nearby_mrts:
            out.append(f"MRT/LRT STATIONS ({len(nearby_mrts)} nearby):")
            for d, st in nearby_mrts:
                name_codes = f"{st['name']} ({'/'.join(st['codes'])})"
                lines_str = ", ".join(st["lines"])
                out.append(
                    f"   {name_codes:<33s} {lines_str:<16s} {int(d):>4}m"
                )
        else:
            out.append("MRT/LRT STATIONS:")
            out.append(EMPTY_SECTION_LINE)

        # --- LINE STATUS section (only when stations are present) ---
        if nearby_mrts:
            out.append("")
            seen: set[str] = set()
            unique_lines: list[str] = []
            for _d, st in nearby_mrts:
                for line in st["lines"]:
                    if line not in seen:
                        seen.add(line)
                        unique_lines.append(line)

            out.append("LINE STATUS:")
            try:
                alerts = await lta.get_train_alerts()
            except UpstreamError:
                out.append("   unavailable (LTA alerts feed unreachable)")
            else:
                value = alerts.get("value", {}) or {}
                segments = value.get("AffectedSegments", []) or []
                messages = value.get("Message", []) or []
                global_msg = ""
                if messages and isinstance(messages[0], dict):
                    global_msg = (messages[0].get("Content", "") or "").strip()
                all_normal = value.get("Status") == 1
                for line in unique_lines:
                    if all_normal:
                        out.append(f"   {line:<7s}: Operating normally")
                        continue
                    line_segs = [
                        s for s in segments if s.get("Line") == line
                    ]
                    if not line_segs:
                        out.append(f"   {line:<7s}: Operating normally")
                        continue
                    stations_affected = str(
                        line_segs[0].get("Stations", "") or ""
                    ).strip()
                    if global_msg:
                        msg = global_msg
                    elif stations_affected:
                        msg = f"Affected: {stations_affected}"
                    else:
                        msg = "Service disruption"
                    out.append(f"   {line:<7s}: DISRUPTED — {msg}")

        return "\n".join(out).rstrip()
