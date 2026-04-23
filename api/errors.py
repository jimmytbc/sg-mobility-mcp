"""Typed upstream errors raised by the LTA and OneMap clients.

Tools catch these and map to the `ERR_*` string IDs from
specs/05-ui.md §5.4 via tools/_format.py, so no raw `RuntimeError`
reaches the MCP layer (specs/00-rules.md R11).
"""

from __future__ import annotations


class UpstreamError(RuntimeError):
    """Base for recoverable upstream API errors."""


class LTAAuthFailed(UpstreamError):
    pass


class LTATimeout(UpstreamError):
    pass


class LTARateLimited(UpstreamError):
    pass


class LTAEndpointNotFound(UpstreamError):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"LTA 404 for {path}")


class OneMapAuthFailed(UpstreamError):
    pass


class OneMapTimeout(UpstreamError):
    pass


class OneMapSchemaDrift(UpstreamError):
    pass


class OneMapRoutingServiceDown(UpstreamError):
    """OneMap PT routing returned 5xx. Per FR-7.4 / FR-E.16 triggers
    immediate fallback — no retry on 5xx."""


class OneMapRoutingRateLimited(UpstreamError):
    """OneMap PT routing returned 429 and the backoff budget was
    exhausted. Per FR-7.4 / FR-E.17 triggers fallback."""
