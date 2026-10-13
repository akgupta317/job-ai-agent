"""Deterministic quality checks for one recommendation.

These are the checks that never depend on a model, so they are fast, free and repeatable. They cover
what the generate validators do not: numbers that disagree with the evidence, invented sites,
near-duplicates, filler, rationales that ignore the evidence, and repeats of what the team dismissed.

Severity: reject (do not use / do not save), revise (fixable problem), note (worth knowing).
"""
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from agent.gaps.taxonomy import REFERENCE_DOMAINS, UGC_DOMAINS, normalize_domain
from agent.judge.text import similarity
from agent.prompts.loader import load

XY = re.compile(r"\b(\d+)\s+(?:out\s+)?of\s+(\d+)\b", re.I)
SCORE = re.compile(r"\bscore\s+(?:of\s+)?(\d+)\s*/\s*10\b", re.I)
TIMES = re.compile(r"\b(?:cited|referenced|mentioned|used)\s+(\d+)\s+times\b", re.I)
DOMAIN = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.(?:com|ai|io|org|net|co|dev|app|edu|gov|so|ly|tech))\b", re.I
)
CAMEL = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z]+\b")

PRIORITY_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2}


@dataclass
class Issue:
    code: str
    severity: str  # reject | revise | note
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


def cfg() -> dict:
    return load()["judge"]


def row_id(row: dict) -> str:
    return f"{row.get('channel_type', '')}:{row.get('prompt_id', '')}"


def kind_of(row: dict) -> str:
    return "earned" if row.get("channel_type") == "earned" else "owned"


def is_citation_backed(row: dict) -> bool:
    return row.get("evidence_type") != "category_inferred"


def rank_key(row: dict) -> tuple[int, int]:
    """Strongest first: priority, then evidence score."""
    return (PRIORITY_ORDER.get(row.get("priority"), 3), -(row.get("gap_score") or 0))


def order_for_review(rows: list[dict]) -> list[dict]:
    """Strongest first, so when two rows are near-identical the stronger one is kept."""
    return sorted(rows, key=rank_key)


def _allowed_domains(row: dict, allowed_names: set[str]) -> set[str]:
    out = {normalize_domain(d) for d in (row.get("competitors_winning") or []) if "." in str(d)}
    out |= {normalize_domain(n) for n in allowed_names if "." in n}
    host = urlparse(str(row.get("source_url") or "")).hostname  # the cited page itself is evidence
    if host:
        out.add(normalize_domain(host))
    out |= set(UGC_DOMAINS) | set(REFERENCE_DOMAINS)
    return out


def _domain_ok(domain: str, allowed: set[str]) -> bool:
    d = normalize_domain(domain)
    return any(d == a or d.endswith("." + a) for a in allowed)


def _brand_label(domain: str) -> str:
    """'blog.hubspot.com' -> 'hubspot': the part people actually write in a sentence."""
    parts = normalize_domain(domain).split(".")
    return parts[-2] if len(parts) >= 2 else ""


def _numbers(row: dict, text: str) -> list[Issue]:
    issues: list[Issue] = []
    our, total = row.get("our_citations"), row.get("total_citations")
    if not is_citation_backed(row):
        # Brand-profile ideas have no citation evidence, so any citation count in them is invented.
        if XY.search(text) or SCORE.search(text):
            issues.append(Issue("invented_number", "revise",
                                "this idea has no citation evidence, but the text quotes citation counts or a score"))
        return issues
    if isinstance(our, int) and isinstance(total, int):
        for a, b in XY.findall(text):
            if (int(a), int(b)) != (our, total):
                issues.append(Issue("number_mismatch", "revise",
                                    f"the text says {a} of {b} citations, but the evidence on this row is {our} of {total}"))
                break
    if kind_of(row) == "owned" and isinstance(row.get("gap_score"), int):
        for n in SCORE.findall(text):
            if int(n) != row["gap_score"]:
                issues.append(Issue("number_mismatch", "revise",
                                    f"the text says evidence score {n}/10, but the row's score is {row['gap_score']}/10"))
                break
    if kind_of(row) == "earned" and isinstance(row.get("gap_score"), int):
        for n in TIMES.findall(text):
            if int(n) != row["gap_score"]:
                issues.append(Issue("number_mismatch", "revise",
                                    f"the text says it was cited {n} times, but the evidence says {row['gap_score']}"))
                break
    return issues


