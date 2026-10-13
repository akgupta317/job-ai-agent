"""Earned-media gap finding. Port of internal/contentrecs/earned_signals.go.

Groups citations by topic, classifies each citing domain into a source type, keeps source
types cited at least MIN_SOURCE_USAGE times, and emits one candidate per evidence domain.
"""
from collections import Counter

from agent.gaps.taxonomy import classify_source_type, normalize_domain

MIN_SOURCE_USAGE = 2
MAX_EVIDENCE_DOMAINS = 3


def competitor_domain_set(questions: list[dict]) -> set[str]:
    return {
        normalize_domain(b["brand_domain"])
        for q in questions
        for b in (q.get("brands") or [])
        if not b.get("is_customer") and b.get("brand_domain")
    }


def _top_domains(counts: Counter) -> list[str]:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [d for d, _ in ranked[:MAX_EVIDENCE_DOMAINS]]


def _pages_for_domain(pages: dict, domain: str) -> list[dict]:
    items = [p for p in pages.values() if p["domain"] == domain]
    items.sort(key=lambda p: (-p["count"], p["uri"]))
    return [{"title": p["title"], "uri": p["uri"], "domain": p["domain"]} for p in items[:MAX_EVIDENCE_DOMAINS]]


def _topic_clusters(topic: str, group: list[dict], brand_domain: str, competitors: set[str]) -> list[dict]:
    usage: Counter = Counter()
    domain_counts: dict[str, Counter] = {}
    page_counts: dict[str, dict] = {}
    our_total = total_total = 0

    for q in group:
        our_total += q.get("our_citations", 0) or 0
        total_total += q.get("total_citations", 0) or 0
        for c in q.get("citations") or []:
            source_type = classify_source_type(c.get("citation_domain", ""), brand_domain, competitors)
            if source_type is None:
                continue
            usage[source_type] += 1
            domain = normalize_domain(c["citation_domain"])
            domain_counts.setdefault(source_type, Counter())[domain] += 1

            title, uri = (c.get("title") or "").strip(), (c.get("uri") or "").strip()
            if not title or not uri:
                continue
            pages = page_counts.setdefault(source_type, {})
            if uri not in pages:
                pages[uri] = {"title": c["title"], "uri": c["uri"], "domain": domain, "count": 0}
            pages[uri]["count"] += 1

    out = []
    for source_type, count in usage.items():
        if count < MIN_SOURCE_USAGE:
            continue
        evidence = _top_domains(domain_counts[source_type])
        if not evidence:
            continue
        tier = "high" if our_total == 0 else "medium"
        for domain in evidence:
            out.append({
                "topic": topic,
                "source_type": source_type,
                "tier": tier,
                "priority": "HIGH" if tier == "high" else "MED",
                "evidence_type": "citation_backed",
                "source_usage": count,
                "our_citations": our_total,
                "total_citations": total_total,
                "evidence_domains": [domain],
                "evidence_pages": _pages_for_domain(page_counts.get(source_type, {}), domain),
            })
    return out


def extract_earned_signals(questions: list[dict], brand_domain: str) -> list[dict]:
    competitors = competitor_domain_set(questions)
    by_topic: dict[str, list[dict]] = {}
    for q in questions:
        by_topic.setdefault(q.get("topic"), []).append(q)

    out = []
    for topic, group in by_topic.items():
        out.extend(_topic_clusters(topic, group, brand_domain, competitors))

    # High tier first, then most-cited; topic and domain break ties so output is stable.
    out.sort(key=lambda c: (c["tier"] != "high", -c["source_usage"], c["topic"] or "", c["evidence_domains"][0]))
    return out
