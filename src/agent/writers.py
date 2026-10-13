"""Calls the model in small batches, validates each reply, and re-asks once for entries
that failed a grounding check. Duplicates the model flagged are not retried."""
from typing import Callable

from agent.llm import ollama

NO_USAGE = {"tokens_in": 0, "tokens_out": 0, "model_calls": 0, "repaired": 0}


def _add(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in NO_USAGE}


def _repairable(reason: str) -> bool:
    return "no entry" in reason or "outside" in reason


def _one_pass(chunk: list, build: Callable, validate: Callable, note: str) -> tuple[list, list, dict]:
    try:
        obj, u = ollama.chat_json(build(chunk, note))
    except ollama.InvalidJSON:
        return [], [{"id": _id(i), "reason": "model did not return valid JSON"} for i in chunk], {"model_calls": 1}
    replies = obj.get("recommendations")
    rows, dropped = validate(chunk, replies if isinstance(replies, list) else [])
    return rows, dropped, {**u, "model_calls": 1}


def _id(item: dict) -> str:
    return item.get("id") or item["prompt_id"]


def run_batches(items: list[dict], build: Callable, validate: Callable, batch_size: int, repair: bool = True):
    """build(chunk, note) -> prompt text. validate(chunk, replies) -> (rows, dropped)."""
    rows, dropped, usage = [], [], dict(NO_USAGE)
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        r, d, u = _one_pass(chunk, build, validate, "")
        usage = _add(usage, u)

        retry_reasons = {x["id"]: x["reason"] for x in d if _repairable(x["reason"])}
        if repair and retry_reasons:
            retry = [i for i in chunk if _id(i) in retry_reasons]
            note = ("\n\nYour previous answer had problems. Answer again, only for these ids, "
                    "and fix each problem:\n" + "\n".join(f"- {k}: {v}" for k, v in retry_reasons.items()))
            r2, d2, u2 = _one_pass(retry, build, validate, note)
            usage = _add(usage, {**u2, "repaired": len(r2)})
            d = [x for x in d if x["id"] not in retry_reasons] + d2
            r = r + r2
        rows += r
        dropped += d
    return rows, dropped, usage
