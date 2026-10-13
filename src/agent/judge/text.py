"""Small text helpers shared by the Judge and the feedback loop."""
import re

_STOP = {
    "the", "a", "an", "for", "to", "of", "and", "in", "on", "with", "your", "you", "how", "what", "is", "are",
    "do", "does", "we", "our", "i", "my", "it", "at", "by", "or", "vs", "from", "this", "that", "can", "should",
}
_WORD = re.compile(r"[a-z0-9]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def similarity(a: str, b: str) -> float:
    """Word-overlap similarity (Jaccard) between two titles, 0..1."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def clean_line(text: str, limit: int = 160) -> str:
    """One safe line: control characters and newlines removed, whitespace collapsed, truncated.
    Used for anything that comes from a model or from stored data before it goes into a prompt."""
    s = _CONTROL.sub(" ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
