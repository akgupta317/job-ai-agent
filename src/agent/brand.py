"""Loads the saved Brand Kit from the ThriveStack MCP and maps it to the prompt context."""
from agent.tools import thrivestack_mcp as mcp


def to_context(info: dict) -> dict:
    """Maps get_brand_information output to the fields the prompts use."""
    return {
        "category": info.get("industry") or "",
        "value_offering": info.get("value_offering") or "",
        "named_competitors": info.get("competitors") or [],
        "description": info.get("description") or "",
        "products_and_services": info.get("products_and_services") or [],
        "brand_identity": info.get("brand_identity") or [],
        "business_model": info.get("business_model") or "",
        "customer_segments": info.get("customer_segments") or [],
        "target_markets": info.get("target_markets") or [],
        "language": info.get("language") or "",
        "ai_topics": info.get("ai_topics") or [],
        "intent_informational_pct": info.get("intent_informational") or 0,
        "intent_commercial_pct": info.get("intent_commercial") or 0,
        "intent_transactional_pct": info.get("intent_transactional") or 0,
    }


async def load_context(token: str, domain: str) -> dict:
    info = await mcp.call_tool(token, "get_brand_information", {"domain": domain})
    return to_context(info) if isinstance(info, dict) else {}
