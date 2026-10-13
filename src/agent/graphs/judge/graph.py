"""The Judge agent: reviews the quality of recommendations and says what to fix.

    load -> rules -> (rubric -> (revise)) -> finish

  load    reads the rows (saved recommendations, or rows you send), the brand profile (names a row may
          mention) and the team's earlier decisions (the feedback loop)
  rules   deterministic checks: numbers vs evidence, invented sites, duplicates, filler, repeats of
          what the team dismissed. Can reject.
  rubric  (mode "full") a model scores grounded / specific / actionable / fit. Can only ask for a revision.
  revise  (mode "full" + revise) one rewrite per weak row; kept only if the rules then pass.

It never changes saved data. It only reports.
"""
import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent import brand, config, feedback
from agent.judge import core
from agent.judge.rules import order_for_review
from agent.tools import thrivestack_mcp as mcp

LIVE_STATUSES = ("suggested", "todo", "approved")


class JudgeState(TypedDict, total=False):
    token: str
    domain: str
    scope: str  # saved | rows
    mode: str  # rules | full
    use_feedback: bool
    revise: bool
    rows: list
    existing: list
    feedback: object
    allowed: set
    reviews: list
    notes: list
    usage: dict


async def _stored(token: str, kind: str, domain: str) -> list[dict]:
    tool = feedback.TOOLS[kind]
    data = await mcp.call_tool(token, tool, {"domain": domain, "limit": config.JUDGE_SAVED_LIMIT})
    return data.get("recommendations", []) if isinstance(data, dict) else []


async def load(state: JudgeState) -> dict:
    token, domain = state["token"], state["domain"]
    notes: list[str] = []
    owned = await _stored(token, "owned", domain)
    earned = await _stored(token, "earned", domain)

    if state["scope"] == "saved":
        live = [r for r in owned + earned if r.get("status") in LIVE_STATUSES]
        rows = order_for_review(live)[: config.JUDGE_SAVED_LIMIT]
        existing: list[dict] = []  # duplicates among the saved rows are found as batch duplicates
        if len(live) > len(rows):
            notes.append(f"Reviewed the strongest {len(rows)} of {len(live)} saved recommendations.")
    else:
        rows = state.get("rows") or []
        existing = [r for r in owned + earned if r.get("status") in LIVE_STATUSES]

    fb = await feedback.load(token, domain, owned, earned) if state.get("use_feedback", True) else None
    if fb is not None and fb.error:
        notes.append(fb.error)
    try:
        ctx = await brand.load_context(token, domain)
    except Exception:
        ctx = {}
        notes.append("Could not read the brand profile, so names are checked against the evidence only.")
    return {"rows": rows, "existing": existing, "feedback": fb, "allowed": core.allowed_names(domain, ctx), "notes": notes}


async def rules(state: JudgeState) -> dict:
    reviews = core.review_rules(state["rows"], existing=state.get("existing"), feedback=state.get("feedback"), allowed_names=state.get("allowed"))
    return {"reviews": reviews, "usage": {"tokens_in": 0, "tokens_out": 0, "model_calls": 0}}


async def rubric(state: JudgeState) -> dict:
    usage, errors = await asyncio.to_thread(core.apply_rubric, state["rows"], state["reviews"])
    return {"usage": core._usage(state["usage"], usage), "notes": [*state.get("notes", []), *errors]}


async def revise(state: JudgeState) -> dict:
    rows, usage, errors = await asyncio.to_thread(
        core.revise_rows, state["rows"], state["reviews"],
        existing=state.get("existing"), feedback=state.get("feedback"), allowed_names=state.get("allowed"))
    return {"rows": rows, "usage": core._usage(state["usage"], usage), "notes": [*state.get("notes", []), *errors]}


async def finish(state: JudgeState) -> dict:
    return {}


def _after_rules(state: JudgeState) -> str:
    return "rubric" if state.get("mode") == "full" else "finish"


def _after_rubric(state: JudgeState) -> str:
    return "revise" if state.get("revise") else "finish"


def build_graph():
    g = StateGraph(JudgeState)
    for name, fn in (("load", load), ("rules", rules), ("rubric", rubric), ("revise", revise), ("finish", finish)):
        g.add_node(name, fn)
    g.add_edge(START, "load")
    g.add_edge("load", "rules")
    g.add_conditional_edges("rules", _after_rules, ["rubric", "finish"])
    g.add_conditional_edges("rubric", _after_rubric, ["revise", "finish"])
    g.add_edge("revise", "finish")
    g.add_edge("finish", END)
    return g.compile()


GRAPH = build_graph()


async def judge(token: str, domain: str, *, scope: str = "saved", rows: list[dict] | None = None, mode: str = "rules",
                use_feedback: bool = True, revise: bool = False) -> dict:
    final = await GRAPH.ainvoke({"token": token, "domain": domain, "scope": scope, "rows": rows or [], "mode": mode,
                                 "use_feedback": use_feedback, "revise": revise, "notes": []})
    reviews = final["reviews"]
    fb = final.get("feedback")
    return {
        "domain": domain, "scope": scope, "mode": mode, "model": config.OLLAMA_JUDGE_MODEL if mode == "full" else None,
        "summary": core.summarise(reviews),
        "reviews": [
            {"id": rv.id, "title": row.get("title", ""), "rationale": row.get("rationale", ""), "kind": rv.kind, "priority": row.get("priority"), **rv.to_dict()}
            for row, rv in zip(final["rows"], reviews)
        ],
        "feedback": fb.summary() if fb is not None else None,
        "notes": final.get("notes", []),
        "usage": final.get("usage", {}),
    }
