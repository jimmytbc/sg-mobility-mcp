"""sg-mobility-mcp — Singapore public-transport MCP server.

Entry point. Validates credentials up front, stands up the API clients
and the two bus caches, loads the static MRT station catalog, wires
the tools into FastMCP, and hands control over to the stdio transport
so Claude Desktop (or any other MCP client) can talk to it.

Author: Jimmy Tong
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from api.lta import LTAClient
from api.onemap import OneMapClient
from cache import MobilityCache
from tools.bus import register_bus_tools
from tools.carpark import register_carpark_tools
from tools.context import register_context_tools
from tools.location import register_location_tools
from tools.routing import register_routing_tools
from tools.train import register_train_tools

load_dotenv(Path(__file__).parent / ".env")


def _load_mrt_stations(path: Path) -> list[dict]:
    """Load and minimally validate data/mrt_stations.json at startup.

    Fail-fast per specs/03-functional-requirements.md FR-4.6: a missing
    or malformed file MUST surface before the server accepts tool
    calls. Full schema validation lives in probes/phase-2-probe.py.
    """
    if not path.exists():
        raise RuntimeError(
            f"Required data file missing: {path}. "
            "See data/README.md for the schema and update process."
        )
    try:
        stations = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} did not parse as JSON: {exc}") from exc
    if not isinstance(stations, list) or not stations:
        raise RuntimeError(
            f"{path} must be a non-empty JSON array of station entries."
        )
    required = ("name", "codes", "lines", "latitude", "longitude")
    for idx, entry in enumerate(stations):
        if not isinstance(entry, dict) or any(k not in entry for k in required):
            raise RuntimeError(
                f"{path} entry {idx} is malformed; missing one of {required}."
            )
    return stations

LTA_ACCOUNT_KEY = os.environ.get("LTA_ACCOUNT_KEY")
ONEMAP_EMAIL = os.environ.get("ONEMAP_EMAIL")
ONEMAP_PASSWORD = os.environ.get("ONEMAP_PASSWORD")

_missing = [
    name
    for name, value in [
        ("LTA_ACCOUNT_KEY", LTA_ACCOUNT_KEY),
        ("ONEMAP_EMAIL", ONEMAP_EMAIL),
        ("ONEMAP_PASSWORD", ONEMAP_PASSWORD),
    ]
    if not value
]
if _missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(_missing)}. "
        f"See .env.example and README.md for setup."
    )

mcp = FastMCP("sg-mobility-mcp")
lta = LTAClient(LTA_ACCOUNT_KEY)  # type: ignore[arg-type]
onemap = OneMapClient(ONEMAP_EMAIL, ONEMAP_PASSWORD)  # type: ignore[arg-type]
cache = MobilityCache()
mrt_stations = _load_mrt_stations(
    Path(__file__).parent / "data" / "mrt_stations.json"
)

register_location_tools(mcp, onemap)
register_bus_tools(mcp, lta, cache)
register_train_tools(mcp, lta)
register_carpark_tools(mcp, lta)
register_routing_tools(mcp, lta, cache)
register_context_tools(mcp, lta, cache, mrt_stations)


if __name__ == "__main__":
    print("sg-mobility-mcp: starting on stdio transport", file=sys.stderr)
    mcp.run(transport="stdio")
