"""The Judge: combines the deterministic rules, the optional model reviewer, and one revision pass.

  rules    -> can reject or ask for a revision (free, instant, repeatable)
  reviewer -> can only ask for a revision (a small local model must not delete good work)
  revise   -> rewrites a row once; kept only if the rules re-check passes

Used by the generate flow (rows just written) and by the standalone Judge agent (saved or given rows).
"""
from collections import Counter
from dataclasses import dataclass, field

from agent import config
from agent.judge import llm
from agent.judge.rules import Issue, check_row, kind_of, rank_key, row_id, score_and_verdict
from agent.llm import ollama
from agent.prompts.loader import load

MAX_REVISE = 6


@dataclass
class Review:
    id: str
    kind: str
    verdict: str = "pass"
    score: float = 5.0
    rules_score: float = 5.0
    issues: list[Issue] = field(default_factory=list)
    scores: dict | None = None
    suggestion: str = ""
    revised: bool = False
    original: dict | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "verdict": self.verdict, "score": self.score, "issues": [i.to_dict() for i in self.issues],
                "scores": self.scores, "suggestion": self.suggestion, "revised": self.revised,
                **({"original": self.original} if self.original else {})}


def _usage(*parts: dict) -> dict:
    out = {"tokens_in": 0, "tokens_out": 0, "model_calls": 0}
    for p in parts:
        for k in out:
            out[k] += int(p.get(k, 0) or 0)
    return out


def review_rules(rows: list[dict], *, existing: list[dict] | None = None, feedback=None, allowed_names: set[str] | None = None) -> list[Review]:
    """Deterministic review. Returns one Review per row, in the rows' original order."""
    reviews: list[Review | None] = [None] * len(rows)
    ranked = sorted(range(len(rows)), key=lambda i: rank_key(rows[i]))
    kept: list[dict] = []
    for i in ranked:
        issues = check_row(rows[i], earlier=kept, existing=existing, feedback=feedback, allowed_names=allowed_names)
        score, verdict = score_and_verdict(issues)
        reviews[i] = Review(id=row_id(rows[i]), kind=kind_of(rows[i]), verdict=verdict, score=score, rules_score=score, issues=issues)
        if verdict != "reject":
            kept.append(rows[i])  # a rejected duplicate must not shield rows that follow it
    return [r for r in reviews if r is not None]


def apply_rubric(rows: list[dict], reviews: list[Review]) -> tuple[dict, list[str]]:
    """Adds the model reviewer's scores. Returns (usage, errors). Never rejects; only asks for revisions."""
    t = load()["judge"]["thresholds"]
    candidates = [i for i in range(len(rows)) if reviews[i].verdict != "reject"]
    candidates.sort(key=lambda i: rank_key(rows[i]))
    candidates = candidates[: config.JUDGE_MAX_LLM_ROWS]
    usage, errors = [], []
    for start in range(0, len(candidates), config.JUDGE_BATCH_SIZE):
        batch = candidates[start:start + config.JUDGE_BATCH_SIZE]
        try:
            parsed, u = llm.review_batch([rows[i] for i in batch])
        except ollama.LLMError as e:
            errors.append(f"The model reviewer was unavailable ({e}); only the rule checks were applied to the rest.")
            break
        usage.append(u)
        for i in batch:
            got = parsed.get(reviews[i].id)
            if not got:
                continue
            r = reviews[i]
            r.scores, r.suggestion = got["scores"], got["suggestion"]
            nums = [v for v in got["scores"].values() if v is not None]
            avg = sum(nums) / len(nums) if nums else None
            grounded = got["scores"].get("grounded")
            if grounded is not None and grounded <= 2:
                r.issues.append(Issue("ungrounded_claims", "revise", got["issues"][0] if got["issues"] else "the reviewer found claims the evidence does not support"))
            elif avg is not None and avg < t["pass_score"]:
                r.issues.append(Issue("low_quality_score", "revise", f"the reviewer scored it {avg:.1f}/5 overall"))
            for note in got["issues"][:3]:
                r.issues.append(Issue("reviewer_note", "note", note))
            _, r.verdict = score_and_verdict(r.issues)
            r.score = round((r.rules_score + avg) / 2, 2) if avg is not None else r.rules_score
    return _usage(*usage), errors


def revise_rows(rows: list[dict], reviews: list[Review], *, existing=None, feedback=None, allowed_names=None) -> tuple[list[dict], dict, list[str]]:
    """One rewrite pass for rows that need revising. A rewrite is kept only if the rules re-check passes."""
    usage, errors, rows = [], [], list(rows)
    todo = [i for i in range(len(rows)) if reviews[i].verdict == "revise"][:MAX_REVISE]
    for i in todo:
        problems = [x.message for x in reviews[i].issues if x.severity == "revise"]
        try:
            new, u = llm.rewrite(rows[i], problems)
        except ollama.LLMError as e:
            errors.append(f"Revision was unavailable ({e}).")
            break
        usage.append(u)
        if not new:
            continue
        candidate = {**rows[i], **new}
        others = [r for j, r in enumerate(rows) if j != i and reviews[j].verdict != "reject"]
        issues = check_row(candidate, earlier=others, existing=existing, feedback=feedback, allowed_names=allowed_names)
        score, verdict = score_and_verdict(issues)
        if verdict == "pass":
            r = reviews[i]
            r.original = {"title": rows[i].get("title", ""), "rationale": rows[i].get("rationale", "")}
            r.issues, r.verdict, r.score, r.rules_score, r.revised = issues, "pass", score, score, True
            rows[i] = candidate
    return rows, _usage(*usage), errors


def allowed_names(domain: str, brand_ctx: dict | None) -> set[str]:
    """Names a recommendation may mention without being flagged: the brand, its tracked competitors
    (the profile stores them like 'usermaven.com[usermaven]'), and the brand domain itself."""
    import re

    out: set[str] = set()
    d = (domain or "").lower().removeprefix("www.")
    if d:
        out |= {d, d.split(".")[0]}
    for raw in (brand_ctx or {}).get("named_competitors") or []:
        for piece in re.split(r"[\[\]\s,;]+", str(raw).lower()):
            piece = piece.strip()
            if piece:
                out.add(piece.removeprefix("www."))
                if "." in piece:
                    out.add(piece.split(".")[0])
    return out


def attach(rows: list[dict], reviews: list[Review]) -> list[dict]:
    """Rows with their review under `quality`."""
    return [{**r, "quality": rv.to_dict()} for r, rv in zip(rows, reviews)]


def summarise(reviews: list[Review]) -> dict:
    counts = Counter(r.verdict for r in reviews)
    codes = Counter(i.code for r in reviews for i in r.issues if i.severity != "note")
    return {
        "reviewed": len(reviews), "pass": counts["pass"], "revise": counts["revise"], "reject": counts["reject"],
        "revised": sum(1 for r in reviews if r.revised),
        "avg_score": round(sum(r.score for r in reviews) / len(reviews), 2) if reviews else None,
        "top_issues": [{"code": c, "count": n} for c, n in codes.most_common(5)],
    }
