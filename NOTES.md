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

## resolve_location misses on bare HDB block numbers

*Observed 2026-04-22 during Phase 4 verification.*

`resolve_location("Blk 263")` and `resolve_location("Blk 405A")`
failed to pin the specific blocks. The agent fell back to a rough
point along the nearest named road (e.g., "Punggol Way corridor").
For Blk 405A, the fallback landed south of the actual block — close
enough to Damai LRT (PE7) that `find_route`'s MRT suggestion named
Damai instead of the correct nearest station, Samudera (PW4), which
is ~200m from Blk 405A's real coordinates. `find_route` is behaving
correctly given its inputs; the geocoder is the weak link.

- **Fix shape A**: enrich `resolve_location` with an HDB-block
  fallback — if the query matches `^Blk(?:ock)?\s+\d+[A-Z]?` and
  OneMap returns no direct hit, try appending the estate
  (Compassvale, Rivervale, Punggol Field, etc.) from context, or
  surface a targeted re-ask ("which estate is Blk 405A in?")
  instead of silently approximating.
- **Fix shape B**: a dedicated `resolve_hdb_block(block, estate)`
  tool — narrower signature, OneMap's block-search parameters are
  richer than the generic search.
- **Effort**: A ≈ 1–2 hours (regex + re-ask path). B ≈ half-day
  (new tool, probe, docs). Either way, localised to OneMap path.

---

## find_route MRT suggestion limited to single nearest station

*Observed 2026-04-22 during Phase 4 verification.*

`find_route` emits one `Board candidate` and one `Alight candidate`
— the station nearest each endpoint within 800m. If the geocoded
coordinates are slightly off (see bare-HDB-block issue above), or
if a slightly-further station would give a better line match (e.g.,
origin has two stations at 600m and 700m on different lines, one
shared with the destination's station), the tool picks geographic
nearest rather than route-optimal. An agent cannot recover what the
tool didn't surface.

- **Fix shape**: emit up to 3 candidate stations per endpoint,
  ranked by walking distance, letting the agent pick the
  route-optimal pairing. §5.1 mock-up would need a minor update
  (plural `Board candidates:` block). Schema change is additive
  per R9.
- **Effort**: ~1 hour in `tools/discovery.py` + spec amendment
  + mock-up update. Low risk, no upstream-API impact.

---

## "47m walk" in find_route MRT block misread as minutes by the agent

*Observed 2026-04-22 during Phase 4 verification (Scenario 3,
Tuas Link → Changi Airport).*

`find_route`'s MRT SUGGESTION body uses the project-wide §5.2
distance convention: `47m walk from origin`, `36m walk to
destination`. For small values (<100m) the agent rendered these
as "~47 min walk" / "~36 min" in its narrative — mistaking metres
for minutes. The values are plausible as either unit in a travel
context, so the LLM guessed wrong.

- **Fix shape**: in `tools/discovery.py`, emit distances as
  `47 m walk from origin` (space) or `47 metres walk from origin`
  within the MRT block only. Conflicts with §5.2's `<n>m` rule, so
  a spec amendment is warranted. Alternatively, add explicit
  qualifier: `— 47m (distance) walk from origin`.
- **Relevance**: agent-readability, not correctness. The tool is
  emitting the right value; the unit is just ambiguous when
  context-free.
- **Effort**: ~10 min code + spec amendment in §5.2 / §5.4.
  Low risk, output-only change.

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
