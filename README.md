# sg-mobility-mcp

**A grounded Singapore travel-planning brain for your AI personal assistant.**

sg-mobility-mcp is a Model Context Protocol (MCP) server that turns any
MCP-compatible agent — Claude Desktop, Claude Code, LangGraph pipelines,
custom Claude Agent SDK assistants, Cursor, Cline, and more — into an
assistant that can actually plan a day around Singapore using live
transport data, not guesses.

Plug it into your agent and say things like:

> _"I have the dentist at Tampines at 10, lunch in Macpherson at 12, a
> hospital visit at Napier Road at 2:30, and a class in Marine Parade at
> 4. Plan my public-transport day."_

The agent chains the nine tools (geocoding → reverse geocoding →
stop search → live arrivals → direct / 1-transfer / 2-transfer bus
ranking → disruption check → carpark lookup → one-shot location
context → unified "best route A to B" discovery) and returns a real
itinerary: which bus from which stop, live ETAs, correct service
numbers, destination terminals so it picks the right direction, and
walking distances sanity-checked against actual coordinates. No more
hallucinated bus 58 to "Tampines MRT" when bus 58 actually terminates
at Bishan.

**v0.2 additions over v0.1.0:** standardized output envelope across
all tools, `reverse_geocode` (coordinates → addresses),
`get_location_context` (one-shot "what's near here?"), 2-transfer bus
routing with cost-promising enumeration, `find_route` (unified bus +
walking + MRT-hint dispatcher), LTA 429 backoff, and cache concurrency
locks. See [`CHANGELOG.md`](CHANGELOG.md) for the full cycle summary.

### Why it exists

General-purpose LLMs are confident-but-wrong about Singapore transit:
they'll invent bus numbers, get directions reversed, or miss a nearby
stop entirely. This server hands the agent deterministic, live data from
two official sources so the agent can reason, but not fabricate:

