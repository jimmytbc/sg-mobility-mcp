"""Output envelope helpers and spec-sourced string IDs.

Every tool renders its output through header() / error() / footer() so
that schema evolution is a single-file change rather than a package
sweep. Per specs/00-rules.md R4, every user-facing string lives in
specs/05-ui.md §5.4; texts here are copied from the spec and
referenced by ID.
"""

from __future__ import annotations


def header(tool_name: str, summary: str) -> str:
    return f"{tool_name} — {summary}"


def error(string_id: str, message: str) -> str:
    return f"{string_id}: {message}"


def footer(note: str) -> str:
    return f"Note: {note}"


# ---------------------------------------------------------------------------
# Error string IDs (texts live in specs/05-ui.md §5.4)
# ---------------------------------------------------------------------------

ERR_ONEMAP_AUTH_FAILED = "ERR_ONEMAP_AUTH_FAILED"
ERR_ONEMAP_TIMEOUT = "ERR_ONEMAP_TIMEOUT"
ERR_ONEMAP_SCHEMA_DRIFT = "ERR_ONEMAP_SCHEMA_DRIFT"
ERR_LTA_AUTH_FAILED = "ERR_LTA_AUTH_FAILED"
ERR_LTA_TIMEOUT = "ERR_LTA_TIMEOUT"
ERR_LTA_RATE_LIMITED = "ERR_LTA_RATE_LIMITED"
ERR_LTA_ENDPOINT_NOT_FOUND = "ERR_LTA_ENDPOINT_NOT_FOUND"
ERR_COORDINATES_OUT_OF_BOUNDS = "ERR_COORDINATES_OUT_OF_BOUNDS"
ERR_BUS_STOP_NOT_FOUND = "ERR_BUS_STOP_NOT_FOUND"
ERR_LOCATION_NOT_FOUND = "ERR_LOCATION_NOT_FOUND"
ERR_INVALID_LINE_CODE = "ERR_INVALID_LINE_CODE"
ERR_NO_BUS_ROUTE = "ERR_NO_BUS_ROUTE"

MSG_ERR_ONEMAP_AUTH_FAILED = (
    "OneMap rejected the configured credentials. Verify ONEMAP_EMAIL "
    "and ONEMAP_PASSWORD and restart the server."
)
MSG_ERR_ONEMAP_TIMEOUT = (
    "OneMap did not respond within 10 seconds. The service may be "
    "slow or down — try again shortly."
)
MSG_ERR_ONEMAP_SCHEMA_DRIFT = (
    "OneMap returned a response in an unexpected shape. The endpoint "
    "may have changed; the server needs an update."
)
MSG_ERR_LTA_AUTH_FAILED = (
    "LTA DataMall rejected the configured AccountKey. Verify "
    "LTA_ACCOUNT_KEY and restart the server."
)
MSG_ERR_LTA_TIMEOUT = (
    "LTA DataMall did not respond within 10 seconds. The service may "
    "be slow or down — try again shortly."
)
MSG_ERR_LTA_RATE_LIMITED = (
    "LTA DataMall is rate-limiting requests; retry in 60 seconds. "
    "The server has already retried with backoff."
)
VALID_LINE_CODES = ("NSL", "EWL", "CCL", "DTL", "TEL", "NEL", "BPLRT", "SKLRT", "PGLRT")


def msg_err_lta_endpoint_not_found(path: str) -> str:
    return (
        f"LTA DataMall returned 404 for {path}. The endpoint URL may "
        "have changed; the server needs an update."
    )


def msg_err_coords_out_of_bounds(lat: float, lng: float) -> str:
    return (
        f"({lat:.5f}, {lng:.5f}) is outside Singapore (lat 1.15-1.50, "
        "lng 103.55-104.10). Verify coordinates."
    )


def msg_err_bus_stop_not_found(code: str) -> str:
    return f"No bus stop with code {code}. Use search_bus_stops to find a valid code."


def msg_err_invalid_line_code(code: str) -> str:
    return (
        f"{code} is not a valid line. Valid codes: "
        + ", ".join(VALID_LINE_CODES)
        + "."
    )


def msg_err_no_bus_route(
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    max_walk_m: int,
    max_transfer_walk_m: int,
    max_total_min: int,
) -> str:
    return (
        f"No bus route found from ({from_lat:.5f}, {from_lng:.5f}) to "
        f"({to_lat:.5f}, {to_lng:.5f}) within walk {max_walk_m}m / "
        f"transfer-walk {max_transfer_walk_m}m / total {max_total_min} "
        "min. Consider MRT or widen walk radii."
    )


# ---------------------------------------------------------------------------
# Informational footer texts (rendered via footer())
# ---------------------------------------------------------------------------

MSG_FIRST_CALL_WARM = "first-call cache warm; subsequent calls will be faster."
MSG_BUS_ROUTE_ESTIMATES = (
    "in-vehicle time estimated at 1.8 min/stop; transfer wait fixed at 10 min."
)
MSG_CARPARK_FEED = "live LTA feed; not all private carparks included."
MSG_BUS_ARRIVALS_LIVE = "load and accessibility from LTA live feed."


# ---------------------------------------------------------------------------
# Singapore coordinate envelope (FR-2.4 / FR-E.8)
# ---------------------------------------------------------------------------

SG_LAT_MIN, SG_LAT_MAX = 1.15, 1.50
SG_LNG_MIN, SG_LNG_MAX = 103.55, 104.10


def coords_in_sg(lat: float, lng: float) -> bool:
    return (SG_LAT_MIN <= lat <= SG_LAT_MAX) and (SG_LNG_MIN <= lng <= SG_LNG_MAX)
