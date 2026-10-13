import json
import threading

import httpx

from agent import config


class LLMError(Exception):
    pass


class InvalidJSON(LLMError):
    pass


class LLMBusy(Exception):
    """The one model slot stayed taken for LLM_WAIT_S. Deliberately not an LLMError: the API answers 429, not 502."""


# Hard limits for EVERY model call, whoever makes it. The model runs on CPU, one call at a time:
# letting requests pile up behind it is how a few cheap requests turn into an outage.
_SLOT = threading.BoundedSemaphore(1)


def _prompt_chars(payload: dict) -> int:
    return sum(len(m.get("content", "")) for m in payload.get("messages", []))


def _post(payload: dict) -> dict:
    if _prompt_chars(payload) > config.MAX_PROMPT_CHARS:
        raise LLMError("Prompt too large for the model")
    if not _SLOT.acquire(timeout=config.LLM_WAIT_S):
        raise LLMBusy("The model is busy with another request. Try again in a minute.")
    try:
        return _send(payload)
    finally:
        _SLOT.release()


def _send(payload: dict) -> dict:
    try:
        resp = httpx.post(f"{config.OLLAMA_BASE_URL}/api/chat", json=payload, timeout=config.OLLAMA_TIMEOUT_S)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama request failed: {type(e).__name__}") from e
    return resp.json()


def _usage(body: dict) -> dict:
    return {"tokens_in": body.get("prompt_eval_count", 0), "tokens_out": body.get("eval_count", 0)}


def chat(prompt: str, system: str | None = None, model: str | None = None) -> tuple[str, dict]:
    """Plain-text reply. Returns (text, usage)."""
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = _post({
        "model": model or config.OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": config.OLLAMA_TEMPERATURE, "num_ctx": config.OLLAMA_NUM_CTX},
    })
    return body["message"]["content"], _usage(body)


def chat_json(prompt: str, model: str | None = None) -> tuple[dict, dict]:
    """JSON reply (Ollama JSON mode). Retries once if the reply is not valid JSON."""
    total = {"tokens_in": 0, "tokens_out": 0}
    last = ""
    for _ in range(2):
        body = _post({
            "model": model or config.OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": config.OLLAMA_TEMPERATURE, "num_ctx": config.OLLAMA_NUM_CTX},
        })
        u = _usage(body)
        total = {k: total[k] + u[k] for k in total}
        last = body["message"]["content"]
        try:
            parsed = json.loads(last)
            if isinstance(parsed, dict):
                return parsed, total
        except json.JSONDecodeError:
            pass
    raise InvalidJSON("Model did not return valid JSON")


def is_up() -> bool:
    try:
        return httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False
