# data/

Static data files shipped with the server. Loaded once at startup and
held in memory.

## `mrt_stations.json`

Hand-curated catalog of all currently-operational Singapore MRT and
LRT stations. Consumed by `get_location_context` (Phase 2) and
`find_route` (Phase 4).

### Schema

A JSON array. Each element:

```json
{
  "name": "Sengkang",
  "codes": ["NE16", "STC"],
  "lines": ["NEL", "SKLRT"],
  "latitude": 1.39169,
  "longitude": 103.89518
}
```

| Field       | Type     | Notes                                                                 |
|-------------|----------|-----------------------------------------------------------------------|
| `name`      | string   | Canonical station name in title case (e.g., `"Sengkang"`).            |
| `codes`     | string[] | Station codes. Interchanges list every code (e.g., `["NE16","STC"]`). |
| `lines`     | string[] | Line codes served. One of: `NSL`, `EWL`, `CCL`, `DTL`, `TEL`, `NEL`, `BPLRT`, `SKLRT`, `PGLRT`. |
| `latitude`  | number   | WGS84 decimal degrees, 5 d.p. preferred.                              |
| `longitude` | number   | WGS84 decimal degrees, 5 d.p. preferred.                              |

Rules:

- Each physical interchange is **one** entry — do not duplicate the
  station across multiple records.
- Every `lines` value must be one of the 9 codes above. Anything else
  will fail the Phase 2 probe.
- Coordinates must lie inside the Singapore envelope
  (`lat ∈ [1.15, 1.50]`, `lng ∈ [103.55, 104.10]`).

### Sources for updates

In priority order:

1. **LTA DataMall station list** — authoritative for station codes
   and opening status.
2. **Land Transport Guru** (`landtransportguru.net`) — useful for
   cross-checking names and opening dates of new stations.
3. **Wikipedia** — the per-line articles (e.g., *Thomson–East Coast
   MRT line*) are reliable for station codes and consolidated
   coordinates.
4. **OpenStreetMap** — fallback for coordinates when LTA / Wikipedia
   disagree. Use the station platform centroid.

### Quarterly review

Review this file every 3 months (or immediately after LTA announces
a new station opening). New stations open roughly every 18–24 months
as MRT extensions come online:

- **TEL5** — Bedok South (TE30), Sungei Bedok (TE31). Opening TBD.
- **JRL** — Jurong Region Line, stages from 2027 onward.
- **CRL** — Cross Island Line Phase 1 from 2030 onward.

When new stations open:

1. Add the entries in this file in the natural code order of their
   line (keeps diffs readable).
2. Run `python probes/phase-2-probe.py` to re-validate the schema
   and coverage (RISK-4, RISK-10 in `specs/11-risks.md`).
3. Spot-check `get_location_context` against one coordinate near the
   new station to confirm it surfaces in the `MRT/LRT STATIONS`
   section.

No automated refresh pipeline is in scope for v0.2 — the static-file
approach was chosen deliberately so the catalog can be audited and
corrected by hand. See `specs/09-out-of-scope.md`.
