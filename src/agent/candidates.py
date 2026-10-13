"""Turns raw gaps into the candidates that get written up: ids, dedup against active
recommendations, selection, and category-based filler. Port of the Go dedup and floor logic."""
from agent.gaps.taxonomy import EDITORIAL, INSTITUTIONAL, REFERENCE, UGC

EARNED_CATEGORY_SOURCE_TYPES = [EDITORIAL, UGC, REFERENCE, INSTITUTIONAL]


def earned_id(c: dict) -> str:
    """Stable id for an earned candidate. Matches prompt_id used by ai-visibility-api."""
    if c["evidence_type"] == "category_inferred":
        base = f"earned:category:{c['run_token']}:{c['source_type']}"
        return f"{base}:{c['seq']}" if c["seq"] else base
    if c["evidence_domains"]:
        return f"earned:citation:{c['source_type']}:{c['topic']}:{c['evidence_domains'][0]}"
    return f"earned:citation:{c['source_type']}:{c['topic']}"


def with_ids(earned: list[dict]) -> list[dict]:
    for c in earned:
        c["id"] = earned_id(c)
    return earned


def best_owned_per_prompt(signals: list[dict]) -> list[dict]:
    """One prompt can have several results (e.g. one per AI engine). Keep the strongest."""
    best: dict[str, dict] = {}
    for s in signals:
        cur = best.get(s["prompt_id"])
        if cur is None or (s["gap_score"], s["total_citations"]) > (cur["gap_score"], cur["total_citations"]):
            best[s["prompt_id"]] = s
    return list(best.values())


def sort_owned(signals: list[dict]) -> list[dict]:
    return sorted(signals, key=lambda s: (-s["gap_score"], -s["total_citations"], s["prompt_id"]))


def filter_unchanged_owned(signals: list[dict], active: list[dict]) -> tuple[list[dict], int]:
    """Drop gaps that already have an active recommendation with the same tier and score."""
    by_prompt = {a["prompt_id"]: a for a in active}
    fresh, unchanged = [], 0
    for s in signals:
        a = by_prompt.get(s["prompt_id"])
        if a and a.get("severity_tier") == s["severity_tier"] and a.get("gap_score") == s["gap_score"]:
            unchanged += 1
        else:
            fresh.append(s)
    return fresh, unchanged


def filter_unchanged_earned(cands: list[dict], active: list[dict]) -> tuple[list[dict], int]:
    by_id = {a["prompt_id"]: a for a in active}
    fresh, unchanged = [], 0
    for c in cands:
        a = by_id.get(c["id"])
        if a and a.get("severity_tier") == c["tier"] and a.get("gap_score") == c["source_usage"]:
            unchanged += 1
        else:
            fresh.append(c)
    return fresh, unchanged


def titles_excluding(active: list[dict], batch_ids: set[str], topics: set[str] | None = None,
                     limit: int | None = None) -> list[str]:
    """Titles of active recommendations NOT in this batch, so the model can flag repeats.

    Every title costs the model reading time, and a domain can have 80+ of them (about 80% of
    the prompt). With a limit, titles on the same topic as the batch come first, then the rest
    in their existing order (already sorted by priority), up to the limit.
    """
    pool = [a for a in active if a["prompt_id"] not in batch_ids and a.get("title")]
    if limit is not None:
        topics = topics or set()
        pool = [a for a in pool if a.get("topic") in topics] + [a for a in pool if a.get("topic") not in topics]
        pool = pool[:limit]
    return [a["title"] for a in pool]


def owned_category_ideas(count: int, run_token: str) -> list[dict]:
    return [{"prompt_id": f"category:idea:{run_token}:{i}", "evidence_type": "category_inferred"}
            for i in range(max(0, count))]


def earned_category_ideas(count: int, run_token: str) -> list[dict]:
    out = []
    for i in range(max(0, count)):
        c = {
            "source_type": EARNED_CATEGORY_SOURCE_TYPES[i % len(EARNED_CATEGORY_SOURCE_TYPES)],
            "evidence_type": "category_inferred",
            "seq": i // len(EARNED_CATEGORY_SOURCE_TYPES),
            "run_token": run_token,
            "topic": "", "tier": "", "source_usage": 0, "our_citations": 0, "total_citations": 0,
            "evidence_domains": [], "evidence_pages": [],
        }
        c["id"] = earned_id(c)
        out.append(c)
    return out


def unique_topics(items: list[dict]) -> list[str]:
    seen, out = set(), []
    for i in items:
        t = i.get("topic")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out
