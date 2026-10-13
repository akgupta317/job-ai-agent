"""Guardrails: keep the agent on its one job (owned + earned content recommendations).

Layers, in the order a question meets them. None can be switched off by a request:

  1. size            question length is capped (API model AND here, so a direct caller cannot skip it)
  2. override/off-task  prompt-injection and "write me code / a joke / trivia" requests are refused BEFORE the model
  3. on-topic        the question must use recommendation vocabulary or the brand's own saved titles/topics
  4. hardened prompt the model is told to answer OUT_OF_SCOPE, and the question is delimited as data
  5. answer check    code, the sentinel, or an answer not tied to any recommendation is replaced by a refusal

Layers 1-3 and 5 are enforced inside the ask graph (graphs/ask/graph.py), the only place a question
reaches the model. The Ollama client adds hard limits that apply to every call (llm/ollama.py).

Honest limit: this is rules plus a prompt, not proof. A clever phrasing can still slip past layer 2/3;
layers 4/5 are what catch it, and the answer check means a slip cannot return code or an off-topic reply.
"""
import logging
import re
from dataclasses import dataclass

from agent import config
from agent.prompts import loader

log = logging.getLogger("agent.guard")

SENTINEL = "OUT_OF_SCOPE"
_CODE_FENCE = re.compile(r"```|~~~")
_REF = re.compile(r"\bR\d+\b")


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""     # too_long | override | off_task | off_topic | bad_answer
    message: str = ""    # what to tell the user when refused


def _g() -> dict:
    return loader.load()["guard"]


def _compile(key: str) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in _g()[key]]


_cache: dict[str, list[re.Pattern]] = {}


def _patterns(key: str) -> list[re.Pattern]:
    if key not in _cache:
        _cache[key] = _compile(key)
    return _cache[key]


def _refuse(reason: str) -> Verdict:
    msg = _g()["refusals"][reason]
    return Verdict(False, reason, msg.replace("<<max>>", str(config.MAX_QUESTION_CHARS)))


def log_refusal(where: str, verdict: Verdict, text: str) -> None:
    """Logs that a refusal happened and why. Never the text itself (it may hold anything)."""
    log.warning("guard refused where=%s reason=%s chars=%d", where, verdict.reason, len(text))


def screen(question: str) -> Verdict:
    """Layers 1-2. Needs no data, so it runs before anything is fetched."""
    q = question.strip()
    if len(q) > config.MAX_QUESTION_CHARS:
        return _refuse("too_long")
    # Normalise so "i g n o r e" style spacing and odd unicode spaces do not hide a pattern.
    flat = re.sub(r"\s+", " ", q.replace(" ", " "))
    if any(p.search(flat) for p in _patterns("override_patterns")):
        return _refuse("override")
    if any(p.search(flat) for p in _patterns("off_task_patterns")):
        return _refuse("off_task")
    return Verdict(True)


def _vocabulary(items: list[dict]) -> set[str]:
    words: set[str] = set()
    for i in items:
        for field in ("title", "topic"):
            words.update(w for w in re.findall(r"[a-z0-9]{5,}", str(i.get(field, "")).lower()))
    return words


def on_topic(question: str, items: list[dict]) -> Verdict:
    """Layer 3. The question must mention recommendation vocabulary, a recommendation id (R3),
    or a word from this brand's own saved titles/topics."""
    q = question.lower()
    if _REF.search(question) or any(t in q for t in _g()["on_topic_terms"]):
        return Verdict(True)
    if _vocabulary(items) & set(re.findall(r"[a-z0-9]{5,}", q)):
        return Verdict(True)
    return _refuse("off_topic")


def harden(question: str) -> str:
    """Layer 4 helper: stop the question from closing the data block it is placed in."""
    return question.strip().replace("QUESTION>>>", "").replace("<<<QUESTION", "")


def check_answer(answer: str, items: list[dict]) -> Verdict:
    """Layer 5. The model's reply must be an answer about the listed recommendations."""
    text = answer.strip()
    if SENTINEL in text:
        return _refuse("off_task")
    if not text or len(text) > config.MAX_ANSWER_CHARS or _CODE_FENCE.search(text):
        return _refuse("bad_answer")
    low = text.lower()
    grounded = _REF.search(text) or any(str(i.get("title", "")).lower() in low for i in items if i.get("title"))
    if not grounded:
        return _refuse("bad_answer")
    return Verdict(True)
