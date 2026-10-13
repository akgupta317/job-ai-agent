"""Source-type rules for earned media. Port of internal/contentrecs/earned_taxonomy.go."""

EDITORIAL = "Editorial"
UGC = "UGC"
REFERENCE = "Reference"
INSTITUTIONAL = "Institutional"

UGC_DOMAINS = {
    "reddit.com", "quora.com", "g2.com", "capterra.com",
    "trustradius.com", "producthunt.com", "stackoverflow.com",
}
REFERENCE_DOMAINS = {"wikipedia.org", "crunchbase.com", "linkedin.com"}

PLATFORM_NAMES = {
    "reddit.com": "Reddit", "quora.com": "Quora", "g2.com": "G2",
    "capterra.com": "Capterra", "trustradius.com": "TrustRadius",
    "producthunt.com": "Product Hunt", "stackoverflow.com": "Stack Overflow",
    "wikipedia.org": "Wikipedia", "crunchbase.com": "Crunchbase", "linkedin.com": "LinkedIn",
}


def normalize_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    return d.removeprefix("www.")


def classify_source_type(citation_domain: str, brand_domain: str, competitor_domains: set[str]) -> str | None:
    """Returns the source type, or None when the domain is not an earned-media target
    (the brand's own domain, or a competitor's)."""
    d = normalize_domain(citation_domain)
    if not d:
        return None
    if d == normalize_domain(brand_domain):
        return None
    if d in competitor_domains:
        return None
    if d.endswith(".gov") or d.endswith(".edu"):
        return INSTITUTIONAL
    if d in UGC_DOMAINS:
        return UGC
    if d in REFERENCE_DOMAINS:
        return REFERENCE
    return EDITORIAL


def channel_for_domain(domain: str) -> str:
    """Real platform name for a domain, e.g. g2.com -> G2, techcrunch.com -> Techcrunch."""
    d = normalize_domain(domain)
    if d in PLATFORM_NAMES:
        return PLATFORM_NAMES[d]
    label = d.split(".", 1)[0].replace("-", " ")
    return " ".join(w[:1].upper() + w[1:] for w in label.split())