- **[LTA DataMall](https://datamall.lta.gov.sg/)** — live bus arrivals,
  bus routes, MRT/LRT alerts, and carpark availability.
- **[OneMap](https://www.onemap.gov.sg/)** — Singapore government
  geocoding (turn a place name or address into coordinates).

The sweet spot is **agentic, multi-step travel planning** — where a
single user request triggers a chain of tool calls across an entire day
or trip. You can absolutely use it in an interactive chat too ("when's
the next bus at VivoCity?"), but the real leverage is letting a personal
assistant orchestrate it end to end on your behalf.

---

## Table of contents

1. [At a glance](#at-a-glance)
2. [How it works in Claude Desktop](#how-it-works-in-claude-desktop)
3. [Prerequisites](#prerequisites)
4. [Getting your API keys](#getting-your-api-keys)
5. [Installation](#installation)
6. [Configuration](#configuration)
7. [Running standalone](#running-standalone)
8. [Connecting to Claude Desktop](#connecting-to-claude-desktop)
9. [Using from other MCP clients](#using-from-other-mcp-clients)
10. [Tool reference](#tool-reference)
11. [Use cases](#use-cases)
12. [Troubleshooting](#troubleshooting)
13. [Architecture](#architecture)
14. [Limitations](#limitations)
15. [Security](#security)
16. [Project layout](#project-layout)
17. [Updating and maintenance](#updating-and-maintenance)
18. [License](#license)

---

## At a glance

| | |
|---|---|
| **Language** | Python 3.10+ |
| **MCP SDK** | `mcp[cli]` |
| **HTTP client** | `httpx` (async) |
| **Transport** | stdio (default for Claude Desktop) |
| **Data sources** | LTA DataMall, OneMap, bundled MRT/LRT station catalog |
| **Runtime deps** | 4 packages (`mcp[cli]`, `httpx`, `pydantic`, `python-dotenv`) |

**Nine tools registered:**

| Tool | What it does |
|---|---|
| `resolve_location` | Geocode a place name / address / landmark → coordinates |
| `reverse_geocode` | Coordinates → up to 3 nearby building names and full addresses |
| `search_bus_stops` | Find stops by name, road, or proximity to coordinates |
| `get_bus_arrivals` | Live bus ETAs, load, type, accessibility, **destination terminal** |
| `find_bus_route` | Ranked **direct, 1-transfer, or 2-transfer bus** journeys between two coordinates (walk + wait + ride + transfer + ride + walk), scored server-side |
| `get_train_alerts` | MRT/LRT service disruptions, optionally filtered by line |
| `get_carpark_availability` | Real-time carpark lots across HDB, URA, LTA |
| `get_location_context` | One-shot summary of nearby bus stops, carparks, MRT/LRT stations, and line status for a coordinate |
| `find_route` | Unified "best route A → B" dispatcher: ranks bus options (via `find_bus_route`) + walking estimate + MRT-suggestion hint in a single call |

---

## How it works in Claude Desktop

You ask Claude a question in natural language. If the question touches
Singapore transport, Claude picks the right tool(s) and chains them:

```
You:    When's the next bus 10 at VivoCity?

Claude  → resolve_location("VivoCity")
         ← VIVO CITY, 1 HarbourFront Walk, 1.26420, 103.82220
        → search_bus_stops(latitude=1.2642, longitude=103.8222)
         ← Top stops: 14131 (VivoCity), 14141 (Opp VivoCity), ...
        → get_bus_arrivals(bus_stop_code="14131", service_no="10")
         ← Service 10 (SBST) → Tanah Merah Int
             Next : 4 min (GPS)  — Seats available · Double deck ♿
             2nd  : 11 min (GPS) — Standing · Single deck
```

The server does the fetching and formatting; Claude does the conversation
and reasoning.

---

## Prerequisites

You need all five:

1. **Python 3.10 or later** (check with `python3 --version`)
2. **pip** (bundled with Python) or [`uv`](https://docs.astral.sh/uv/)
3. **Claude Desktop** installed ([claude.ai/download](https://claude.ai/download))
4. **LTA DataMall AccountKey** — free, takes 1–2 business days (see below)
5. **OneMap account** — free, immediate (see below)

---

## Getting your API keys

### LTA DataMall (1–2 business days)

1. Go to the registration page:
   **<https://datamall.lta.gov.sg/content/datamall/en/request-for-api.html>**
2. Fill in the form (name, email, organisation, intended use). "Personal
   project" is an acceptable purpose.
3. Wait for an approval email from LTA — usually within 1–2 business days.
4. The email contains your **AccountKey** (a ~32-character string). Copy it.
5. Treat it like a password. **Never share it or commit it to git.**

### OneMap (immediate)

1. Go to the OneMap developer portal:
   **<https://www.onemap.gov.sg/apidocs/>**
2. Click **Register**. Provide an email and choose a password.
3. Verify your email.
4. Your **email + password** are your credentials. This server generates and
   automatically refreshes the JWT access token in the background — you do
   **not** manage tokens manually.

> You do **not** need a OneMap API key separate from your login. The server
> authenticates with email + password via OneMap's `/auth/post/getToken`
> endpoint and caches the token until near expiry.

---

## Installation

```bash
git clone https://github.com/jimmytbc/sg-mobility-mcp.git
cd sg-mobility-mcp

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Copy the environment template and fill in your three credentials:

```bash
cp .env.example .env
```

Open `.env` in your editor and replace the placeholders:

```
LTA_ACCOUNT_KEY=your_actual_lta_key_here
ONEMAP_EMAIL=your_onemap_email@example.com
ONEMAP_PASSWORD=your_onemap_password
```

`.env` is gitignored by default. **Never remove the gitignore rule or commit
this file.**

### Alternative: inject via Claude Desktop's `env` block

If you prefer not to keep a `.env` file at all, skip the step above and
supply the three variables directly in the Claude Desktop config `env` block
(covered in [Connecting to Claude Desktop](#connecting-to-claude-desktop)).

---

## Running standalone

Before wiring into Claude Desktop, verify the server starts cleanly:

```bash
python server.py
```

Expected output on stderr:

```
sg-mobility-mcp: starting on stdio transport
```

The process then waits for MCP stdio input — this is normal. Press `Ctrl-C`
to exit. If any env var is missing, you'll see a clear error listing exactly
which variables are unset:

```
RuntimeError: Missing required environment variables: ONEMAP_PASSWORD.
See .env.example and README.md for setup.
```

---

## Connecting to Claude Desktop

### 1. Find your config file

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

If the file doesn't exist, create it with `{"mcpServers": {}}`.

### 2. Add this entry under `mcpServers`

Replace `<path/to/sg-mobility-mcp>` with the absolute path to your clone.

```json
{
  "mcpServers": {
    "sg-mobility-mcp": {
      "command": "<path/to/sg-mobility-mcp>/.venv/bin/python",
      "args": ["<path/to/sg-mobility-mcp>/server.py"]
    }
  }
}
```

> On **Windows**, use `\\` separators and `.venv\\Scripts\\python.exe`.

If you have other MCP servers, merge the `sg-mobility-mcp` entry into your
existing `mcpServers` object — do not overwrite.

### 3. Point at the venv Python

Using the venv's Python (`.venv/bin/python`) ensures Claude Desktop finds the
installed `mcp` and `httpx` packages. Using your system `python` will almost
certainly fail.

### 4. Optionally pass credentials via env

If you're skipping `.env`, add an `env` block:

```json
"sg-mobility-mcp": {
  "command": "<path>/.venv/bin/python",
  "args": ["<path>/server.py"],
  "env": {
    "LTA_ACCOUNT_KEY": "your_key",
    "ONEMAP_EMAIL": "you@example.com",
    "ONEMAP_PASSWORD": "your_password"
  }
}
```

Values in `env` take precedence over `.env` file values.

### 5. Restart Claude Desktop

Fully **quit** (⌘Q on macOS, or right-click tray icon → Quit on Windows),
then reopen. MCP server tool schemas are cached at startup — just closing
the window does not reload them.

### 6. Verify

In a new chat, ask:

> Are there any MRT disruptions right now?

Claude should call `get_train_alerts` and reply with the live status. If
nothing fires, see [Troubleshooting](#troubleshooting).

---

## Using from other MCP clients

The server speaks stdio MCP — the same protocol Claude Desktop uses — so
any MCP-compatible client can drive it. Three common ways below.

### From Claude Code (CLI)

[Claude Code](https://claude.com/claude-code) is Anthropic's CLI agent.
Register the server once, then every `claude` session in the project (or
across projects, depending on scope) has all eight tools available.

**Option A — `.mcp.json` at your project root** (shareable with a team,
safe to commit as long as you leave the env values as placeholders):

```json
{
  "mcpServers": {
    "sg-mobility-mcp": {
      "type": "stdio",
      "command": "<path/to/sg-mobility-mcp>/.venv/bin/python",
      "args": ["<path/to/sg-mobility-mcp>/server.py"],
      "env": {
        "LTA_ACCOUNT_KEY": "your_key",
        "ONEMAP_EMAIL": "you@example.com",
        "ONEMAP_PASSWORD": "your_password"
      }
    }
  }
}
```

**Option B — register via CLI** (scoped to the current project):

```bash
claude mcp add \
  --transport stdio \
  --scope project \
  --env LTA_ACCOUNT_KEY=your_key \
  --env ONEMAP_EMAIL=you@example.com \
  --env ONEMAP_PASSWORD=your_password \
  sg-mobility-mcp \
  -- <path/to/sg-mobility-mcp>/.venv/bin/python <path/to/sg-mobility-mcp>/server.py
```

Scope options:
- `--scope local` (default) — you only, current project, stored in `~/.claude.json`
- `--scope project` — shared via `.mcp.json` (checked into git)
- `--scope user` — you only, all projects

**Verifying and using tools in a session:**

```bash
claude       # start a session in the project
> /mcp       # list registered MCP servers and their status
> Are there any MRT disruptions right now?
```

Claude Code discovers the tools automatically — you don't need to name
them in the prompt. If it doesn't fire a tool, see [Troubleshooting](#troubleshooting).

### From LangGraph (Python)

Via [`langchain-mcp-adapters`](https://pypi.org/project/langchain-mcp-adapters/),
which bridges stdio MCP servers into LangChain-compatible tools.

**Install:**

```bash
pip install "langchain-mcp-adapters>=0.2" langgraph "langchain[openai]"
```

**Minimal example** (`run_agent.py`):

```python
import asyncio
import os

from langchain.chat_models import init_chat_model
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

SERVER_PY = "/absolute/path/to/sg-mobility-mcp/server.py"
VENV_PY   = "/absolute/path/to/sg-mobility-mcp/.venv/bin/python"


async def main():
    client = MultiServerMCPClient({
        "sg_mobility_mcp": {
            "transport": "stdio",
            "command": VENV_PY,
            "args": [SERVER_PY],
            "env": {
                "LTA_ACCOUNT_KEY": os.environ["LTA_ACCOUNT_KEY"],
                "ONEMAP_EMAIL":    os.environ["ONEMAP_EMAIL"],
                "ONEMAP_PASSWORD": os.environ["ONEMAP_PASSWORD"],
            },
        }
    })

    tools = await client.get_tools()

    # Swap for whatever provider you have keys for, e.g.
    # "anthropic:claude-sonnet-4-5" (requires `pip install "langchain[anthropic]"`)
    model = init_chat_model("openai:gpt-4.1")

    agent = create_react_agent(model, tools)

    result = await agent.ainvoke(
        {"messages": "Are there any MRT disruptions in Singapore right now?"}
    )
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
```

Run it with your chosen LLM provider's key plus the three sg-mobility-mcp
env vars exported in the shell, or loaded from a `.env`.

> If `get_tools()` behaves as if env vars aren't reaching the subprocess,
> fall back to exporting them in the parent shell before
> `MultiServerMCPClient(...)` is constructed — they'll be inherited.

### From the Claude Agent SDK (Python)

Anthropic's [Claude Agent SDK](https://docs.claude.com/en/docs/agent-sdk)
has first-class support for stdio MCP servers via `ClaudeAgentOptions.mcp_servers`.

**Install:**

```bash
pip install claude-agent-sdk
export ANTHROPIC_API_KEY=sk-ant-...
```

**Minimal example** (`run_agent_sdk.py`):

```python
import os

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

SERVER_PY = "/absolute/path/to/sg-mobility-mcp/server.py"
VENV_PY   = "/absolute/path/to/sg-mobility-mcp/.venv/bin/python"

options = ClaudeAgentOptions(
    model="opus",  # alias — resolves to the latest Opus. Or pass a full ID.
    mcp_servers={
        "sg_mobility_mcp": {
            "type": "stdio",
            "command": VENV_PY,
            "args": [SERVER_PY],
            "env": {
                "LTA_ACCOUNT_KEY": os.environ["LTA_ACCOUNT_KEY"],
                "ONEMAP_EMAIL":    os.environ["ONEMAP_EMAIL"],
                "ONEMAP_PASSWORD": os.environ["ONEMAP_PASSWORD"],
            },
        }
    },
    # Auto-approve every tool from this server (prefix = "mcp__<server_key>")
    allowed_tools=["mcp__sg_mobility_mcp"],
)


async def main():
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Are there any MRT disruptions in Singapore right now?"
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(block.text)


anyio.run(main)
```

The `async with` block manages the subprocess — the stdio MCP server is
spawned on entry and terminated cleanly on exit.

**Model IDs**: the SDK accepts short aliases (`"opus"`, `"sonnet"`,
`"haiku"`) or full published IDs like `"claude-opus-4-5"`. If you want a
specific version, check
[docs.claude.com](https://docs.claude.com/en/docs/about-claude/models)
for the current published ID before hardcoding it.

**Auth**: standard `ANTHROPIC_API_KEY` env var. AWS Bedrock and GCP Vertex
credentials are also honored if configured at the system level.

For the one-shot, non-interactive case, the SDK also exposes a simpler
`query(prompt, options=...)` function that returns an async iterator of
messages — see the SDK docs for details.

### Any other MCP client

Cursor, Continue, Cline, Zed AI, and other MCP hosts all accept the same
shape:

| Field | Value |
|---|---|
| transport | `stdio` |
| command | `<path>/.venv/bin/python` |
| args | `["<path>/server.py"]` |
| env | `LTA_ACCOUNT_KEY`, `ONEMAP_EMAIL`, `ONEMAP_PASSWORD` |

Consult each client's MCP-config docs for the exact file location and
schema — the values are identical.

---

## Tool reference

Each tool returns a pre-formatted string optimised for LLM consumption.

### `resolve_location(query: str) -> str`

Geocode a Singapore place name, address, or landmark via OneMap.

**Example prompt to Claude:** _"Where is Gleneagles Hospital?"_

**Returns** up to 3 matching results with building name, full address, and
coordinates. If nothing matches, returns a clear "no results" message.

---

### `reverse_geocode(latitude: float, longitude: float) -> str`

The inverse of `resolve_location`: given coordinates (e.g. from a map
pin, a GPS fix, or another tool's output), return nearby named
buildings and addresses.

**Example prompts:**
- _"What's at 1.39142, 103.89515?"_
- _"I dropped a pin at these coordinates — what's around it?"_

**Returns** up to 3 nearby addresses with building name (where
present), full address, and postal code. Coordinates outside Singapore
are rejected with `ERR_COORDINATES_OUT_OF_BOUNDS`.

---

### `search_bus_stops(query, latitude, longitude, radius_m=500, limit=10) -> str`

Find bus stops by text match **or** by proximity to coordinates. Geo mode
takes precedence if both are provided.

**Example prompts:**
- _"Find bus stops near VivoCity"_ (Claude resolves VivoCity first, then calls this)
- _"Bus stops on Bedok North Road"_ (text mode)

**Returns** stop code, description, road name, and — in geo mode — walking
distance in metres, sorted ascending.

---

### `get_bus_arrivals(bus_stop_code: str, service_no: str | None) -> str`

Live arrival times at a specific stop. Returns up to 3 buses per service
with ETA, GPS/scheduled indicator, load, bus type, wheelchair access, and
**destination terminal** (the end stop of the route).

**Example prompts:**
- _"When's the next bus at stop 14131?"_
- _"What time is the next 10 bus at Vivocity?"_

---

### `find_bus_route(from_latitude, from_longitude, to_latitude, to_longitude, max_walk_m=600, max_transfer_walk_m=200, max_total_min=120, limit=3) -> str`

**The recommended tool for trip planning.** Given origin and destination
coordinates, this tool finds the best bus journeys — **direct,
1-transfer, or 2-transfer** — scored server-side by total time. It
evaluates every candidate origin and destination stop within
`max_walk_m`, looks for:

- **Direct** services that serve both origin and destination stops in
  the correct direction.
- **1-transfer** service pairs that share an interchange stop
  reachable within `max_transfer_walk_m`.
- **2-transfer** service triples (bus A → walk → bus B → walk → bus C),
  where each transfer walk is within `max_transfer_walk_m`. Added in
  v0.2 for cross-island pairs where no good direct or single-transfer
  option exists.

For each candidate it fetches live ETA at the origin, estimates
in-vehicle and transfer times, and ranks the top `limit` options by
**total walk + wait + ride** time.

**Example prompts:**
- _"What's the best bus from Bedok Mall to Gleneagles Hospital?"_ (likely 1-transfer)
- _"How do I get from Sengkang to Tuas Link by bus?"_ (likely 2-transfer — cross-island)

**Returns** ranked options with: origin stop code + walk distance,
live ETA, ride stop count, alight stop (for transfers, board stop +
transfer walk), terminus for each leg, and estimated total journey
time. 2-transfer options use a distinct `OPTION N — 2-TRANSFER — M
min total` header to signal the leg count; direct and 1-transfer
keep the original `{i}. Bus X → ...` header shape for backward
compatibility.

**Bounds:**
- Direct + 1-transfer options are capped at **90 min** total time
  regardless of `max_total_min` (preserving v0.1.0 behaviour).
- 2-transfer options use `max_total_min` (default **120 min**, raised
  in v0.2 from 90 to accommodate the extra transfer wait). The caller
  can override; passing a tighter `max_total_min` applies to all
  kinds.
- 2-transfer enumeration is capped at **500 candidate triples** per
  call to prevent combinatorial blow-up. If hit, the response appends
  `Note: 2-transfer evaluation truncated at 500 candidates; results
  are best-found-so-far.` — only when a 2-transfer option actually
  surfaced in the ranked output. Enumeration is ordered by
  cost-promising heuristics (shortest middle-leg ride first, shortest
  origin/destination walk first) so early termination still yields
  good results.

If no direct, 1-transfer, or 2-transfer option is found within the
constraints, the tool returns `ERR_NO_BUS_ROUTE` — fall back to MRT
or loosen the walk radii.

**Honest limits**: transfer wait time is a fixed 10-minute assumption
per transfer point (we don't have scheduled intervals). In-vehicle
time is a flat ~1.8 min/stop estimate. Totals are flagged as
estimates in the output. No MRT routing — see Limitations.

---

### `get_train_alerts(line: str | None) -> str`

Current MRT/LRT disruption alerts. If all lines are operating normally,
returns that as a single clean line. Filter by line code: `NSL`, `EWL`,
`CCL`, `DTL`, `TEL`, `NEL`, `BPLRT`, `SKLRT`, `PGLRT`.

**Example prompts:**
- _"Are there any MRT disruptions?"_
- _"Any issues on the East-West Line?"_

---

### `get_carpark_availability(area, latitude, longitude, radius_m=500, lot_type="C", min_lots=0) -> str`

Live carpark availability across HDB, URA, and LTA carparks. Text search
(on `area` / `development`) or geo search. `lot_type`: `C` (car, default),
`Y` (motorcycle), `H` (heavy vehicle).

**Example prompts:**
- _"Find parking near Marina Bay Sands with at least 100 spots"_
- _"Motorcycle parking near Suntec City"_

Results are capped at 20 rows and sorted by distance (geo mode) or lot
count descending (text mode).

---

### `get_location_context(latitude: float, longitude: float, radius_m=500) -> str`

**One-shot "what's near here?"** Given Singapore coordinates and a
search radius, returns an aggregated snapshot of nearby transport
infrastructure so your agent doesn't have to chain four tool calls.

For the given radius the tool returns, in a single response:

- Up to **5 nearest bus stops** (code, name, road, walking distance).
- Up to **5 nearest carparks** with at least 1 available car lot
  (name, lots free, distance).
- Up to **3 nearest MRT/LRT stations** with their codes, lines, and
  distance — sourced from a bundled catalog of all 181 operational
  stations across the 9 rail lines (NSL, EWL, CCL, DTL, TEL, NEL,
  BPLRT, SKLRT, PGLRT).
- **Current alert status** for each line among those nearby stations,
  using the same LTA feed `get_train_alerts` reads.

`radius_m` silently clamps at 5000m (5 km) to keep the scan bounded.
If nothing is within radius, you get a clean
"No transport infrastructure within <N>m …" message — not an error,
not an empty body.

**Example prompts:**
- _"What's near Sengkang MRT?"_ (the agent resolves Sengkang first, then calls this)
- _"I'm at 1.29349, 103.85583 — what are my travel options?"_
- _"What's around Marina Bay Sands?"_

**Graceful degradation**: if the MRT alerts feed is slow or
unreachable, the `LINE STATUS` section degrades to a single
`unavailable` line; the rest of the response still renders. The tool
never fails outright because of alerts.

**When to use vs. chaining:** prefer `get_location_context` over the
manual chain of `search_bus_stops` + `get_carpark_availability` +
`get_train_alerts` when the user's question is "what's near X?". It's
one call, already scoped to the radius, and already joined against
line alerts for you.

---

### `find_route(from_latitude, from_longitude, to_latitude, to_longitude) -> str`

**Unified "best route from A to B" dispatcher.** Given an origin and a
destination coordinate, returns a single ranked list combining bus
options, a straight-line walking estimate, and an MRT-suggestion hint —
without the agent having to chain `find_bus_route` + walking calc + MRT
lookup itself.

In one response, you get:

- **Bus options** — the top-ranked journeys from `find_bus_route` (up to
  2 transfers), relabelled `OPTION N — BUS — M min total` and sorted by
  estimated time. The per-leg body is delegated unchanged, so stop codes,
  live ETAs, and per-leg timings match what `find_bus_route` would have
  produced on its own.
- **Walking option** — included when the straight-line walk is under 25
  minutes (at the 80 m/min v0.1.0 assumption), formatted
  `OPTION N — WALK — M min (D km)`. Omitted otherwise with a footer
  noting the omission. As a fallback, walking is included anyway when
  no bus and no MRT option exists.
- **MRT suggestion** — if both endpoints are within 800 m of an MRT or
  LRT station (per the Phase 2 catalog), the output includes an
  `OPTION N — MRT SUGGESTION` block naming the board and alight
  candidates, their codes, lines, and walk distances. This is a hint,
  not a routed plan — `find_route` does not compute MRT travel time or
  the transfer interchange (the agent fills those in from general
  knowledge, per R10). The hint also covers LRT: Sengkang, Punggol, and
  Bukit Panjang LRT stations are surfaced the same way.

**Ranking**: bus and walking options are sorted ascending by estimated
total time. The MRT suggestion has no time estimate and appears last.

**Long-distance short-circuit**: if the straight-line distance exceeds
~25 km (cross-island extreme), `find_route` skips the bus enumeration
entirely and returns just the MRT suggestion + walking fallback. This
keeps per-call latency bounded for the worst-case pairs and is the
prescribed fallback for RISK-11.

**Latency**: under 7 s at the 95th percentile for typical Singapore
pairs after cache warm. First call may take longer due to cache warm
(surfaced via the same `MSG_FIRST_CALL_WARM` footer as `find_bus_route`).

**Example prompts:**
- _"Best route from Compass One to Outram Park?"_
- _"How do I get from Tuas Link to Changi Airport?"_ (triggers the
  long-distance short-circuit)
- _"Best way from Sengkang to Vivocity?"_

**When to use vs. chaining:** this is the recommended starting point for
"best route from A to B" questions. Prefer `find_route` over chaining
`find_bus_route` + walking calc + MRT lookup manually.
`get_train_alerts` is a deliberate separate call — `find_route` never
calls it, so the agent must call `get_train_alerts` or
`get_location_context` separately if alert awareness is needed.

---

## Use cases

Patterns that work well in practice:

### 1. "When's my bus?"

> When's the next bus 15 at Bedok MRT?

Claude calls `resolve_location` → `search_bus_stops` (geo) →
`get_bus_arrivals(service_no="15")`. Three tool calls, one clean answer.

### 2. Best route between two landmarks

> What's the fastest route from Compass One to Outram Park?

Claude calls `resolve_location` twice then `find_route`. The tool
composes bus options (direct, 1-transfer, or 2-transfer via
`find_bus_route`), a straight-line walking estimate when the pair is
close, and an MRT-suggestion hint when both endpoints are within 800 m
of a station — all ranked and returned in one call.

Prefer `find_route` as the entry point for "best route A → B" queries.
Use `find_bus_route` directly only when you need the raw bus-only
ranking (e.g., explicit walk-radius or max-time overrides, or debugging).

For cross-island pairs like **Sengkang → Tuas Link** where no good
direct or single-transfer option exists, `find_route` (via
`find_bus_route`) will surface 2-transfer options labelled
`OPTION N — BUS (2-TRANSFER) — M min total`, complete with the two
transfer blocks. This is v0.2's key routing upgrade over v0.1.0.

### 3. Find parking near a destination

> I'm driving to Marina Bay Sands. Where can I park with at least 80 spots?

Claude calls `resolve_location` → `get_carpark_availability(latitude=...,
longitude=..., min_lots=80)`. You get a live lots count per carpark with
distances.

### 4. Full-day itinerary — the main event

This is where the server earns its keep. Feed your assistant a list of
appointments and let it plan the whole day:

> I have the dentist at Tampines Mall at 10, lunch at 68 Circuit Road at
> 12, Gleneagles Hospital at 2:30, class at Marine Parade at 4, then home
> to Tampines in the evening. Plan the public-transport route, optimised
> for time.

Behind the scenes your agent will:

1. `resolve_location` each venue (5–6 calls) to get coordinates.
2. `find_bus_route` for each leg — the server does the walk + wait +
   ride scoring so the agent gets a ranked answer, not a bus number to
   guess between.
3. Fall back to MRT when `find_bus_route` returns nothing (or every
   option exceeds the 120-min cap for 2-transfer journeys, or
   90-min for direct/1-transfer) — your agent fills in train legs
   from general knowledge, knowing the bus options were genuinely
   checked.
4. Optionally `get_train_alerts` to check for disruptions before
   committing to an MRT-heavy itinerary.
5. Assemble a timed plan with leave-by times and buffers.

This is the workload this server was built for.

### 5. Check disruptions before heading out

> Are any trains delayed right now?

Single `get_train_alerts` call. If everything is normal you get a one-line
confirmation; if anything is disrupted you get affected lines, stations,
messages, and any free-bus bridging service.

### 6. "What's near X?" in one call

> What's near Sengkang MRT?

The agent calls `resolve_location("Sengkang MRT")` → `get_location_context(lat, lng)`.
Two calls, one complete answer: nearest bus stops, carparks with free
lots, MRT/LRT stations, and current alert status for those lines — all
joined and scoped to a single radius. The alternative (chaining
`search_bus_stops` + `get_carpark_availability` + `get_train_alerts`
yourself) is four tool calls and no cross-joining.

---

## Troubleshooting

### `RuntimeError: Missing required environment variables: ...`

Your env vars aren't reaching the process. Check:

- `.env` exists in the **project root** (not in a parent directory).
- You haven't typo'd the variable names.
- For Claude Desktop: the `env` block is inside the correct server entry,
  and Claude Desktop has been **fully restarted** (⌘Q, not close window).
- For local runs: `echo $LTA_ACCOUNT_KEY` in the same shell returns your key.

### `OneMap auth failed (401)`

The email or password is wrong, or your OneMap account isn't active. Log
into <https://www.onemap.gov.sg/apidocs/> in a browser to verify.

### `LTA /BusStops returned 401/403`

Your LTA AccountKey is invalid or still pending approval. Check your email
for the approval message — LTA issues keys within 1–2 business days.

### The first `find_bus_route` or `search_bus_stops` call is slow

Expected. The server lazy-loads ~5,200 bus stops (and ~26,700 bus route
rows on first `find_bus_route` call) from LTA on first use, then caches
them in memory for 24 hours. Subsequent calls are instant.

### Claude doesn't call my tools

- Check `~/Library/Logs/Claude/mcp-server-sg-mobility-mcp.log` (macOS) for
  startup errors.
- Verify Claude Desktop sees the server: in a new chat, ask
  _"What tools do you have from sg-mobility-mcp?"_ — Claude should list all eight.
- If Claude answers without calling any tool (e.g.
  _"I don't have real-time transport data"_), ask more directly:
  _"Please call the get_train_alerts tool."_

### Tool returns "No buses currently arriving at stop X"

Real data. Outside operating hours, or the stop code is wrong. Use
`search_bus_stops` to verify the code.

---

## Architecture

Small, flat, explicit — no framework, no database, no background workers.

- **Transport**: stdio. Claude Desktop spawns the server as a child process
  and communicates over stdin/stdout using the MCP protocol. All logs go
  to stderr to avoid corrupting the protocol stream.
- **Clients**: one long-lived `httpx.AsyncClient` per API (LTA, OneMap).
  LTA requests inject the `AccountKey` header once, at client creation.
  OneMap requests attach a Bearer token per call.
- **OneMap auth**: the `access_token` is a JWT. The server decodes the
  `exp` claim on receipt and refreshes 5 minutes before expiry, guarded by
  an `asyncio.Lock` so concurrent tool calls don't double-fetch.
- **Caches** (`cache.py`):
  - `bus_stops` list — warmed lazily on first `search_bus_stops`,
    `get_bus_arrivals`, or `get_location_context` call. Used for
    code → name resolution and proximity search. 24h TTL.
  - `routes_by_service` / `routes_by_stop` — warmed lazily on first
    `find_bus_route` call. Indexed two ways so both "what services pass
    this stop?" and "what's the stop sequence on this route?" are O(1).
    24h TTL.
- **Static MRT/LRT catalog** (`data/mrt_stations.json`): a hand-curated
  JSON array of all 181 operational stations across the 9 rail lines,
  loaded once at server import and held in memory. Enables
  `get_location_context` (and future tools) to resolve "nearby
  stations" without calling any upstream API. See `data/README.md` for
  the schema and the quarterly-review process.
- **Fail-fast**: `server.py` validates all three env vars and the MRT
  station catalog at import time. Missing env vars or a malformed /
  missing `data/mrt_stations.json` produce a clear error pointing back
  at this README, rather than a cryptic failure on first tool call.
- **Tool registration**: each `tools/*.py` file exports a
  `register_X_tools(mcp, *deps)` function. `server.py` calls each
  explicitly — no wildcard imports, no import-order fragility.

---

## Limitations

Be honest with users about what this server does not do:

1. **`find_bus_route` supports up to 2 bus transfers (3 buses).**
   Three or more transfers are not planned; if the best route needs
   more hops, the tool returns `ERR_NO_BUS_ROUTE` and you fall back
   to MRT. The 2-transfer search is bounded by a 500-candidate
   evaluation cap — for cross-island pairs you may see a "best-found-
   so-far" note rather than a guaranteed-optimal result.
2. **No MRT routing.** The server does not plan MRT rides or
   transfers. It _does_ know where the 181 operational MRT/LRT
   stations are (via the bundled catalog) — enough for
   `get_location_context` to surface "there's an MRT station X metres
   away on lines Y, Z" and for `find_route` to emit a board/alight
   candidate hint when both endpoints are within 800 m of a station
   — but it will not compute a train journey between stations.
   `find_route` explicitly does not return MRT travel time or a
   computed interchange station. When a trip needs MRT, the agent
   fills in the route from general knowledge. The station catalog is
   reviewed quarterly (see `data/README.md`); new stations opened
   since the last review may be missing.
3. **In-vehicle time, walking speed, and transfer wait are estimates.**
   `find_bus_route` uses a flat ~1.8 minutes per stop, 80 m/min walking,
   and a fixed 10-minute wait at any transfer point (we don't have
   scheduled intervals). Real bus rides vary with traffic,
   express-vs-local service patterns, and time of day.
4. **No walking-directions planner.** `find_route` includes a
   straight-line walking estimate (distance / 80 m/min) as a ranked
   option when it's under 25 minutes, and `find_bus_route` short-
   circuits with a "walk is faster" note for very short pairs. Neither
   computes turn-by-turn walking directions.
5. **Carpark data comes from LTA's feed** — not every carpark in Singapore
   is included (e.g. some private ones).
6. **Singapore only.** The data sources are Singapore-specific; this
   server is not useful outside SG.

---

## Security

- This server is designed for single-tenant local use. Do not host this
  server on a shared multi-tenant endpoint without first adding
  authentication, quota, and audit — none of which v0.2 provides.
- `.env` is in `.gitignore`. So are `.env.*` (except `.env.example`),
  `*.pem`, `*.key`, `*.p12`, and `.DS_Store`. **Do not remove these
  rules.**
- The server never logs credentials. Stderr output is limited to cache
  warm notifications and HTTP request info (URLs only, not headers).
- If a key leaks (e.g. accidentally committed):
  - **LTA**: log into the DataMall portal and request a key rotation.
  - **OneMap**: change your password at onemap.gov.sg immediately.
  - Rewrite git history with `git filter-repo` or similar, and force-push
    only if the repo is yours alone.
- Before pushing to GitHub, always run `git status` — `.env` should
  **never** appear in the tracked list.

---

## Project layout

```
sg-mobility-mcp/
├── server.py              ← entry point + tool registration + env fail-fast + MRT catalog load
├── api/
│   ├── __init__.py
│   ├── lta.py             ← LTA DataMall client (paginated GET + typed methods)
│   └── onemap.py          ← OneMap client with JWT auto-refresh + lock
├── tools/
│   ├── __init__.py
│   ├── _format.py         ← shared envelope / error-string helpers
│   ├── bus.py             ← search_bus_stops, get_bus_arrivals
│   ├── train.py           ← get_train_alerts
│   ├── carpark.py         ← get_carpark_availability
│   ├── location.py        ← resolve_location, reverse_geocode
│   ├── routing.py         ← find_bus_route
│   └── context.py         ← get_location_context (aggregation)
├── data/
│   ├── mrt_stations.json  ← hand-curated MRT/LRT station catalog (loaded at startup)
│   └── README.md          ← schema, update sources, quarterly-review process
├── cache.py               ← lazy-warmed bus stops + bus routes (24h TTL each)
├── requirements.txt
├── .env.example           ← template with placeholders (safe to commit)
├── .gitignore
└── README.md
```

---

## Updating and maintenance

- **Cache refresh.** Both caches have a 24-hour TTL. Restarting the server
  forces a full re-warm on next use. There is no manual cache-invalidation
  tool by design — LTA data is stable enough that 24h works.
- **LTA changes an endpoint path.** If an LTA endpoint moves (e.g.
  `/CarParkAvailabilityv2` → `v3`), the affected tool will return a clear
  `"LTA <path> returned 404: ..."` error. Update the path in `api/lta.py`
  and restart.
- **OneMap changes auth.** If OneMap rotates their token scheme, update
  `api/onemap.py`. The JWT expiry-parsing logic is the most coupled to
  their current format.
- **Adding a new tool.** Create a new file under `tools/` with a
  `register_*_tools(mcp, ...)` function. Register it in `server.py`. Follow
  the existing pattern: return a formatted string via the helpers in
  `tools/_format.py`, catch upstream exceptions and convert to a
  user-facing `ERR_*` message, do not let raw exceptions reach the MCP
  layer.
- **MRT/LRT station catalog.** `data/mrt_stations.json` is a static,
  hand-curated file loaded once at startup. When a new station opens
  (roughly every 18–24 months — e.g. TEL5, JRL, CRL extensions):
  1. Add the entry to `data/mrt_stations.json`. Schema and sources are
     documented in `data/README.md`.
  2. Commit the change and restart the server. The file is only read at
     import time; a running server will not pick up new stations.

  There is no automated refresh pipeline by design — the static-file
  approach keeps startup network-independent and the catalog
  audit-friendly.

---

## License

[MIT](LICENSE) © 2026 Jimmy Tong.
