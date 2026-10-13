from fastapi import Header, HTTPException

from agent import config


def get_token(authorization: str | None = Header(default=None)) -> str:
    """Token of the calling user; forwarded to the ThriveStack MCP, never stored or logged."""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    if config.ALLOW_ENV_TOKEN and config.DEV_MCP_TOKEN:
        return config.DEV_MCP_TOKEN
    raise HTTPException(401, "Missing Authorization: Bearer <token> header")
