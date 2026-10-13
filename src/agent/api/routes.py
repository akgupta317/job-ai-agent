from fastapi import APIRouter, Depends, HTTPException

from agent import config
from agent.api.auth import get_token
from agent.llm import ollama
from agent.tools import thrivestack_mcp

router = APIRouter()


@router.get("/hello")
def hello() -> str:
    return "hello"


@router.get("/health")
def health() -> str:
    return "ok"


@router.get("/llm/ping")
def llm_ping() -> dict:
    """Checks the agent can reach Ollama and get a reply."""
    if not ollama.is_up():
        raise HTTPException(503, f"Ollama not reachable at {config.OLLAMA_BASE_URL}")
    try:
        reply, _usage = ollama.chat("Reply with the single word: pong")
    except Exception as e:
        raise HTTPException(502, f"Ollama call failed: {e}")
    return {"model": config.OLLAMA_MODEL, "reply": reply.strip()}


async def _mcp(token: str, name: str, arguments: dict) -> object:
    try:
        return await thrivestack_mcp.call_tool(token, name, arguments)
    except Exception:
        # Deliberately generic: never echo anything that could contain the token.
        raise HTTPException(502, "ThriveStack MCP call failed")


@router.get("/mcp/tools")
async def mcp_tools(token: str = Depends(get_token)) -> list[dict]:
    try:
        return await thrivestack_mcp.list_tools(token)
    except Exception:
        raise HTTPException(502, "ThriveStack MCP call failed")


@router.get("/mcp/brands")
async def mcp_brands(token: str = Depends(get_token)) -> object:
    return await _mcp(token, "get_accessible_brands", {})


@router.get("/mcp/citations")
async def mcp_citations(domain: str, token: str = Depends(get_token)) -> object:
    """Raw citation-gap evidence for a domain's latest probe run."""
    return await _mcp(token, "get_citation_probe_data", {"domain": domain})


@router.get("/gaps")
async def gaps(domain: str, token: str = Depends(get_token)) -> dict:
    """Finds owned and earned content gaps for a domain from its latest probe run.
    No LLM is used here; this is the deterministic evidence step."""
    from collections import Counter

    from agent.gaps.earned import extract_earned_signals
    from agent.gaps.owned import extract_owned_signals

    data = await _mcp(token, "get_citation_probe_data", {"domain": domain})
    if not isinstance(data, dict) or not data.get("has_run"):
        return {"domain": domain, "has_run": False, "owned": [], "earned": []}

    questions = data.get("questions", [])
    owned = extract_owned_signals(questions)
    earned = extract_earned_signals(questions, domain)
    return {
        "domain": domain,
        "has_run": True,
        "run_id": data.get("run_id"),
        "last_probed_at": data.get("last_probed_at"),
        "summary": {
            "questions": len(questions),
            "owned": len(owned),
            "owned_by_tier": dict(Counter(o["severity_tier"] for o in owned)),
            "owned_by_priority": dict(Counter(o["priority"] for o in owned)),
            "earned": len(earned),
            "earned_by_source_type": dict(Counter(e["source_type"] for e in earned)),
            "earned_by_priority": dict(Counter(e["priority"] for e in earned)),
        },
        "owned": owned,
        "earned": earned,
    }
