import json
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agent import config


class McpToolError(RuntimeError):
    """The MCP tool ran and reported an error (for example the API rejected the request).

    The text comes from the MCP/API, never from our request headers. Retrying cannot fix it.
    """


@asynccontextmanager
async def _session(token: str):
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(config.THRIVESTACK_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_tools(token: str) -> list[dict]:
    async with _session(token) as s:
        result = await s.list_tools()
        return [{"name": t.name, "description": t.description} for t in result.tools]


async def call_tool(token: str, name: str, arguments: dict, retries: int = 1) -> object:
    for attempt in range(retries + 1):
        try:
            return await _call_tool_once(token, name, arguments)
        except McpToolError:
            raise  # a tool-reported error is deterministic: do not retry
        except Exception:
            if attempt == retries:
                raise


async def _call_tool_once(token: str, name: str, arguments: dict) -> object:
    async with _session(token) as s:
        result = await s.call_tool(name, arguments)
        text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if result.isError:
            raise McpToolError(f"MCP tool {name} failed: {text[:500]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
