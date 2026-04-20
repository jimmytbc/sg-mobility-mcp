"""MRT and LRT service disruption alerts.

Optional filter by line code (NSL, EWL, CCL, DTL, TEL, NEL, BPLRT,
SKLRT, PGLRT). Returns a clean one-liner when everything is operating
normally — which, happily, is most of the time.

Author: Jimmy Tong
"""

from api.lta import LTAClient

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


def register_train_tools(mcp, lta: LTAClient) -> None:
    @mcp.tool()
    async def get_train_alerts(line: str | None = None) -> str:
        """Get current MRT and LRT service disruption alerts in Singapore.

        Returns affected lines, stations, disruption message, and available
        alternative transport. Optionally filter by line code:
        NSL, EWL, CCL, DTL, TEL, NEL, BPLRT, SKLRT, PGLRT.
        """
        try:
            data = await lta.get_train_alerts()
        except RuntimeError as e:
            return f"Could not fetch train alerts: {e}"

        value = data.get("value", {}) or {}
        if value.get("Status") == 1:
            return "All MRT and LRT lines operating normally."

        segments = value.get("AffectedSegments", []) or []
        messages = value.get("Message", []) or []
        if line:
            line_u = line.upper()
            segments = [s for s in segments if s.get("Line") == line_u]
            if not segments and not messages:
                return f"No disruption alerts for line {line_u}."

        if not segments and not messages:
            return "No active disruption alerts."

        out = ["Train service disruptions:", ""]
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