def _names(row: dict, text: str, allowed_names: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    allowed = _allowed_domains(row, allowed_names)
    seen: set[str] = set()
    for m in DOMAIN.findall(text):
        d = normalize_domain(m)
        if d in seen:
            continue
        seen.add(d)
        if not _domain_ok(d, allowed):
            issues.append(Issue("unverified_domain", "revise",
                                f"names {d}, which is not in this row's evidence or the brand profile"))
    if not is_citation_backed(row):
        known = {w.lower() for w in cfg()["known_brand_words"]} | {n.lower() for n in allowed_names}
        for w in CAMEL.findall(text):
            if w.lower() not in known:
                issues.append(Issue("possible_invented_name", "note", f"names '{w}', which could not be verified"))
                break
    return issues


def _wording(row: dict, title: str, rationale: str) -> list[Issue]:
    c = cfg()
    t = c["thresholds"]
    issues: list[Issue] = []
    if len(title) < t["title_min"]:
        issues.append(Issue("title_too_short", "revise", f"title is under {t['title_min']} characters"))
    elif len(title) > t["title_max"]:
        issues.append(Issue("title_too_long", "note", f"title is over {t['title_max']} characters"))
    if len(rationale) < t["rationale_min"]:
        issues.append(Issue("rationale_too_short", "revise", f"rationale is under {t['rationale_min']} characters"))
    elif len(rationale) > t["rationale_max"]:
        issues.append(Issue("rationale_too_long", "note", f"rationale is over {t['rationale_max']} characters"))

    hits = [p for p in c["generic_phrases"] if p in f"{title} {rationale}".lower()]
    if len(hits) >= 2:
        issues.append(Issue("generic_filler", "revise", f"leans on generic phrases ({', '.join(hits[:3])})"))
    elif hits:
        issues.append(Issue("generic_filler", "note", f"uses a generic phrase ({hits[0]})"))

    if is_citation_backed(row):
        rivals = [str(x).lower() for x in (row.get("competitors_winning") or []) if x]
        mentions_rival = any(r in rationale.lower() for r in rivals)
        if not re.search(r"\d", rationale) and not mentions_rival:
            issues.append(Issue("ignores_evidence", "revise",
                                "the rationale uses none of the evidence (no citation counts, no rival or source named)"))
    if kind_of(row) == "earned" and is_citation_backed(row):
        targets = [str(x).lower() for x in (row.get("competitors_winning") or []) if x]
        channel = str(row.get("channel") or "").lower()
        labels = [_brand_label(t_) for t_ in targets]
        named = any(t_ in title.lower() for t_ in targets) or any(lb and lb in title.lower() for lb in labels)
        if targets and not named and not (channel and channel in title.lower()):
            issues.append(Issue("missing_target", "revise", f"the title does not name where to act ({targets[0]})"))
    return issues


def _duplicates(row: dict, title: str, earlier: list[dict], existing: list[dict], feedback) -> list[Issue]:
    thr = cfg()["thresholds"]
    issues: list[Issue] = []
    me = row_id(row)
    for other in earlier:
        if row_id(other) != me and kind_of(other) == kind_of(row) and similarity(title, other.get("title", "")) >= thr["duplicate_jaccard"]:
            issues.append(Issue("duplicate_in_batch", "reject", f'near-identical to "{other.get("title", "")[:90]}" (stronger or earlier in this list)'))
            break
    for other in existing:
        if other.get("prompt_id") != row.get("prompt_id") and similarity(title, other.get("title", "")) >= thr["duplicate_jaccard"]:
            issues.append(Issue("duplicate_of_saved", "reject", f'near-identical to a saved recommendation: "{other.get("title", "")[:90]}"'))
            break
    if feedback is not None:
        hit = feedback.similar_dismissed(title, kind_of(row))
        if hit:
            dismissed_title, score = hit
            if score >= thr["dismissed_reject_jaccard"]:
                issues.append(Issue("similar_to_dismissed", "reject", f'the team already dismissed a very similar one: "{dismissed_title[:90]}"'))
            elif score >= thr["dismissed_note_jaccard"]:
                issues.append(Issue("similar_to_dismissed", "note", f'resembles one the team dismissed: "{dismissed_title[:90]}"'))
    return issues


def check_row(row: dict, *, earlier: list[dict] | None = None, existing: list[dict] | None = None,
              feedback=None, allowed_names: set[str] | None = None) -> list[Issue]:
    """All deterministic checks for one row. `earlier` are the rows ranked above it in the same list."""
    title = str(row.get("title") or "").strip()
    rationale = str(row.get("rationale") or "").strip()
    text = f"{title} {rationale}"
    allowed = allowed_names or set()
    return [
        *_numbers(row, text),
        *_names(row, text, allowed),
        *_wording(row, title, rationale),
        *_duplicates(row, title, earlier or [], existing or [], feedback),
    ]


def score_and_verdict(issues: list[Issue]) -> tuple[float, str]:
    if any(i.severity == "reject" for i in issues):
        return 1.0, "reject"
    score = 5.0 - sum(1.0 for i in issues if i.severity == "revise") - sum(0.25 for i in issues if i.severity == "note")
    score = max(1.0, round(score, 2))
    return score, ("revise" if any(i.severity == "revise" for i in issues) else "pass")
