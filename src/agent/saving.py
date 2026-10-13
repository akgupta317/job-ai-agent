"""Saves recommendations through the ThriveStack MCP tool `save_content_recommendations`.

The agent has no database. The MCP tool calls ai-visibility-api's import endpoint with the caller's
own token, so the user can only save to brands they can access. The API re-checks every
citation-backed row against the probe run and takes the evidence (priority, scores, counts,
competitors, cited page) from its own analysis, so only the wording and a few choices go over.
"""
from agent.tools import thrivestack_mcp as mcp

SAVE_TOOL = "save_content_recommendations"
MAX_ROWS_PER_CALL = 30  # the API and the MCP tool both enforce this

# The only fields the API accepts from a caller. Everything else a generate step returns
# (scores, citation counts, competitors, cited page) is evidence the server recomputes.
SAVE_FIELDS = (
    "channel_type", "prompt_id", "evidence_type", "title", "rationale",
    "role", "content_type", "topic", "priority", "source_type",
)
_COUNTS = ("created", "updated", "unchanged", "rejected")


def pick_fields(row: dict) -> dict:
    return {k: row[k] for k in SAVE_FIELDS if k in row and row[k] is not None}


def _merge(parts: list[dict], domain: str, run_id: str | None) -> dict:
    merged: dict = {"domain": domain, "probe_run_id": run_id or "", **{k: 0 for k in _COUNTS}, "results": []}
    for part in parts:
        for k in _COUNTS:
            merged[k] += int(part.get(k, 0) or 0)
        merged["results"].extend(part.get("results") or [])
        merged["probe_run_id"] = part.get("probe_run_id") or merged["probe_run_id"]
    return merged


async def save_recommendations(token: str, domain: str, run_id: str | None, rows: list[dict]) -> dict:
    """Saves rows as 'suggested' recommendations. Raises McpToolError if the API refuses the call."""
    payload = [pick_fields(r) for r in rows]
    parts = []
    for start in range(0, len(payload), MAX_ROWS_PER_CALL):
        args: dict = {"domain": domain, "recommendations": payload[start:start + MAX_ROWS_PER_CALL]}
        if run_id:
            args["probe_run_id"] = run_id
        result = await mcp.call_tool(token, SAVE_TOOL, args)
        if not isinstance(result, dict):
            raise mcp.McpToolError(f"MCP tool {SAVE_TOOL} returned an unexpected reply")
        parts.append(result)
    return _merge(parts, domain, run_id)
