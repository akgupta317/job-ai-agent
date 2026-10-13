"""LangGraph workflow: load evidence -> (write owned | write earned in parallel) -> finish.

State only carries plain data; the Go API remains the system of record, so this graph
returns recommendations and does not save them anywhere.
"""
import asyncio
import uuid
from collections import Counter
from operator import add
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from agent import brand, feedback
from agent import candidates as cand
from agent import config, prompting
from agent.gaps.earned import extract_earned_signals
from agent.gaps.owned import extract_owned_signals
from agent.judge import core as judge_core
from agent.tools import thrivestack_mcp as mcp
from agent.validators.earned import validate_earned_category, validate_earned_citation
from agent.validators.owned import validate_owned_category, validate_owned_citation
from agent.writers import NO_USAGE, run_batches


def _merge_usage(a: dict, b: dict) -> dict:
    return {k: (a or {}).get(k, 0) + (b or {}).get(k, 0) for k in NO_USAGE}


class GenState(TypedDict, total=False):
    # inputs
    token: str
    domain: str
    kinds: list[str]
    max_per_side: int
    min_per_side: int
    brand_context: dict
    judge: str  # off | rules | full
    use_feedback: bool
    revise: bool
    # working data
    run_id: str
    owned_real: list
    earned_real: list
    owned_filler: list
    earned_filler: list
    active_owned: list
    active_earned: list
    stats: dict
    feedback: object
    guidance_owned: str
    guidance_earned: str
    allowed: set
    # outputs (written by parallel branches, so they use reducers)
    owned: list
    earned: list
    held_back: list
    quality: dict
    dropped: Annotated[list, add]
    notes: Annotated[list, add]
    usage: Annotated[dict, _merge_usage]


async def _active(token: str, tool: str, domain: str) -> list[dict]:
    data = await mcp.call_tool(token, tool, {"domain": domain, "limit": 500})
    return data.get("recommendations", []) if isinstance(data, dict) else []


async def load(state: GenState) -> dict:
    token, domain, kinds = state["token"], state["domain"], state["kinds"]
    cap, floor = state["max_per_side"], state.get("min_per_side", 0)
    ctx = state.get("brand_context") or {}
    notes: list[str] = []
    run_token = uuid.uuid4().hex[:8]

    # The Brand Kit is only needed to fill up to the minimum. Use what the caller sent,
    # otherwise load the saved profile.
    if floor > 0 and not ctx.get("category"):
        ctx = await brand.load_context(token, domain)
        if not ctx.get("category"):
            notes.append("The brand profile has no industry/category saved, so category-based ideas cannot be written.")

    probe = await mcp.call_tool(token, "get_citation_probe_data", {"domain": domain})
    questions = probe.get("questions", []) if isinstance(probe, dict) and probe.get("has_run") else []
    if not questions:
        notes.append("No completed probe run with results for this domain, so there is no citation evidence.")

    out: dict = {"run_id": probe.get("run_id") if isinstance(probe, dict) else None, "notes": notes, "stats": {}, "brand_context": ctx}

    if "owned" in kinds:
        active = await _active(token, "get_owned_content_recommendations", domain)
        gaps = cand.sort_owned(cand.best_owned_per_prompt(extract_owned_signals(questions)))
        fresh, unchanged = cand.filter_unchanged_owned(gaps, active)
        out["active_owned"] = active
        out["owned_real"] = fresh[:cap]
        out["stats"]["owned"] = {"gaps_found": len(gaps), "unchanged_skipped": unchanged,
                                 "not_selected": max(0, len(fresh) - cap)}
        need = max(0, floor - len(out["owned_real"]))
        out["owned_filler"] = cand.owned_category_ideas(need, run_token) if need and ctx.get("category") else []
        if need and not ctx.get("category"):
            notes.append(f"Owned: {need} more needed to reach the minimum of {floor}, but there is no brand category, so nothing was added.")

    if "earned" in kinds:
        active = await _active(token, "get_earned_content_recommendations", domain)
        gaps = cand.with_ids(extract_earned_signals(questions, domain))
        fresh, unchanged = cand.filter_unchanged_earned(gaps, active)
        out["active_earned"] = active
        out["earned_real"] = fresh[:cap]
        out["stats"]["earned"] = {"gaps_found": len(gaps), "unchanged_skipped": unchanged,
                                  "not_selected": max(0, len(fresh) - cap)}
        need = max(0, floor - len(out["earned_real"]))
        out["earned_filler"] = cand.earned_category_ideas(need, run_token) if need and ctx.get("category") else []
        if need and not ctx.get("category"):
            notes.append(f"Earned: {need} more needed to reach the minimum of {floor}, but there is no brand category, so nothing was added.")

    # Feedback loop: what the team already approved or dismissed shapes the writers and the Judge.
    if state.get("use_feedback", True):
        fb = await feedback.load(token, domain, out.get("active_owned"), out.get("active_earned"))
        out["feedback"] = fb
        out["guidance_owned"], out["guidance_earned"] = fb.guidance("owned"), fb.guidance("earned")
        if fb.error:
            notes.append(fb.error)

    # Judge: names a recommendation may mention (the brand profile lists tracked competitors).
    if state.get("judge", "rules") != "off":
        names_ctx = ctx if ctx.get("named_competitors") else {}
        if not names_ctx:
            try:
                names_ctx = await brand.load_context(token, domain)
            except Exception:
                names_ctx = {}
        out["allowed"] = judge_core.allowed_names(domain, names_ctx)
    return out


