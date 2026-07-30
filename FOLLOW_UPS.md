# Follow-ups - out-of-scope items for operator triage

Per `~/.claude/CLAUDE.md` §5.3. Opened 2026-07-30 during the MCP-spec
transport migration (legacy SSE → streamable HTTP in `docker-compose.yml`).

## Open

### F-1 - Native MCP 2026-07-28 requires migration off the removed FastMCP shim

- 2026-07-30: bumped `mcp[cli]` 1.27.0 → 1.29.0 (final 1.x) under operator
  authorisation. Protocol ceiling remains 2025-11-25: `mcp 2.0.0` (the
  2026-07-28-native release) **removes** `mcp.server.fastmcp`, which
  `server.py` imports. Native support needs a small code migration - the
  likeliest target is the 2.x SDK's new high-level
  `mcp.server.mcpserver.MCPServer` (`@tool` decorator, similar surface);
  alternative is the standalone `fastmcp` package once its 4.x line
  (mcp>=2) is stable. Deprecation-clean and safe within the 12-month
  window meanwhile.

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
