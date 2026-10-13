"""Turns a plain-English message into an action: generate recommendations, or answer a question.
Rule-based on purpose: predictable, free, and easy to test."""
import re

GENERATE_WORDS = ("generate", "create", "write", "suggest", "come up", "give me", "make", "produce", "find")
OWNED_WORDS = ("owned", "on-site", "onsite", "on site", "own site", "our site", "blog", "our website")
SAVE_WORDS = ("save", "store", "persist", "keep them")
JUDGE_WORDS = ("review", "audit", "quality", "judge", "grade", "critique", "validate", "proofread", "how good")
DEEP_WORDS = ("deep", "thorough", "full review", "with the model", "detailed")
FEEDBACK_WORDS = ("dismissed", "approved", "accepted", "team decided", "feedback", "learned", "rejected by")
EARNED_WORDS = ("earned", "off-site", "offsite", "off site", "third-party", "third party", "pr ", "outreach", "press", "backlink")


def _has(text: str, words: tuple) -> bool:
    return any(w in text for w in words)


def parse(message: str) -> dict:
    m = f" {message.lower()} "
    owned, earned = _has(m, OWNED_WORDS), _has(m, EARNED_WORDS)
    asks_to_generate = _has(m, GENERATE_WORDS) and ("recommend" in m or "idea" in m or "content" in m or owned or earned)
    # "which should I do first" is a question even if it mentions "recommendations".
    is_question = _has(m, ("which", "why", "prioriti", "first", "should we", "should i", "most important", "best")) or "?" in m
    if asks_to_generate and not (is_question and not _has(m, ("generate", "create", "write", "suggest", "come up", "produce"))):
        kinds = [k for k, on in (("owned", owned), ("earned", earned)) if on] if (owned != earned) else ["owned", "earned"]
        n = re.search(r"\b(\d{1,2})\b", m)
        return {"intent": "generate", "kinds": kinds, "max_per_side": int(n.group(1)) if n else None,
                "save": _has(m, SAVE_WORDS)}
    about_recs = "recommend" in m or "idea" in m or "suggestion" in m
    if _has(m, FEEDBACK_WORDS) and (about_recs or "team" in m):
        return {"intent": "feedback"}
    if _has(m, JUDGE_WORDS) and about_recs:
        return {"intent": "judge", "mode": "full" if _has(m, DEEP_WORDS) else "rules"}
    # "save these" on its own: the agent keeps no earlier results, so it can only explain how.
    if _has(m, SAVE_WORDS) and not is_question:
        return {"intent": "save_hint"}
    return {"intent": "ask"}
