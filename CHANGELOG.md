# Changelog

All notable changes to `sg-mobility-mcp` are recorded here. Versions
follow [Semantic Versioning](https://semver.org). Per
[`specs/00-rules.md`](specs/00-rules.md) R9, schema changes within a
major version are additive only.

## [0.2.0] — 2026-04-22

An enhancement cycle over v0.1.0. Closes specific functionality gaps
without changing the architectural shape of the server. All output-
schema changes are additive; v0.1.0 consumers continue to work.

### Added

- **`reverse_geocode(latitude, longitude)`** — coordinates → up to 3
  nearby addresses via OneMap. New tool. Reuses the existing OneMap
  JWT client.
- **`get_location_context(latitude, longitude, radius_m=500)`** —
  one-shot "what's near here?": up to 5 bus stops, 5 carparks with
  lots available, 3 MRT/LRT stations, and current alert status for
  their lines, in a single response. New tool.
- **`find_route(from_lat, from_lng, to_lat, to_lng)`** — unified
  discovery tool. Composes `find_bus_route` (up to 2 transfers), a
  straight-line walking estimate when under 25 min, and an MRT-
  suggestion hint when both endpoints are within 800 m of a station.
  Ranked by estimated time; MRT suggestion appears last. Long-
  distance pairs (> 25 km straight-line) short-circuit the bus
  enumeration per RISK-11. New tool — recommended entry point for
  "best route from A to B" queries.
- **Bundled MRT/LRT station catalog** (`data/mrt_stations.json`) —
  181 operational stations across NSL, EWL, CCL, DTL, TEL, NEL,
  BPLRT, SKLRT, PGLRT. Hand-curated; reviewed quarterly (see
  `data/README.md`). Loaded once at server startup.
- **2-transfer bus routing** in `find_bus_route`. Enumerates journeys
  of up to three buses, bounded by a 500-candidate evaluation cap
  (RISK-6) in cost-promising order. `max_total_min` default raised
  to 120 for the 2-transfer path; direct and 1-transfer remain at
  the 90-minute v0.1.0 ceiling. A truncation footer surfaces when
  the cap fires.
- **Standardized output envelope** across all tools:
  `<tool-name> — <summary>` header, optional `Note: <caveat>` footer,
  `ERR_<NAME>: <message>` error prefix. Formatting helpers live in
  `tools/_format.py`. String IDs are spec-sourced from
  `specs/05-ui.md` §5.4.
- **LTA 429 rate-limit backoff** — exponential retry (2 s, 5 s, 15 s)
  before surfacing `ERR_LTA_RATE_LIMITED` per RISK-1.
- **Cache concurrency locks** — `asyncio.Lock` guards on `bus_stops`,
  `routes_by_service`, and `routes_by_stop` warms so bursty
  concurrent tool calls don't double-fetch LTA. Lock release on
  warm failure prevents poisoning.
- **First-call warm footer** — any tool that triggers a lazy cache
  warm surfaces `Note: first-call cache warm; subsequent calls will
  be faster.` (RISK-8).
- **Stop-label enrichment in `find_bus_route`** — alight-stop names
  now include the road name when the block number alone would be
  ambiguous across estates.
- **Single-tenant deployment warning** in `README.md` §Security per
  RISK-3.

### Changed

- `@mcp.tool()` descriptions for `find_route` and `find_bus_route`
  include an explicit nudge: option 1 is the fastest; preserve this
  ordering when recommending to the user. Addresses a verified
  agent-side failure mode where the top-ranked option is re-ranked
  by narrative plausibility.
- `find_bus_route_impl` extracted as a plain async function so
  `find_route` (Phase 4) and the Phase 0 probes can call it without
  going through the MCP tool indirection.

### Documentation

- `CLAUDE.md` updated: v0.2 cycle marked complete.
- `README.md` updated: nine-tool list, `find_route` reference, v0.2
  additions summary, updated limitations for MRT-suggestion and
  walking-estimate behaviour.
- `NOTES.md` logs post-v0.2 observations for triage: OneMap tenant-
  shop leakage, service-category awareness, feeder-to-MRT as a
  first-class query, `get_location_context` radius defaults for
  outlying estates, MRT interchange walk times, `resolve_location`
  misses on bare HDB block numbers, single-nearest-station limit in
  `find_route` MRT block, `<n>m` distance unit ambiguity in the MRT
  block, and agent ranking-override behaviour.

### Phase tags

v0.2 shipped in four reviewed phases:

- `v0.2-phase-1` — envelope, `reverse_geocode`, LTA backoff, cache locks.
- `v0.2-phase-2` — `get_location_context`, MRT/LRT catalog, stop-label enrichment.
- `v0.2-phase-3` — 2-transfer bus routing with 500-candidate cap.
- `v0.2-phase-4` — `find_route` unified discovery.

### Compatibility

No breaking changes to existing output shapes. v0.1.0 consumers
continue to parse v0.2 output; the envelope standardization is
additive (adds a header line and optional footer, never removes or
renames fields). No new required env vars. No new runtime
dependencies.

## [0.1.0] — prior release

Initial public release: `resolve_location`, `search_bus_stops`,
`get_bus_arrivals`, `find_bus_route` (direct + 1-transfer),
`get_train_alerts`, `get_carpark_availability`. Not documented in
this changelog.
