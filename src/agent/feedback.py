"""The feedback loop: what the team decided about earlier recommendations, fed back into new ones.

There is no separate feedback store. The signal already exists in the product: a recommendation the
team moves to to-do or approved is accepted, one they dismiss is rejected. This module reads those
decisions through the ThriveStack MCP (the caller's own token, so only their brand) and turns them into:

  * guidance for the writer prompts (what to avoid, what they act on),
  * a similarity check for the Judge (do not propose what was already dismissed),
  * a summary for people (`GET /v1/feedback`).

What it cannot do yet: learn *why* something was dismissed. The API stores no reason, only the status.
A pattern only counts after FEEDBACK_MIN_DECIDED decisions, so a couple of clicks cannot skew anything.
"""
from collections import Counter

from agent import config
from agent.judge.text import clean_line, similarity
from agent.prompts import loader
from agent.prompts.loader import fill
from agent.tools import thrivestack_mcp as mcp

ACCEPTED = ("todo", "approved")
KIND_OF = {"onsite": "owned", "earned": "earned"}
TOOLS = {"owned": "get_owned_content_recommendations", "earned": "get_earned_content_recommendations"}

# How a grouping of decisions is described to the writer. Order = which pattern is shown first.
GROUPS = (
    ("content_type", lambda v, kind: f'"{v}" ideas'),
    ("role", lambda v, kind: f"ideas owned by {v}"),
    ("source_type", lambda v, kind: f"{v} targets"),
    ("evidence_type", lambda v, kind: "ideas based only on the brand profile" if v == "category_inferred" else "citation-backed ideas"),
    ("priority", lambda v, kind: f"{v} priority ideas"),
)


def _sentence(text: str) -> str:
    """Capitalise the first letter only (str.capitalize() would lowercase names like "Listicle")."""
    return text[:1].upper() + text[1:]


def _kind(rec: dict) -> str:
    return KIND_OF.get(rec.get("channel_type", ""), "owned")


class Feedback:
    def __init__(self, active: list[dict], dismissed: list[dict], error: str | None = None):
        self.error = error
        self.suggested = [r for r in active if r.get("status") == "suggested"]
        self.accepted = [r for r in active if r.get("status") in ACCEPTED]
        self.dismissed = [r for r in dismissed if r.get("status", "dismissed") == "dismissed"]

    @property
    def decided(self) -> int:
        return len(self.accepted) + len(self.dismissed)

    def _recent(self, rows: list[dict], kind: str | None) -> list[dict]:
        pool = [r for r in rows if kind is None or _kind(r) == kind]
        return sorted(pool, key=lambda r: str(r.get("updated_at") or ""), reverse=True)

    def similar_dismissed(self, title: str, kind: str | None = None) -> tuple[str, float] | None:
        """The most similar dismissed title (and how similar), or None when nothing was dismissed."""
        best: tuple[str, float] | None = None
        for r in self._recent(self.dismissed, kind):
            s = similarity(title, r.get("title", ""))
            if best is None or s > best[1]:
                best = (r.get("title", ""), s)
        return best if best and best[1] > 0 else None

    def patterns(self, kind: str) -> list[dict]:
        """Groups with enough decisions and a lopsided outcome (>= 75% one way)."""
        rows = [(r, True) for r in self.accepted if _kind(r) == kind] + [(r, False) for r in self.dismissed if _kind(r) == kind]
        out: list[dict] = []
        for field, describe in GROUPS:
            counts: dict[str, Counter] = {}
            for r, ok in rows:
                v = r.get(field)
                if v:
                    counts.setdefault(str(v), Counter())["accepted" if ok else "dismissed"] += 1
            for value, c in counts.items():
                n = c["accepted"] + c["dismissed"]
                if n < config.FEEDBACK_MIN_DECIDED:
                    continue
                if c["dismissed"] / n >= 0.75:
                    out.append({"field": field, "value": value, "outcome": "dismissed", "dismissed": c["dismissed"], "decided": n,
                                "text": f"{_sentence(describe(value, kind))} are usually dismissed ({c['dismissed']} of {n} decided)."})
                elif c["accepted"] / n >= 0.75:
                    out.append({"field": field, "value": value, "outcome": "accepted", "accepted": c["accepted"], "decided": n,
                                "text": f"{_sentence(describe(value, kind))} are usually accepted ({c['accepted']} of {n} decided)."})
        return out[:3]

    def guidance(self, kind: str) -> str:
        """A block for the writer prompt, or '' when there is nothing to learn from yet.
        Everything in it is stored data, cleaned to one line each and framed as data, not instructions."""
        g = loader.load()["shared"]["feedback_guidance"]
        dismissed = self._recent(self.dismissed, kind)[: config.FEEDBACK_MAX_DISMISSED]
        accepted = self._recent(self.accepted, kind)[: config.FEEDBACK_MAX_ACCEPTED]
        patterns = self.patterns(kind)
        if not (dismissed or accepted or patterns):
            return ""
        out = g["intro"]
        if dismissed:
            out += g["dismissed_intro"] + "".join(fill(g["dismissed_item"], title=clean_line(r.get("title", ""), 120)) for r in dismissed)
        if accepted:
            out += g["accepted_intro"] + "".join(
                fill(g["accepted_item"], title=clean_line(r.get("title", ""), 120), rationale=clean_line(r.get("rationale", ""), 140))
                for r in accepted
            )
        if patterns:
            out += g["patterns_intro"] + "".join(fill(g["pattern_item"], line=p["text"]) for p in patterns)
        return out + "\n"

    def summary(self) -> dict:
        def titles(kind: str, rows: list[dict]) -> list[str]:
            return [clean_line(r.get("title", ""), 120) for r in self._recent(rows, kind)[:5]]

        return {
            "suggested": len(self.suggested),
            "accepted": len(self.accepted),
            "dismissed": len(self.dismissed),
            "decided": self.decided,
            "active": self.decided > 0 and self.error is None,
            "min_decided_for_patterns": config.FEEDBACK_MIN_DECIDED,
            "patterns": {k: [p["text"] for p in self.patterns(k)] for k in ("owned", "earned")},
            "recent_dismissed": {k: titles(k, self.dismissed) for k in ("owned", "earned")},
            "recent_accepted": {k: titles(k, self.accepted) for k in ("owned", "earned")},
            "note": self.error or (
                "No decisions yet: nothing is dismissed, approved or on a to-do list, so there is nothing to learn from."
                if self.decided == 0 else "Learning from the decisions above."
            ),
        }


async def _fetch(token: str, kind: str, domain: str, status: str | None) -> list[dict]:
    args: dict = {"domain": domain, "limit": 200}
    if status:
        args["status"] = status
    data = await mcp.call_tool(token, TOOLS[kind], args)
    return data.get("recommendations", []) if isinstance(data, dict) else []


async def load(token: str, domain: str, active_owned: list[dict] | None = None, active_earned: list[dict] | None = None) -> Feedback:
    """Reads the team's decisions. Never raises: a failure returns an empty Feedback that says why,
    so a feedback problem cannot stop recommendations from being generated."""
    try:
        if active_owned is None:
            active_owned = await _fetch(token, "owned", domain, None)
        if active_earned is None:
            active_earned = await _fetch(token, "earned", domain, None)
        dismissed = [*await _fetch(token, "owned", domain, "dismissed"), *await _fetch(token, "earned", domain, "dismissed")]
        return Feedback([*active_owned, *active_earned], dismissed)
    except Exception:
        return Feedback([], [], error="Could not read the team's earlier decisions, so none were used.")
