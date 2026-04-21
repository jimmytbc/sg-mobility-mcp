"""MRT and LRT service disruption alerts.

Optional filter by line code (NSL, EWL, CCL, DTL, TEL, NEL, BPLRT,
SKLRT, PGLRT). Returns a clean one-liner when everything is operating
normally — which, happily, is most of the time.

Author: Jimmy Tong
"""

from __future__ import annotations

from api.errors import (
    LTAAuthFailed,
    LTAEndpointNotFound,
    LTARateLimited,
    LTATimeout,
    UpstreamError,
)
from api.lta import LTAClient
from tools._format import (
    ERR_INVALID_LINE_CODE,
    ERR_LTA_AUTH_FAILED,
    ERR_LTA_ENDPOINT_NOT_FOUND,
    ERR_LTA_RATE_LIMITED,
    ERR_LTA_TIMEOUT,
    MSG_ERR_LTA_AUTH_FAILED,
    MSG_ERR_LTA_RATE_LIMITED,
    MSG_ERR_LTA_TIMEOUT,
    VALID_LINE_CODES,
    error,
    header,
    msg_err_invalid_line_code,
    msg_err_lta_endpoint_not_found,
)

LINE_NAMES = {
    "NSL": "North-South Line",
    "EWL": "East-West Line",
    "CCL": "Circle Line",
    "DTL": "Downtown Line",
    "TEL": "Thomson-East Coast Line",
    "NEL": "North East Line",
    "BPLRT": "Bukit Panjang LRT",
    "SKLRT": "Sengkang LRT",
    "PGLRT": "Punggol LRT",
}


def _lta_error(exc: UpstreamError) -> str:
    if isinstance(exc, LTAAuthFailed):
        return error(ERR_LTA_AUTH_FAILED, MSG_ERR_LTA_AUTH_FAILED)
    if isinstance(exc, LTARateLimited):
        return error(ERR_LTA_RATE_LIMITED, MSG_ERR_LTA_RATE_LIMITED)
    if isinstance(exc, LTAEndpointNotFound):
        return error(
            ERR_LTA_ENDPOINT_NOT_FOUND,
            msg_err_lta_endpoint_not_found(exc.path),
        )
    return error(ERR_LTA_TIMEOUT, MSG_ERR_LTA_TIMEOUT)


def register_train_tools(mcp, lta: LTAClient) -> None:
    @mcp.tool()
    async def get_train_alerts(line: str | None = None) -> str:
        """Get current MRT/LRT service disruption alerts in Singapore.

        Returns affected lines, stations, disruption message, and
        available alternative transport. Optionally filter by line code:
        NSL, EWL, CCL, DTL, TEL, NEL, BPLRT, SKLRT, PGLRT.
        """
        line_u = line.upper() if line else None
        if line_u is not None and line_u not in VALID_LINE_CODES:
            return error(ERR_INVALID_LINE_CODE, msg_err_invalid_line_code(line_u))

        try:
            data = await lta.get_train_alerts()
        except UpstreamError as exc:
            return _lta_error(exc)

        value = data.get("value", {}) or {}
        if value.get("Status") == 1:
            if line_u is not None:
                return header("get_train_alerts", f"{line_u} operating normally.")
            return header(
                "get_train_alerts", "All MRT and LRT lines operating normally."
            )

        segments = value.get("AffectedSegments", []) or []
        messages = value.get("Message", []) or []
        if line_u is not None:
            segments = [s for s in segments if s.get("Line") == line_u]
            if not segments and not messages:
                return header("get_train_alerts", f"{line_u} operating normally.")

        if not segments and not messages:
            return header("get_train_alerts", "No active disruption alerts.")

        disrupted_count = len({s.get("Line", "") for s in segments if s.get("Line")})
        summary = (
            f"{disrupted_count} lines reporting disruptions"
            if disrupted_count
            else "Disruption alerts"
        )
        out = [header("get_train_alerts", summary), ""]
        for s in segments:
            code = s.get("Line", "?")
            name = LINE_NAMES.get(code, code)
            out.append(f"{name} ({code})")
            if s.get("Direction"):
                out.append(f"  Direction: {s['Direction']}")
            if s.get("Stations"):
                out.append(f"  Stations : {s['Stations']}")
            if s.get("FreePublicBus"):
                out.append(f"  Free bus : {s['FreePublicBus']}")
            if s.get("MRTShuttle"):
                out.append(f"  Shuttle  : {s['MRTShuttle']}")
            out.append("")
        for m in messages:
            content = m.get("Content", "")
            if content:
                out.append(f"Message: {content}")
                out.append("")
        return "\n".join(out).rstrip()
