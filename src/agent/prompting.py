"""Builds the text sent to the model from the parts in prompts/prompts.json."""
from agent.prompts.loader import fill, get, load


def _text(part) -> str:
    return part["text"] if isinstance(part, dict) else part


def _join(v) -> str:
    return ", ".join(v) if isinstance(v, (list, tuple)) else str(v)


def _competitors(cs: list[dict]) -> str:
    if not cs:
        return "none"
    out = []
    for c in cs:
        if c.get("domain"):
            out.append(f"{c['name']} ({c['domain']}, rank {c['rank']})")
        else:
            out.append(f"{c['name']} (rank {c['rank']})")
    return ", ".join(out)


def _brand_extras(ctx: dict) -> str:
    lines = load()["shared"]["brand_context_extras"]["lines"]
    out = ""
    for key in ("description", "products_and_services", "brand_identity", "business_model",
                "customer_segments", "target_markets", "language", "ai_topics"):
        v = ctx.get(key)
        if v:
            out += fill(lines[key], **{key: _join(v)})
    info, com, trn = (ctx.get(f"intent_{k}_pct", 0) or 0 for k in ("informational", "commercial", "transactional"))
    if info + com + trn > 0:
        out += fill(lines["intent_mix"], informational=info, commercial=com, transactional=trn)
    return out


def _existing(p: dict, titles: list[str]) -> str:
    if not titles:
        return ""
    return _text(p["existing_titles_intro"]) + "".join(fill(p["existing_title_item"], title=t) for t in titles)


def _category_head(p: dict, domain: str, ctx: dict) -> str:
    out = fill(p["header"], domain=domain, category=ctx.get("category", ""), value_offering=ctx.get("value_offering", ""))
    if ctx.get("named_competitors"):
        out += fill(_text(p["named_competitors"]), named_competitors=_join(ctx["named_competitors"]))
    return out + _brand_extras(ctx)


def _related(p: dict, topics: list[str]) -> str:
    return fill(_text(p["related_topics"]), related_topics="; ".join(topics)) if topics else ""


def owned_citation(domain: str, signals: list[dict], existing_titles: list[str], note: str = "", guidance: str = "") -> str:
    p = get("owned_citation")["parts"]
    out = fill(p["header"], domain=domain)
    if existing_titles:
        out += _existing(p, existing_titles) + "\n"
    out += guidance
    out += p["evidence_rules"]
    for s in signals:
        out += fill(p["candidate_item"], prompt_id=s["prompt_id"], prompt_text=s["prompt_text"],
                    severity_tier=s["severity_tier"], gap_score=s["gap_score"],
                    our_citations=s["our_citations"], total_citations=s["total_citations"],
                    competitors=_competitors(s["competitors"]), citation_domains=", ".join(s["citation_domains"]))
    return out + p["instructions"] + note


def owned_category(domain: str, cands: list[dict], existing_titles: list[str], ctx: dict,
                   related_topics: list[str], note: str = "", guidance: str = "") -> str:
    p = get("owned_category")["parts"]
    out = _category_head(p, domain, ctx) + _related(p, related_topics) + _existing(p, existing_titles) + guidance
    out += p["task"] + "".join(fill(p["candidate_item"], prompt_id=c["prompt_id"]) for c in cands)
    return out + p["instructions"] + note


def earned_citation(domain: str, cands: list[dict], existing_titles: list[str], note: str = "", guidance: str = "") -> str:
    p = get("earned_citation")["parts"]
    out = fill(p["header"], domain=domain)
    if existing_titles:
        out += _existing(p, existing_titles) + "\n"
    out += guidance
    out += p["evidence_rules"]
    for c in cands:
        out += fill(p["candidate_item"], cluster_id=c["id"], topic=c["topic"], source_type=c["source_type"],
                    tier=c["tier"], source_usage=c["source_usage"], our_citations=c["our_citations"],
                    total_citations=c["total_citations"], domain=c["evidence_domains"][0] if c["evidence_domains"] else "")
        if c["evidence_pages"]:
            out += _text(p["pages_intro"])
            for i, pg in enumerate(c["evidence_pages"], 1):
                out += fill(p["page_item"], n=i, title=pg["title"], uri=pg["uri"], domain=pg["domain"])
        else:
            out += _text(p["no_pages"])
    return out + p["instructions"] + note


def earned_category(domain: str, cands: list[dict], existing_titles: list[str], ctx: dict,
                    related_topics: list[str], note: str = "", guidance: str = "") -> str:
    p = get("earned_category")["parts"]
    out = _category_head(p, domain, ctx) + _related(p, related_topics) + _existing(p, existing_titles) + guidance
    out += p["task"] + "".join(fill(p["candidate_item"], cluster_id=c["id"], source_type=c["source_type"]) for c in cands)
    return out + p["instructions"] + note


def ask_system(domain: str) -> str:
    return fill(get("ask_prioritize")["parts"]["system"], domain=domain)


def ask_user(items: list[dict], question: str) -> str:
    p = get("ask_prioritize")["parts"]
    body = "".join(fill(p["context_item"], **{**i, "competitors_winning": _join(i["competitors_winning"]) or "none"}) + "\n"
                   for i in items)
    return "Recommendations:\n\n" + body + fill(p["question"], question=question)
