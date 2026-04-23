"""OneMap PT routing response parser and A1 envelope formatter.

Thin layer between `api/onemap.py::route_pt` and `tools/discovery.py`'s
`find_route_impl`. Per FR-7.1 this module does not compute routes,
score itineraries, or estimate per-leg durations — it reshapes what
OneMap returns into the A1 envelope format defined in
`specs/05-ui.md` §5.1.

Author: Jimmy Tong
"""

from __future__ import annotations

from dataclasses import dataclass

from tools._format import (
    MSG_ERR_NO_PT_ROUTE,
    MSG_WALK_LIMIT_EXCEEDED,
    footer,
    header,
    label_itinerary,
)

# Only these OneMap mode strings are expected. RAIL is intentionally
# not listed — OneMap PT emits SUBWAY for MRT/LRT per probe shape
# verification (prompts/phase-5.md CONSTRAINTS).
MODE_WALK = "WALK"
MODE_BUS = "BUS"
MODE_SUBWAY = "SUBWAY"


@dataclass
class Leg:
    mode: str
    duration_s: int
    distance_m: float
    from_name: str
    to_name: str
    route_short_name: str  # bus service number or MRT line code; "" for WALK
    intermediate_count: int
    first_intermediate: str  # "" when none
    last_intermediate: str  # "" when none


@dataclass
class Itinerary:
    duration_s: int
    fare: str  # OneMap-supplied decimal string, e.g. "2.07"
    transfers: int
    walk_limit_exceeded: bool
    legs: list[Leg]


def parse_itineraries(body: dict) -> list[Itinerary]:
    """Pull the itinerary list out of a OneMap PT routing response.

    Tolerates missing optional fields (falls back to "" / 0). Raises
    nothing — the caller decides what to do with an empty list.
    """
    plan = body.get("plan")
    if not isinstance(plan, dict):
        return []
    raw_itineraries = plan.get("itineraries")
    if not isinstance(raw_itineraries, list):
        return []
    out: list[Itinerary] = []
    for raw in raw_itineraries:
        if not isinstance(raw, dict):
            continue
        legs = _parse_legs(raw.get("legs") or [])
        if not legs:
            continue
        out.append(
            Itinerary(
                duration_s=int(raw.get("duration") or 0),
                fare=str(raw.get("fare") or "0.00"),
                transfers=int(raw.get("transfers") or 0),
                walk_limit_exceeded=bool(raw.get("walkLimitExceeded")),
                legs=legs,
            )
        )
    return out


def _parse_legs(raw_legs: list[dict]) -> list[Leg]:
    out: list[Leg] = []
    for raw in raw_legs:
        if not isinstance(raw, dict):
            continue
        mode = str(raw.get("mode") or "").upper()
        from_end = raw.get("from") or {}
        to_end = raw.get("to") or {}
        intermediates = raw.get("intermediateStops") or []
        if not isinstance(intermediates, list):
            intermediates = []
        first_name = ""
        last_name = ""
        if intermediates:
            first_name = str(
                (intermediates[0] or {}).get("name") or ""
            )
            last_name = str(
                (intermediates[-1] or {}).get("name") or ""
            )
        # routeShortName is the agent-facing identifier (bus service
        # number, line code). OneMap also carries `route`; prefer
        # routeShortName per probe verification, fall back to route.
        ident = str(
            raw.get("routeShortName") or raw.get("route") or ""
        )
        out.append(
            Leg(
                mode=mode,
                duration_s=int(raw.get("duration") or 0),
                distance_m=float(raw.get("distance") or 0.0),
                from_name=str(from_end.get("name") or ""),
                to_name=str(to_end.get("name") or ""),
                route_short_name=ident,
                intermediate_count=len(intermediates),
                first_intermediate=first_name,
                last_intermediate=last_name,
            )
        )
    return out


def _fmt_min(secs: int) -> int:
    """Seconds to minutes, rounded to nearest, clamped at >=1.

    §5.2 says "for sub-minute, omit" but the §5.1 mock-ups show "1 min"
    for a 38s walk leg; clamping to 1 min avoids emitting "0 min" or
    having to collapse the duration column entirely.
    """
    m = round(secs / 60)
    return max(1, m)


