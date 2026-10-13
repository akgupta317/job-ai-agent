"""Owned (on-site) gap finding. Port of internal/contentrecs/signals.go.

Pure functions: no network, no LLM. Input is the `questions` list returned by the
ThriveStack MCP tool get_citation_probe_data.
"""

SILENT = "silent"
MENTIONED_UNCITED = "mentioned_uncited"
LOSING_SHARE = "losing_share"

MAX_COMPETITORS = 5
TIER_BASE = {SILENT: 6, MENTIONED_UNCITED: 4, LOSING_SHARE: 2}


def citation_volume_bonus(total: int) -> int:
    if total >= 20:
        return 4
    if total >= 10:
        return 3
    if total >= 5:
        return 2
    if total >= 2:
        return 1
    return 0


def gap_score(tier: str, total_citations: int) -> int:
    return min(10, TIER_BASE[tier] + citation_volume_bonus(total_citations))


def priority_from_gap_score(score: int) -> str:
    if score >= 8:
        return "HIGH"
    if score >= 5:
        return "MED"
    return "LOW"


def _classify(our: int, total: int, our_mentioned: bool, competitor_count: int) -> str | None:
    if our == 0 and not our_mentioned and total > 0:
        return SILENT
    if our == 0 and our_mentioned:
        return MENTIONED_UNCITED
    if our > 0 and our < total and competitor_count > 0:
        return LOSING_SHARE
    return None


def extract_owned_signals(questions: list[dict]) -> list[dict]:
    out = []
    for q in questions:
        brands = q.get("brands") or []
        our_mentioned = any(b.get("is_customer") for b in brands)
        competitors = sorted(
            (b for b in brands if not b.get("is_customer")),
            key=lambda b: b.get("brand_rank", 0),
        )
        our = q.get("our_citations", 0) or 0
        total = q.get("total_citations", 0) or 0

        tier = _classify(our, total, our_mentioned, len(competitors))
        if tier is None:
            continue

        seen, domains = set(), []
        for c in q.get("citations") or []:
            d = c.get("citation_domain")
            if d and d not in seen:
                seen.add(d)
                domains.append(d)

        score = gap_score(tier, total)
        out.append({
            "prompt_id": q.get("prompt_id"),
            "result_id": q.get("result_id"),
            "topic": q.get("topic"),
            "prompt_text": q.get("prompt_text"),
            "severity_tier": tier,
            "gap_score": score,
            "priority": priority_from_gap_score(score),
            "our_citations": our,
            "total_citations": total,
            "citation_domains": domains,
            "competitors": [
                {"name": b.get("name"), "domain": b.get("brand_domain"), "rank": b.get("brand_rank")}
                for b in competitors[:MAX_COMPETITORS]
            ],
            "evidence_type": "citation_backed",
        })
    return out
