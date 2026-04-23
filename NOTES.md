# NOTES — post-v0.2 observations

Adjacent improvements noticed during v0.2 delivery but deliberately
not implemented in scope. Logged per `specs/00-rules.md` R7 for
product-owner triage. Each entry is a candidate — not a commitment.

## Phase 5 resolution summary (2026-04-23)

Four of the items below were rendered moot or directly resolved by
the v0.2-phase-5 `find_route` REPLACE (OneMap PT thin orchestrator).
Marked inline under each affected section. Items 1, 2, 4, 5 remain
open for v0.3 triage.

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

**Scope narrowed by Phase 5.** OneMap's PT routing uses the published
schedule, so the misrepresentation is resolved on the primary
`find_route` path — City Direct / peak-only services won't be
suggested off-peak. The issue remains for the bus-only fallback
(`find_bus_route_impl`), which still uses the flat 10-min transfer
wait and 1.8 min/stop heuristic.

- **Fix shape**: add a third cache keyed by `ServiceNo` from LTA's
  `/BusServices` endpoint (`Category`: TRUNK / FEEDER / EXPRESS /
  CITY_DIRECT / INDUSTRIAL / NIGHT_RIDER / ...). In
  `find_bus_route_impl` scoring: either filter non-TRUNK services
  or apply a category-based penalty.
- **Effort**: ~1–2 hours. Touches `api/lta.py`, `cache.py`,
  `tools/routing.py`. Lower priority than before since it only
  affects the fallback path.

---

## "Feeder to MRT station X" as a first-class query

*Observed 2026-04-22.* **Resolved by Phase 5.**

The common case ("which feeder gets me from here to the nearest
MRT?") is now a single `find_route` call — OneMap PT returns
multimodal itineraries that chain feeder-bus WALK + BUS legs into
a SUBWAY leg automatically, with schedule-based durations and the
actual station codes in the response. Whether the agent asks "how
do I get from X to Y?" or "what's the feeder to Tampines West?",
the tool covers both shapes. No dedicated `find_feeder_bus` tool
is needed.

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

*Observed 2026-04-22 during Phase 4 verification.* **Obsolete after
Phase 5.**

The MRT-suggestion pattern (single `Board candidate` / `Alight
candidate` within 800 m) was replaced wholesale by Phase 5's OneMap
PT orchestrator. OneMap picks the route-optimal boarding/alighting
stations itself as part of the returned itinerary — no geographic-
nearest heuristic in Phase 5 code. Kept in NOTES for traceability
against the v0.2-phase-4 tag.

---

## "47m walk" in find_route MRT block misread as minutes by the agent

*Observed 2026-04-22 during Phase 4 verification (Scenario 3,
Tuas Link → Changi Airport).* **Obsolete after Phase 5.**

The MRT SUGGESTION block is gone in Phase 5. WALK legs in the new
envelope carry the distance as `(47 m)` (space before unit, per the
§5.1 mock-up) inside a column already labelled WALK, and appear
alongside an explicit `N min` duration in a separate column — so the
unit ambiguity that tripped up the agent is structurally avoided.
Kept in NOTES for traceability against the v0.2-phase-4 tag.

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

---

## find_route sensitivity to input-coord precision (Phase 5)

*Observed 2026-04-23 during Phase 5 live testing (Singapore Zoo →
Parc Central Residences, 528516).*

The same natural-language prompt produced itineraries that differed
by 10+ min depending on how `resolve_location` pinned the endpoints.
A ~400 m origin shift and ~900 m destination shift between two runs
produced a 96 min vs. 106 min fastest itinerary and a 12 m vs. 207 m
final walk — because the "better" coordinates happened to sit within
an express-bus (969) stop radius that the other coordinates missed.

This is OneMap's PT routing behaving correctly (tight first/last-mile
coupling to nearby transit stops). The weak link is upstream: postal
codes resolve to postcode centroids, which can be 100–300 m off the
actual building entrance.

- **Fix shape A**: agent-side nudge — prefer building-name searches
  over postal-code searches when both are available. Document in
  `resolve_location`'s `@mcp.tool()` description.
- **Fix shape B**: server-side — snap `find_route` input coordinates
  to the nearest bus stop / MRT node before calling OneMap, up to a
  capped offset (e.g. 150 m).
- **Fix shape C**: run OneMap twice (name-resolved + postcode-
  resolved coords) and union the itineraries, dedup-ing by leg
  signature.
- **Effort**: A ≈ 5 min; B ≈ half-day + probe; C ≈ 1–2 hours but
  doubles the OneMap call budget per `find_route` invocation.

---

## OneMap PT ranks by duration only; no transfer-comfort weighting (Phase 5)

*Observed 2026-04-23 during Phase 5 live testing (Tampines → Pasir
Ris St 72).*

OneMap returned 3 itineraries where Option 1 was 18 min / 1 transfer
and Option 2 was 19 min / 0 transfers. OneMap's ranker preferred the
1-transfer-for-1-min-savings itinerary; the consuming agent correctly
downgraded it in the narration ("skip the transfer, take the direct
bus"). This meant the tool's top-ranked itinerary wasn't the one the
user would pick.

The OneMap PT endpoint exposes a `transferPenalty` query parameter
(seen at `"2500"` in `scratch/onemap-pt-probe-pair-2.json`'s
`requestParameters`). Tuning that could nudge OneMap's ranker toward
the human-comfort choice and remove the narration-layer override.

- **Fix shape**: add `transferPenalty` to the `api/onemap.py
  route_pt` call with a calibrated default (probe-verified;
  candidate values 60 / 120 / 180 s). Validate that the new ranking
  still produces the Sengkang → Outram and Tampines → Far East
  Flora outcomes unchanged.
- **Effort**: ~1 hour including calibration against the existing
  5-pair set.
- **Relevance**: medium. Avoids a class of agent-narration overrides
  that otherwise have to be documented as expected behaviour.
