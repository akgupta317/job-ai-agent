"""The model reviewer: scores a batch of recommendations against ONLY the evidence stored on each row,
and rewrites a row to fix problems. The model is untrusted: scores are clamped, text is cleaned to
one line, and nothing here can reject a row (only the deterministic rules can)."""
from agent import config
from agent.judge.rules import is_citation_backed, kind_of, row_id
from agent.judge.text import clean_line
from agent.llm import ollama
from agent.prompts.loader import fill, get

SCORE_KEYS = ("grounded", "specific", "actionable", "fit")


def evidence_line(row: dict) -> str:
    """The facts a reviewer may use for this row, taken from the row itself (never from outside)."""
    topic = clean_line(row.get("topic", ""), 80) or "n/a"
    our, total = row.get("our_citations"), row.get("total_citations")
    cited = f"the brand is cited in {our} of {total}" if isinstance(our, int) and isinstance(total, int) and total else "no citation counts"
    rivals = ", ".join(clean_line(x, 40) for x in (row.get("competitors_winning") or [])[:5]) or "none"
    if not is_citation_backed(row):
        return f"No citation evidence: this idea comes from the brand profile only. Topic: {topic}."
    if kind_of(row) == "earned":
        page = clean_line(row.get("source_url", ""), 120) or "none given"
        return (f"Topic: {topic}. Source type {clean_line(row.get('source_type', ''), 30) or 'n/a'}; target {rivals}; "
                f"this source type was cited {row.get('gap_score', 'n/a')} times on the topic; {cited}. Cited page: {page}.")
    return f"Topic: {topic}. Priority {row.get('priority', 'n/a')}. {cited.capitalize()} AI answers on this question. Rivals cited instead: {rivals}."


def build_prompt(rows: list[dict]) -> str:
    p = get("judge_rubric")["parts"]
    out = p["header"]
    for r in rows:
        out += fill(p["item"], id=row_id(r), kind=kind_of(r), evidence=evidence_line(r),
                    title=clean_line(r.get("title", ""), 220), rationale=clean_line(r.get("rationale", ""), 520))
    return out + p["instructions"]


def _score(v) -> int | None:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


def parse(obj: dict, ids: set[str]) -> dict[str, dict]:
    """Reads the model's reply defensively: only known ids, scores clamped to 1-5, text cleaned."""
    out: dict[str, dict] = {}
    reviews = obj.get("reviews") if isinstance(obj, dict) else None
    for item in reviews if isinstance(reviews, list) else []:
        if not isinstance(item, dict) or item.get("id") not in ids:
            continue
        scores = {k: _score(item.get(k)) for k in SCORE_KEYS}
        issues = [clean_line(i, 160) for i in (item.get("issues") if isinstance(item.get("issues"), list) else [])[:3] if str(i).strip()]
        out[item["id"]] = {"scores": scores, "issues": issues, "suggestion": clean_line(item.get("suggestion", ""), 200)}
    return out


def review_batch(rows: list[dict]) -> tuple[dict[str, dict], dict]:
    """One model call for a few rows. A reply that is not valid JSON scores nothing (no penalty)."""
    try:
        obj, usage = ollama.chat_json(build_prompt(rows), model=config.OLLAMA_JUDGE_MODEL)
    except ollama.InvalidJSON:
        return {}, {"tokens_in": 0, "tokens_out": 0, "model_calls": 1}
    return parse(obj, {row_id(r) for r in rows}), {**usage, "model_calls": 1}


def rewrite(row: dict, problems: list[str]) -> tuple[dict | None, dict]:
    """Asks the model for a better title/rationale for ONE row. Returns None if it can't be used."""
    p = get("revise_recommendation")["parts"]
    prompt = (p["header"] + fill(p["body"], kind=kind_of(row), evidence=evidence_line(row), title=clean_line(row.get("title", ""), 220),
                                 rationale=clean_line(row.get("rationale", ""), 520),
                                 issues="\n".join(f"- {clean_line(x, 200)}" for x in problems[:5]) or "- none listed")
              + p["instructions"])
    try:
        obj, usage = ollama.chat_json(prompt, model=config.OLLAMA_JUDGE_MODEL)
    except ollama.InvalidJSON:
        return None, {"tokens_in": 0, "tokens_out": 0, "model_calls": 1}
    usage = {**usage, "model_calls": 1}
    title, rationale = clean_line(obj.get("title", ""), 220), clean_line(obj.get("rationale", ""), 520)
    return ({"title": title, "rationale": rationale} if title and rationale else None), usage
