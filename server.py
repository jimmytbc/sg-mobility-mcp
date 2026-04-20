"""sg-mobility — Singapore public-transport MCP server.

Entry point. Validates credentials up front, stands up the API clients
and the two bus caches, wires the six tools into FastMCP, and hands
control over to the stdio transport so Claude Desktop (or any other
MCP client) can talk to it.

Author: Jimmy Tong
"""

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
from tools.location import register_location_tools
from tools.routing import register_routing_tools
from tools.train import register_train_tools

load_dotenv(Path(__file__).parent / ".env")

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

mcp = FastMCP("sg-mobility")
lta = LTAClient(LTA_ACCOUNT_KEY)  # type: ignore[arg-type]
onemap = OneMapClient(ONEMAP_EMAIL, ONEMAP_PASSWORD)  # type: ignore[arg-type]
cache = MobilityCache()

register_location_tools(mcp, onemap)
register_bus_tools(mcp, lta, cache)
register_train_tools(mcp, lta)
register_carpark_tools(mcp, lta)
register_routing_tools(mcp, lta, cache)


if __name__ == "__main__":
    print("sg-mobility: starting on stdio transport", file=sys.stderr)
    mcp.run(transport="stdio")
