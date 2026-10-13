"""Checks model output for earned recommendations. Port of validateAndMergeEarned{A,B}."""
from agent.gaps.taxonomy import REFERENCE_DOMAINS, UGC_DOMAINS, UGC, REFERENCE, channel_for_domain
from agent.prompts.loader import enums


def _by_cluster(replies: list) -> dict:
    return {r.get("cluster_id"): r for r in replies if isinstance(r, dict)}


def names_ungrounded_domain(text: str, own: list[str], all_domains: set[str]) -> bool:
    lower = (text or "").lower()
    return any(d in lower for d in all_domains if d not in own and len(d) >= 4)


def names_ungrounded_platform(text: str, own_allowed: set[str], all_platforms: set[str], competitors: set[str]) -> bool:
    lower = (text or "").lower()
    return any(p in lower for p in all_platforms if p not in own_allowed and p not in competitors and len(p) >= 4)


def _own_allowed(source_type: str) -> set[str]:
    return UGC_DOMAINS if source_type == UGC else REFERENCE_DOMAINS if source_type == REFERENCE else set()


def _row(c: dict, rec: dict, channel: str, topic: str) -> dict:
    return {
        "prompt_id": c["id"], "result_id": "", "topic": topic, "title": rec["title"],
        "source": "earned_media", "severity_tier": c["tier"], "gap_score": c["source_usage"],
        "priority": rec["priority"], "channel": channel, "role": rec["role"],
        "our_citations": c["our_citations"], "total_citations": c["total_citations"],
        "competitors_winning": list(c["evidence_domains"]), "rationale": rec.get("rationale", ""),
        "status": "suggested", "channel_type": "earned", "source_type": c["source_type"],
        "evidence_type": c["evidence_type"], "content_type": rec["content_type"],
        "source_url": c["evidence_pages"][0]["uri"] if c["evidence_pages"] else "",
    }


def validate_earned_citation(cands: list[dict], replies: list) -> tuple[list[dict], list[dict]]:
    e = enums()
    by_id = _by_cluster(replies)
    all_domains = {d for c in cands for d in c["evidence_domains"]}
    rows, dropped = [], []
    for c in cands:
        rec = by_id.get(c["id"])
        if rec is None:
            dropped.append({"id": c["id"], "reason": "model reply had no entry for this cluster_id"})
            continue
        if rec.get("is_duplicate"):
            dropped.append({"id": c["id"], "reason": "model flagged is_duplicate"})
            continue
        if names_ungrounded_domain(f"{rec.get('title', '')} {rec.get('rationale', '')}", c["evidence_domains"], all_domains):
            dropped.append({"id": c["id"], "reason": "title or rationale named a domain outside this candidate's own evidence"})
            continue
        rec = {
            "title": (rec.get("title") or "").strip() or f"{c['source_type']} — {c['topic']}",
            "rationale": rec.get("rationale", ""),
            "role": rec.get("role") if rec.get("role") in e["earned_roles"] else "PR",
            "priority": rec.get("priority") if rec.get("priority") in e["priorities"] else ("HIGH" if c["tier"] == "high" else "MED"),
            "content_type": rec.get("content_type") if rec.get("content_type") in e["earned_content_types"]
            else e["earned_content_type_fallback_by_source_type"][c["source_type"]],
        }
        channel = channel_for_domain(c["evidence_domains"][0]) if c["evidence_domains"] else ""
        rows.append(_row(c, rec, channel, c["topic"]))
    return rows, dropped


def validate_earned_category(cands: list[dict], replies: list, category: str, named_competitors: list[str]) -> tuple[list[dict], list[dict]]:
    e = enums()
    by_id = _by_cluster(replies)
    competitors = {n.lower() for n in named_competitors}
    all_platforms = set(UGC_DOMAINS) | set(REFERENCE_DOMAINS)
    rows, dropped = [], []
    for c in cands:
        rec = by_id.get(c["id"])
        if rec is None:
            dropped.append({"id": c["id"], "reason": "model reply had no entry for this cluster_id"})
            continue
        if rec.get("is_duplicate"):
            dropped.append({"id": c["id"], "reason": "model flagged is_duplicate"})
            continue
        if names_ungrounded_platform(f"{rec.get('title', '')} {rec.get('rationale', '')}",
                                     _own_allowed(c["source_type"]), all_platforms, competitors):
            dropped.append({"id": c["id"], "reason": "title or rationale named a platform outside this candidate's own source type"})
            continue
        if not (rec.get("title") or "").strip():
            dropped.append({"id": c["id"], "reason": "model returned an empty title (nothing to fall back to for category-based ideas)"})
            continue
        rec2 = {
            "title": rec["title"], "rationale": rec.get("rationale", ""),
            "role": rec.get("role") if rec.get("role") in e["earned_roles"] else "PR",
            "priority": rec.get("priority") if rec.get("priority") in ("HIGH", "MED") else "MED",
            "content_type": rec.get("content_type") if rec.get("content_type") in e["earned_content_types"]
            else e["earned_content_type_fallback_by_source_type"][c["source_type"]],
        }
        topic = (rec.get("topic") or "").strip() or category
        rows.append(_row(c, rec2, e["earned_default_channel_by_source_type"][c["source_type"]], topic))
    return rows, dropped
