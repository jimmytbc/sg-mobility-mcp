# NOTES — post-v0.2 observations

Adjacent improvements noticed during v0.2 delivery but deliberately
not implemented in scope. Logged per `specs/00-rules.md` R7 for
product-owner triage. Each entry is a candidate — not a commitment.

---

## OneMap: resolve_location returns tenant shops for multi-tenant buildings

*Observed 2026-04-22 in Phase 2 debug handoff.*

`resolve_location("Centrepoint Orchard Road")` returned three
tenant shops ("365 Juices Bar The Centrepoint", "49 Seats The
Centrepoint", "Chrysalis Spa The Centrepoint") instead of "The
Centrepoint" building. The agent picked one and inferred intent,
but the top-1 heuristic is fragile for multi-tenant buildings.

- **Fix shape**: post-process OneMap hits in `tools/location.py` —
  dedupe by `ADDRESS`, prefer entries where `BUILDING == SEARCHVAL`
  (the building itself, not a tenant). Fall back to current
  behaviour if no canonical building row is present.
- **Effort**: ~30 min. Localised to `tools/location.py`. Low risk.

---

## Service-category awareness (feeders, City Direct, peak-only)

*Observed 2026-04-22 across two sessions.*

`find_bus_route` suggested Tampines → Punggol → MBS via bus 118 +
City Direct 666/673. The services are real per LTA, and the
transfer geography is valid — but City Direct buses run only on
weekday peak hours, which the flat 10-min transfer-wait estimate
wildly misrepresents off-peak. Same class of issue surfaces with
feeders (short-range, many-stop loops designed for HDB-to-MRT, not
cross-island travel) that can win the scoring because 1.8 min/stop
treats all stops equally.

- **Fix shape**: add a third cache keyed by `ServiceNo` from LTA's
  `/BusServices` endpoint (`Category`: TRUNK / FEEDER / EXPRESS /
  CITY_DIRECT / INDUSTRIAL / NIGHT_RIDER / ...). In
  `find_bus_route` scoring: either filter non-TRUNK services or
  apply a category-based penalty.
- **Effort**: ~1–2 hours. Touches `api/lta.py`, `cache.py`,
  `tools/routing.py`. Natural to fold into Phase 3, which already
  edits scoring.

---

## "Feeder to MRT station X" as a first-class query

*Observed 2026-04-22.*

Confirming that bus 454 is a Tampines feeder to Tampines West MRT
required 5 tool calls: resolve origin → search stops near origin →
arrivals at origin stop → resolve MRT → search stops near MRT →
arrivals at MRT stop → manual cross-reference. The common case
("which feeder gets me from here to the nearest MRT?") deserves a
direct tool.

- **Fix shape A**: new `find_feeder_bus(latitude, longitude,
  station_code) -> str` tool. Simpler, Singapore-idiomatic.
- **Fix shape B**: `via_stop` parameter on `find_bus_route` that
  constrains the route through a named stop. More general, but
  scoring becomes conditional.
- **Effort**: A ≈ 2 hours; B ≈ half-day.

---

## get_location_context radius UX in transit-gap estates

*Observed 2026-04-22.*

`radius_m=500` (default) returned zero MRT stations for origin
near Parc Central Residences (Tampines St 86). The agent
re-queried at 800m, then 1500m before surfacing Tampines West at
1462m. The server behaved correctly — outer estates simply have no
MRT within walking distance — but the default was a false start.

- **Fix shape**: update the `@mcp.tool()` description for
  `get_location_context` to nudge the agent toward 1500–2000m in
  suburban / outlying estates. No behaviour change; better
  upstream prompting only.
- **Effort**: ~5 min. Verify description stays ≤500 chars per
  `specs/05-ui.md` §5.3.

---

## MRT interchange transfer walk times not represented

*Observed 2026-04-22.*

`data/mrt_stations.json` stores one coordinate per interchange, so
transfer walks within Dhoby Ghaut (NS/NE/CCL), Outram Park
(NE/EW/TEL), Botanic Gardens (CCL/DTL), etc. read as 0m. The
debug-session agent narrated transfer times from general
knowledge — works, but isn't tool-verified.

- **Relevance**: only matters if MRT routing is added. Currently
  out of scope per `specs/09-out-of-scope.md`. If scope ever
  expands (v0.3+ consideration), this is the data gap to close.
- **Fix shape (if scope expands)**: add a hand-curated
  `interchange_walk_min` field to station entries at known
  interchanges (e.g. Dhoby Ghaut ~6 min, Jurong East ~5 min,
  Outram Park ~4 min) rather than per-platform coordinates. Spec
  amendment + probe update required.
- **Effort**: ~2 hours data entry + schema update + probe update.
  Gated on scope decision, not an immediate candidate.