def _fmt_distance_m(distance_m: float) -> str:
    """Match the §5.1 Phase 5 mock-up: '(40 m)', '(180 m)', '(1.4 km)'.

    The space before the unit follows the mock-up literally — note that
    §5.2 tabulates distance as '450m' (no space) but §5.1 is the source
    of truth for output structure per the file's own header.
    """
    if distance_m < 1000:
        return f"({int(round(distance_m))} m)"
    return f"({distance_m / 1000:.1f} km)"


# Column layout for per-leg lines. mode and duration are left-padded to
# their respective widths so identifier and from→to start at fixed
# offsets. The mock-up uses plain whitespace — no tabs.
_MODE_W = 6
_DUR_W = 6
_IDENT_W = 8
_LEG_INDENT = "  "
# Position where the from→to clause starts, used for aligning the
# intermediate-stop summary line underneath the preceding transit leg.
_INTERMEDIATE_INDENT = " " * (
    len(_LEG_INDENT) + _MODE_W + 2 + _DUR_W + 3 + _IDENT_W + 1
)


def _format_leg(
    leg: Leg, origin_display: str, destination_display: str
) -> list[str]:
    dur = f"{_fmt_min(leg.duration_s)} min"
    if leg.mode == MODE_WALK:
        ident = _fmt_distance_m(leg.distance_m)
    else:
        ident = leg.route_short_name
    # OneMap labels the synthetic start / end nodes of a trip as
    # literal "Origin" / "Destination". §5.5 Phase 5 parseability rule
    # says those MUST NOT appear in the envelope — substitute the
    # user's display strings instead.
    from_name = (
        origin_display if leg.from_name == "Origin" else leg.from_name
    )
    to_name = (
        destination_display if leg.to_name == "Destination" else leg.to_name
    )
    main = (
        f"{_LEG_INDENT}{leg.mode:<{_MODE_W}}  "
        f"{dur:<{_DUR_W}}   {ident:<{_IDENT_W}} "
        f"{from_name} → {to_name}"
    )
    lines = [main]
    # Lean intermediate-stop policy per FR-7.3: count + first + last
    # names only. Omit entirely when the leg has 0 intermediates.
    if leg.mode != MODE_WALK and leg.intermediate_count > 0:
        lines.append(
            f"{_INTERMEDIATE_INDENT}"
            f"({leg.intermediate_count} stops between "
            f"{leg.first_intermediate} and {leg.last_intermediate})"
        )
    return lines


def _summary_line(itineraries: list[Itinerary]) -> str:
    count = len(itineraries)
    noun = "itinerary" if count == 1 else "itineraries"
    fastest_s = min(it.duration_s for it in itineraries)
    cheapest = min((_fare_as_float(it.fare) for it in itineraries))
    return (
        f"{count} {noun} · fastest {_fmt_min(fastest_s)} min "
        f"· cheapest ${cheapest:.2f}"
    )


def _fare_as_float(fare: str) -> float:
    try:
        return float(fare)
    except ValueError:
        return 0.0


def format_envelope(
    itineraries: list[Itinerary],
    origin_display: str,
    destination_display: str,
) -> str:
    """Render a non-empty itinerary list as the Phase 5 A1 envelope.

    Precondition: `itineraries` is non-empty. Zero-itinerary and
    failure paths are handled in the caller.
    """
    if not itineraries:
        # Defensive: the contract is "non-empty", but callers that wire
        # this up wrong shouldn't produce a malformed envelope.
        return (
            header(
                "find_route",
                f"{origin_display} → {destination_display}",
            )
            + "\n\n"
            + footer(MSG_ERR_NO_PT_ROUTE)
        )
    lines: list[str] = [
        header(
            "find_route",
            f"{origin_display} → {destination_display}",
        ),
        _summary_line(itineraries),
    ]
    any_walk_limit = False
    for i, it in enumerate(itineraries, 1):
        lines.append("")
        lines.append(
            label_itinerary(
                i, _fmt_min(it.duration_s), it.fare, it.transfers
            )
        )
        for leg in it.legs:
            lines.extend(
                _format_leg(leg, origin_display, destination_display)
            )
        if it.walk_limit_exceeded:
            any_walk_limit = True
    if any_walk_limit:
        lines.append("")
        lines.append(footer(MSG_WALK_LIMIT_EXCEEDED))
    return "\n".join(lines)
