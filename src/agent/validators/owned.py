"""Checks model output for owned recommendations. Port of validateAndMerge* in generate.go
and category_generate.go. Returns (rows, dropped); never raises on a bad entry."""
from agent.gaps.owned import priority_from_gap_score
from agent.prompts.loader import enums

REPAIRABLE = ("no entry", "outside")


def _by_key(replies: list, key: str) -> dict:
    return {r.get(key): r for r in replies if isinstance(r, dict)}


def names_foreign_competitor(rationale: str, own: set[str], all_names: set[str]) -> bool:
    lower = (rationale or "").lower()
    return any(n.lower() in lower for n in all_names if n not in own and len(n) >= 3)


def validate_owned_citation(signals: list[dict], replies: list) -> tuple[list[dict], list[dict]]:
    e = enums()
    by_prompt = _by_key(replies, "prompt_id")
    all_names = {c["name"] for s in signals for c in s["competitors"] if c.get("name")}
    rows, dropped = [], []
    for s in signals:
        pid = s["prompt_id"]
        rec = by_prompt.get(pid)
        if rec is None:
            dropped.append({"id": pid, "reason": "model reply had no entry for this prompt_id"})
            continue
        if rec.get("is_duplicate"):
            dropped.append({"id": pid, "reason": "model flagged is_duplicate"})
            continue
        own = {c["name"] for c in s["competitors"]}
        if names_foreign_competitor(rec.get("rationale", ""), own, all_names):
            dropped.append({"id": pid, "reason": "rationale named a competitor outside this candidate's own evidence"})
            continue

        channel = rec.get("channel") if rec.get("channel") in e["owned_channels"] else e["owned_fallbacks"]["channel"]
        role = rec.get("role") if rec.get("role") in e["owned_roles"] else e["owned_fallbacks"]["role"]
        ctype = rec.get("content_type") if rec.get("content_type") in e["owned_content_types"] else e["owned_fallbacks"]["content_type"]
        title = (rec.get("title") or "").strip() or s["prompt_text"]
        rows.append({
            "prompt_id": pid, "result_id": s["result_id"], "topic": s["topic"], "title": title,
            "source": "citation_gap", "severity_tier": s["severity_tier"], "gap_score": s["gap_score"],
            "priority": priority_from_gap_score(s["gap_score"]),
            "channel": channel, "role": role,
            "our_citations": s["our_citations"], "total_citations": s["total_citations"],
            "competitors_winning": [c["name"] for c in s["competitors"]],
            "rationale": rec.get("rationale", ""), "status": "suggested",
            "channel_type": "onsite", "content_type": ctype, "evidence_type": "citation_backed",
        })
    return rows, dropped


def validate_owned_category(cands: list[dict], replies: list, category: str) -> tuple[list[dict], list[dict]]:
    e = enums()
    by_prompt = _by_key(replies, "prompt_id")
    rows, dropped = [], []
    for c in cands:
        pid = c["prompt_id"]
        rec = by_prompt.get(pid)
        if rec is None:
            dropped.append({"id": pid, "reason": "model reply had no entry for this prompt_id"})
            continue
        if rec.get("is_duplicate"):
            dropped.append({"id": pid, "reason": "model flagged is_duplicate"})
            continue
        if not (rec.get("title") or "").strip():
            dropped.append({"id": pid, "reason": "model returned an empty title (nothing to fall back to for category-based ideas)"})
            continue
        rows.append({
            "prompt_id": pid, "topic": (rec.get("topic") or "").strip() or category, "title": rec["title"],
            "source": "category_content",
            "priority": rec.get("priority") if rec.get("priority") in e["priorities"] else "MED",
            "channel": rec.get("channel") if rec.get("channel") in e["owned_channels"] else e["owned_fallbacks"]["channel"],
            "role": rec.get("role") if rec.get("role") in e["owned_roles"] else e["owned_fallbacks"]["role"],
            "rationale": rec.get("rationale", ""), "status": "suggested", "channel_type": "onsite",
            "evidence_type": "category_inferred", "competitors_winning": [],
            "content_type": rec.get("content_type") if rec.get("content_type") in e["owned_content_types"] else e["owned_fallbacks"]["content_type"],
        })
    return rows, dropped
