# Follow-ups - out-of-scope items for operator triage

Per `~/.claude/CLAUDE.md` §5.3. Opened 2026-07-30 during the MCP-spec
transport migration (legacy SSE → streamable HTTP in `docker-compose.yml`).

## Open

### F-1 - SDK pin blocks MCP 2026-07-28

- `requirements.txt` pins `mcp[cli]==1.27.0`, whose protocol ceiling is
  2025-11-25. Bump deliberately once a 2026-07-28-capable release is vetted
  (dependency edits need explicit authorisation per §7). First-party code
  touches minimal SDK surface, so the bump should be near-mechanical.

### F-2 - server.json staleness

- `version` says 0.1.0 but CHANGELOG shows v0.2.0 shipped; description still
  advertises "bus trip planning up to 2 transfers" though `find_bus_route`
  was deregistered in Phase 5; no `packages`/`remotes` block, so the remote
  deployment is invisible to the registry. If publishing the remote endpoint,
  declare `streamable-http` there.

### F-3 - MCP_TRANSPORT env passthrough is unvalidated

- `server.py` passes the env value straight to `mcp.run()`; a typo fails
  deep inside the SDK instead of at startup. Add an allowlist
  (`stdio` / `streamable-http`) with a clear error.

### F-4 - Auth for the exposed HTTP endpoint

- `docker-compose.yml` binds `0.0.0.0:8001` with no authentication while the
  process holds LTA and OneMap credentials - a credential proxy for anyone
  who can reach the port. Decide mechanism at Stage 2 (bearer middleware,
  reverse proxy, or private-network-only).

### F-5 - README does not document the Docker/remote deployment

- README covers stdio only; the compose deployment (now streamable HTTP at
  `/mcp` on :8001) is undocumented, including the client endpoint change
  from `/sse` to `/mcp`.
