"""OneMap location tools.

resolve_location — forward geocode a place name, address, or postal
    code to up to 3 candidate matches.
reverse_geocode   — given coordinates, return up to 3 nearby addresses.

Author: Jimmy Tong
"""

from __future__ import annotations

from api.errors import (
    OneMapAuthFailed,
    OneMapSchemaDrift,
    OneMapTimeout,
    UpstreamError,
)
from api.onemap import OneMapClient
from tools._format import (
    ERR_COORDINATES_OUT_OF_BOUNDS,
    ERR_LOCATION_NOT_FOUND,
    ERR_ONEMAP_AUTH_FAILED,
    ERR_ONEMAP_SCHEMA_DRIFT,
    ERR_ONEMAP_TIMEOUT,
    MSG_ERR_ONEMAP_AUTH_FAILED,
    MSG_ERR_ONEMAP_SCHEMA_DRIFT,
    MSG_ERR_ONEMAP_TIMEOUT,
    coords_in_sg,
    error,
    header,
    msg_err_coords_out_of_bounds,
)


def register_location_tools(mcp, onemap: OneMapClient) -> None:
    @mcp.tool()
    async def resolve_location(query: str) -> str:
        """Geocode a Singapore place name, address, or landmark to coordinates.

        Returns up to 3 matching results with name, full address, latitude,
        and longitude. Use this before any tool that requires latitude and
        longitude. Examples: 'Compass One MRT', '311 New Upper Changi Road',
        'Sengkang General Hospital'.
        """
        try:
            results = await onemap.search(query)
        except OneMapAuthFailed:
            return error(ERR_ONEMAP_AUTH_FAILED, MSG_ERR_ONEMAP_AUTH_FAILED)
        except OneMapTimeout:
            return error(ERR_ONEMAP_TIMEOUT, MSG_ERR_ONEMAP_TIMEOUT)
        except UpstreamError:
            return error(ERR_ONEMAP_TIMEOUT, MSG_ERR_ONEMAP_TIMEOUT)

        if not results:
            return error(
                ERR_LOCATION_NOT_FOUND,
                f'No matches for "{query}". Try a more specific name '
                "or include a road or postal code.",
            )

        out = [header("resolve_location", f'{len(results)} matches for "{query}"'), ""]
        for i, r in enumerate(results, 1):
            name = r.get("BUILDING") or r.get("SEARCHVAL") or "(no name)"
            address = r.get("ADDRESS") or "(no address)"
            lat = r.get("LATITUDE", "?")
            lng = r.get("LONGITUDE", "?")
            out.append(f"{i}. {name}")
            out.append(f"   {address}")
            out.append(f"   {lat}, {lng}")
            out.append("")
        return "\n".join(out).rstrip()

    @mcp.tool()
    async def reverse_geocode(latitude: float, longitude: float) -> str:
        """Find up to 3 Singapore addresses near a latitude/longitude pair.

        Returns building name, full address, and postal code for each
        nearby address. Use this when you have coordinates (e.g. from a
        map pin or GPS fix) and need a human-readable place. For the
        inverse direction (name → coordinates) use resolve_location.
        """
        if not coords_in_sg(latitude, longitude):
            return error(
                ERR_COORDINATES_OUT_OF_BOUNDS,
                msg_err_coords_out_of_bounds(latitude, longitude),
            )

        try:
            results = await onemap.reverse_geocode(latitude, longitude)
        except OneMapAuthFailed:
            return error(ERR_ONEMAP_AUTH_FAILED, MSG_ERR_ONEMAP_AUTH_FAILED)
        except OneMapTimeout:
            return error(ERR_ONEMAP_TIMEOUT, MSG_ERR_ONEMAP_TIMEOUT)
        except OneMapSchemaDrift:
            return error(ERR_ONEMAP_SCHEMA_DRIFT, MSG_ERR_ONEMAP_SCHEMA_DRIFT)
        except UpstreamError:
            return error(ERR_ONEMAP_TIMEOUT, MSG_ERR_ONEMAP_TIMEOUT)

        if not results:
            return error(
                ERR_LOCATION_NOT_FOUND,
                f"No addresses near {latitude:.5f}, {longitude:.5f} within "
                "OneMap's reverse-geocode radius.",
            )

        summary = (
            f"{len(results)} addresses near {latitude:.5f}, {longitude:.5f}"
        )
        out = [header("reverse_geocode", summary), ""]
        for i, r in enumerate(results, 1):
            building = (r.get("BUILDINGNAME") or "").strip()
            road = (r.get("ROAD") or "").strip()
            block = (r.get("BLOCK") or "").strip()
            postal = (r.get("POSTALCODE") or "").strip()

            name = building if building and building != "NIL" else road or "(no name)"
            address_parts: list[str] = []
            if block and block != "NIL":
                address_parts.append(block)
            if road and road != "NIL":
                address_parts.append(road)
            address = " ".join(address_parts) if address_parts else "(no address)"
            if postal and postal != "NIL":
                address = f"{address}, {postal}" if address != "(no address)" else postal

            out.append(f"{i}. {name}")
            out.append(f"   {address}")
            out.append("")
        return "\n".join(out).rstrip()
