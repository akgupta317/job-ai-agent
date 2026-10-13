"""LangGraph workflow for questions such as "which should we do first, and why?".
Reads the domain's active recommendations, keeps the strongest ones, and asks the model to
answer using only that evidence."""
import asyncio
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent import config, guard, prompting
from agent.llm import ollama
from agent.tools import thrivestack_mcp as mcp

PRIORITY_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2}
ACTIONABLE = ("suggested", "todo")
PER_KIND = 10


class AskState(TypedDict, total=False):
    token: str
    domain: str
    question: str
    items: list
    answer: str
    usage: dict
    refusal: dict


def _rank(recs: list[dict]) -> list[dict]:
    live = [r for r in recs if r.get("status") in ACTIONABLE]
    return sorted(live, key=lambda r: (PRIORITY_ORDER.get(r.get("priority"), 3),
                                       -(r.get("gap_score") or 0), -(r.get("total_citations") or 0)))[:PER_KIND]


def _refusal(where: str, verdict: guard.Verdict, question: str) -> dict:
    guard.log_refusal(where, verdict, question)
    return {"refusal": {"reason": verdict.reason, "message": verdict.message}, "answer": verdict.message, "usage": {}}


async def screen(state: AskState) -> dict:
    """Guard layers 1-2: size, prompt-injection and off-task requests. Runs before any data is fetched."""
    verdict = guard.screen(state["question"])
    return {} if verdict.allowed else _refusal("screen", verdict, state["question"])


def _after_screen(state: AskState) -> str:
    return END if state.get("refusal") else "load_recs"


async def load_recs(state: AskState) -> dict:
    token, domain = state["token"], state["domain"]
    owned = await mcp.call_tool(token, "get_owned_content_recommendations", {"domain": domain, "limit": 500})
    earned = await mcp.call_tool(token, "get_earned_content_recommendations", {"domain": domain, "limit": 500})
    items = []
    for kind, data in (("owned", owned), ("earned", earned)):
        recs = data.get("recommendations", []) if isinstance(data, dict) else []
        for r in _rank(recs):
            items.append({
                "ref": f"R{len(items) + 1}", "id": r["id"], "kind": kind, "priority": r.get("priority"),
                "status": r.get("status"), "evidence_type": r.get("evidence_type"), "title": r.get("title", ""),
                "topic": r.get("topic", ""), "our_citations": r.get("our_citations", 0),
                "total_citations": r.get("total_citations", 0), "gap_score": r.get("gap_score", 0),
                "competitors_winning": r.get("competitors_winning") or [],
                "rationale": (r.get("rationale") or "")[:300],
            })
    return {"items": items}


async def answer(state: AskState) -> dict:
    items = state.get("items", [])
    question = state["question"]
    # Re-check here, not only in `screen`: this is the one function that calls the model with the
    # question, so it must not depend on an earlier node having run.
    verdict = guard.screen(question)
    if verdict.allowed:
        verdict = guard.on_topic(question, items)
    if not verdict.allowed:
        return _refusal("answer", verdict, question)
    if not items:
        return {"answer": "There are no active recommendations for this domain yet, so there is nothing to prioritise. "
                          "Ask me to generate owned or earned recommendations first.", "usage": {}}
    # The model sees short refs (R1, R2...) instead of long ids so it can cite them reliably.
    context = [{**i, "id": i["ref"]} for i in items]
    text, usage = await asyncio.to_thread(
        ollama.chat, prompting.ask_user(context, guard.harden(question)), prompting.ask_system(state["domain"]))
    checked = guard.check_answer(text, context)
    if not checked.allowed:
        return _refusal("answer_check", checked, question) | {"usage": usage}
    return {"answer": text.strip(), "usage": usage}


def build_graph():
    g = StateGraph(AskState)
    g.add_node("screen", screen)
    g.add_node("load_recs", load_recs)
    g.add_node("answer", answer)
    g.add_edge(START, "screen")
    g.add_conditional_edges("screen", _after_screen, {END: END, "load_recs": "load_recs"})
    g.add_edge("load_recs", "answer")
    g.add_edge("answer", END)
    return g.compile()


GRAPH = build_graph()


async def ask(token: str, domain: str, question: str) -> dict:
    final = await GRAPH.ainvoke({"token": token, "domain": domain, "question": question})
    text, items = final["answer"], final.get("items", [])
    cited = set(re.findall(r"\bR\d+\b", text))
    refs = [{"ref": i["ref"], "id": i["id"], "kind": i["kind"], "priority": i["priority"], "title": i["title"]}
            for i in items if i["ref"] in cited]
    refusal = final.get("refusal")
    return {"domain": domain, "model": config.OLLAMA_MODEL, "answer": text, "references": [] if refusal else refs,
            "considered": len(items), "usage": final.get("usage", {}),
            "refused": bool(refusal), "refusal_reason": refusal["reason"] if refusal else None}
