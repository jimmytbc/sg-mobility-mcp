"""OneMap geocoding tool — place name, address, or postal code to coordinates.

Returns the top 3 matches so the LLM can disambiguate when a search
term is fuzzy (e.g. 'Tampines MRT' could mean Tampines, Tampines East,
or Tampines West).

Author: Jimmy Tong
"""

from api.onemap import OneMapClient


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
        except RuntimeError as e:
            return f"Could not resolve '{query}': {e}"
        if not results:
            return f"No results found for '{query}'."

        out = [f'Results for "{query}":', ""]
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