def _route(state: GenState) -> list[str]:
    nodes = [n for k, n in (("owned", "write_owned"), ("earned", "write_earned")) if k in state["kinds"]]
    return nodes or ["finish"]


async def write_owned(state: GenState) -> dict:
    domain, ctx = state["domain"], state.get("brand_context") or {}
    real, filler, active = state.get("owned_real", []), state.get("owned_filler", []), state.get("active_owned", [])
    guidance = state.get("guidance_owned", "")
    rows, dropped, usage = [], [], dict(NO_USAGE)

    if real:
        def titles_for(chunk):
            return cand.titles_excluding(active, {s["prompt_id"] for s in chunk},
                                         {s["topic"] for s in chunk}, config.MAX_EXISTING_TITLES)
        r, d, u = await asyncio.to_thread(
            run_batches, real,
            lambda chunk, note: prompting.owned_citation(domain, chunk, titles_for(chunk), note, guidance=guidance),
            validate_owned_citation, config.BATCH_SIZE)
        rows += r; dropped += d; usage = _merge_usage(usage, u)

    if filler:
        topics = cand.unique_topics(real)
        titles = cand.titles_excluding(active, set(), set(topics), config.MAX_EXISTING_TITLES)
        r, d, u = await asyncio.to_thread(
            run_batches, filler,
            lambda chunk, note: prompting.owned_category(domain, chunk, titles, ctx, topics, note, guidance=guidance),
            lambda chunk, replies: validate_owned_category(chunk, replies, ctx.get("category", "")),
            config.BATCH_SIZE)
        rows += r; dropped += d; usage = _merge_usage(usage, u)
    return {"owned": rows, "dropped": [{**x, "kind": "owned"} for x in dropped], "usage": usage}


async def write_earned(state: GenState) -> dict:
    domain, ctx = state["domain"], state.get("brand_context") or {}
    real, filler, active = state.get("earned_real", []), state.get("earned_filler", []), state.get("active_earned", [])
    guidance = state.get("guidance_earned", "")
    rows, dropped, usage = [], [], dict(NO_USAGE)

    if real:
        def titles_for(chunk):
            return cand.titles_excluding(active, {c["id"] for c in chunk},
                                         {c["topic"] for c in chunk}, config.MAX_EXISTING_TITLES)
        r, d, u = await asyncio.to_thread(
            run_batches, real,
            lambda chunk, note: prompting.earned_citation(domain, chunk, titles_for(chunk), note, guidance=guidance),
            validate_earned_citation, config.BATCH_SIZE)
        rows += r; dropped += d; usage = _merge_usage(usage, u)

    if filler:
        topics = cand.unique_topics(real)
        titles = cand.titles_excluding(active, set(), set(topics), config.MAX_EXISTING_TITLES)
        r, d, u = await asyncio.to_thread(
            run_batches, filler,
            lambda chunk, note: prompting.earned_category(domain, chunk, titles, ctx, topics, note, guidance=guidance),
            lambda chunk, replies: validate_earned_category(chunk, replies, ctx.get("category", ""), ctx.get("named_competitors", [])),
            config.BATCH_SIZE)
        rows += r; dropped += d; usage = _merge_usage(usage, u)
    return {"earned": rows, "dropped": [{**x, "kind": "earned"} for x in dropped], "usage": usage}


async def quality(state: GenState) -> dict:
    """The Judge reviews what was just written. Rows it rejects are held back (listed, never saved);
    every other row carries its review under `quality`."""
    mode = state.get("judge", "rules")
    rows = [*state.get("owned", []), *state.get("earned", [])]
    if mode == "off" or not rows:
        return {}
    existing = [*state.get("active_owned", []), *state.get("active_earned", [])]
    fb, allowed = state.get("feedback"), state.get("allowed")
    reviews = judge_core.review_rules(rows, existing=existing, feedback=fb, allowed_names=allowed)
    usage, notes = [], []
    if mode == "full":
        u, errors = await asyncio.to_thread(judge_core.apply_rubric, rows, reviews)
        usage.append(u)
        notes += errors
        if state.get("revise"):
            rows, u2, errors2 = await asyncio.to_thread(judge_core.revise_rows, rows, reviews, existing=existing, feedback=fb, allowed_names=allowed)
            usage.append(u2)
            notes += errors2
    owned, earned, held = [], [], []
    for row, rv in zip(rows, reviews):
        if rv.verdict == "reject":
            held.append({"id": rv.id, "kind": rv.kind, "title": row.get("title", ""),
                         "reasons": [i.message for i in rv.issues if i.severity == "reject"], "quality": rv.to_dict()})
        else:
            (earned if rv.kind == "earned" else owned).append({**row, "quality": rv.to_dict()})
    return {"owned": owned, "earned": earned, "held_back": held, "quality": judge_core.summarise(reviews),
            "usage": judge_core._usage(*usage), "notes": notes}


async def finish(state: GenState) -> dict:
    return {}


def build_graph():
    g = StateGraph(GenState)
    g.add_node("load", load)
    g.add_node("write_owned", write_owned)
    g.add_node("write_earned", write_earned)
    g.add_node("quality", quality)
    g.add_node("finish", finish)
    g.add_edge(START, "load")
    g.add_conditional_edges("load", _route, ["write_owned", "write_earned", "finish"])
    g.add_edge("write_owned", "quality")
    g.add_edge("write_earned", "quality")
    g.add_edge("quality", "finish")
    g.add_edge("finish", END)
    return g.compile()


GRAPH = build_graph()


async def generate(token: str, domain: str, kinds: list[str], max_per_side: int | None = None,
                   min_per_side: int = 0, brand_context: dict | None = None, judge: str = "rules",
                   use_feedback: bool = True, revise: bool = False) -> dict:
    final = await GRAPH.ainvoke({
        "token": token, "domain": domain, "kinds": kinds,
        "max_per_side": max_per_side or config.DEFAULT_MAX_PER_SIDE,
        "min_per_side": min_per_side, "brand_context": brand_context or {},
        "judge": judge, "use_feedback": use_feedback, "revise": revise,
        "dropped": [], "notes": [], "usage": dict(NO_USAGE),
    })
    owned, earned = final.get("owned", []), final.get("earned", [])
    return {
        "domain": domain,
        "run_id": final.get("run_id"),
        "model": config.OLLAMA_MODEL,
        "summary": {
            "owned_written": len(owned), "earned_written": len(earned),
            "owned_by_priority": dict(Counter(r["priority"] for r in owned)),
            "earned_by_priority": dict(Counter(r["priority"] for r in earned)),
            "dropped": len(final.get("dropped", [])),
            "held_back": len(final.get("held_back", [])),
            "evidence": final.get("stats", {}),
        },
        "owned": owned,
        "earned": earned,
        "held_back": final.get("held_back", []),
        "quality": final.get("quality"),
        "feedback": final["feedback"].summary() if final.get("feedback") is not None else None,
        "dropped": final.get("dropped", []),
        "notes": final.get("notes", []),
        "usage": final.get("usage", {}),
    }
