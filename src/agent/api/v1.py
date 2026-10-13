from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints

from agent import chat as chat_router
from agent import feedback as feedback_loop
from agent import config, guard, saving
from agent.api.auth import get_token
from agent.graphs.ask.graph import ask as run_ask
from agent.graphs.generate.graph import generate as run_generate
from agent.graphs.judge.graph import judge as run_judge
from agent.llm import ollama
from agent.tools.thrivestack_mcp import McpToolError

router = APIRouter(prefix="/v1")

# Free text that ends up inside a model prompt is bounded here, at the request edge. The model-facing
# code (graphs/ask, llm/ollama) enforces its own limits again, so this is the first fence, not the only one.
DOMAIN_PATTERN = r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$"
Domain = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=DOMAIN_PATTERN)]
Short = Annotated[str, StringConstraints(max_length=300)]
Long = Annotated[str, StringConstraints(max_length=2000)]
ShortList = Annotated[list[Short], Field(max_length=25)]


class BrandContext(BaseModel):
    """Optional Brand Kit override. If omitted, the saved brand profile is loaded from ThriveStack."""
    category: Short = ""
    value_offering: Long = ""
    named_competitors: ShortList = []
    description: Long = ""
    products_and_services: ShortList = []
    brand_identity: ShortList = []
    business_model: Short = ""
    customer_segments: ShortList = []
    target_markets: ShortList = []
    language: Short = ""
    ai_topics: ShortList = []
    intent_informational_pct: int = 0
    intent_commercial_pct: int = 0
    intent_transactional_pct: int = 0


class GenerateRequest(BaseModel):
    domain: Domain
    kinds: list[Literal["owned", "earned"]] = Field(default=["owned", "earned"])
    max_per_side: int | None = Field(default=None, ge=1, le=30, description="Recommendations to write per side. Default 5.")
    min_per_side: int = Field(default=0, ge=0, le=30, description="Fill up to this many per side from the saved brand profile. Default 0 (off).")
    brand_context: BrandContext | None = None
    save: bool = Field(default=False, description="Also save the generated recommendations (as 'suggested') through the ThriveStack MCP. Default false: preview only.")
    judge: Literal["off", "rules", "full"] = Field(default="rules", description="Quality check on what was written. rules = fast deterministic checks (can hold rows back); full = also a model reviewer (slow on CPU); off = none.")
    revise: bool = Field(default=False, description="With judge=full: rewrite rows the Judge says need fixing, once, and keep a rewrite only if it then passes the rules.")
    use_feedback: bool = Field(default=True, description="Learn from what the team already approved or dismissed (read through the MCP).")
    save_policy: Literal["hold_rejects", "pass_only"] = Field(default="hold_rejects", description="With save=true: hold_rejects saves everything the Judge did not reject; pass_only saves only rows that passed outright.")


class JudgeRequest(BaseModel):
    domain: Domain
    scope: Literal["saved", "rows"] = Field(default="saved", description="saved = review the recommendations already saved for this brand; rows = review the ones you send.")
    recommendations: list[dict] | None = Field(default=None, max_length=60, description="Rows to review when scope=rows (for example from a generate response).")
    mode: Literal["rules", "full"] = Field(default="rules", description="rules = fast deterministic checks (no model); full = also a model reviewer (slow on CPU, needs Ollama).")
    use_feedback: bool = True
    revise: bool = Field(default=False, description="With mode=full: one rewrite pass for rows that need fixing.")


class SaveRequest(BaseModel):
    """Rows from an earlier generate call. The agent keeps no results between calls, so send them back."""
    domain: Domain
    run_id: str | None = Field(default=None, description="run_id from the generate response (the probe run the evidence came from).")
    recommendations: list[dict] = Field(min_length=1, max_length=60)


class AskRequest(BaseModel):
    domain: Domain
    question: str = Field(min_length=3, max_length=config.MAX_QUESTION_CHARS)


class ChatRequest(BaseModel):
    domain: Domain
    message: str = Field(min_length=3, max_length=config.MAX_QUESTION_CHARS)


def _guard():
    if not ollama.is_up():
        raise HTTPException(503, "Ollama is not reachable. Start it with: ollama serve")


def _save_error_status(message: str) -> int:
    """Maps a tool-reported save failure to the HTTP status a caller can act on."""
    m = message.lower()
    if "don't have access" in m or "forbidden" in m:
        return 403
    if "probe_not_found" in m or "probe run not found" in m:
        return 404
    if "subscription_paused" in m:
        return 409
    if "invalid_request" in m:
        return 422
    return 502


async def _try_save(token: str, result: dict, policy: str = "hold_rejects") -> dict:
    """Saves a generate result without ever failing the request: the user keeps the rows and sees why.
    Rows the Judge rejected are already held back (never in owned/earned). With pass_only, rows that
    still need revising are kept out of the save too."""
    rows = [*result["owned"], *result["earned"]]
    needs_work = 0
    if policy == "pass_only":
        passing = [r for r in rows if (r.get("quality") or {}).get("verdict", "pass") == "pass"]
        needs_work, rows = len(rows) - len(passing), passing
    if not rows:
        return {"skipped": "nothing to save" if not needs_work else f"{needs_work} recommendation(s) need revising before they are saved (save_policy=pass_only)"}
    try:
        saved = await saving.save_recommendations(token, result["domain"], result.get("run_id"), rows)
        if needs_work:
            saved["not_saved_needs_revision"] = needs_work
        return saved
    except McpToolError as e:
        return {"error": str(e)}
    except Exception:
        return {"error": "Saving failed (ThriveStack MCP call error). The recommendations above were not saved."}


async def _generate(token: str, req: GenerateRequest) -> dict:
    _guard()
    try:
        result = await run_generate(token, req.domain, req.kinds, req.max_per_side, req.min_per_side,
                                    req.brand_context.model_dump() if req.brand_context else None,
                                    judge=req.judge, use_feedback=req.use_feedback, revise=req.revise)
        if req.save:
            result["save"] = await _try_save(token, result, req.save_policy)
        return result
    except ollama.LLMBusy:
        raise
    except ollama.LLMError as e:
        raise HTTPException(502, str(e))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Generation failed (ThriveStack MCP or model call error)")


async def _ask(token: str, domain: str, question: str) -> dict:
    _guard()
    try:
        return await run_ask(token, domain, question)
    except ollama.LLMBusy:
        raise
    except ollama.LLMError as e:
        raise HTTPException(502, str(e))
    except Exception:
        raise HTTPException(502, "Question failed (ThriveStack MCP or model call error)")


@router.post("/recommendations/generate")
async def generate(req: GenerateRequest, token: str = Depends(get_token)) -> dict:
    """Generate owned and/or earned content recommendations for a domain."""
    return await _generate(token, req)


@router.post("/recommendations/save")
async def save(req: SaveRequest, token: str = Depends(get_token)) -> dict:
    """Save recommendations from an earlier generate call. They are stored as 'suggested'; the API
    re-checks citation-backed rows against the probe run, so evidence cannot be invented."""
    try:
        return await saving.save_recommendations(token, req.domain, req.run_id, req.recommendations)
    except McpToolError as e:
        raise HTTPException(_save_error_status(str(e)), str(e))
    except Exception:
        raise HTTPException(502, "Saving failed (ThriveStack MCP call error)")


@router.post("/judge")
async def judge(req: JudgeRequest, token: str = Depends(get_token)) -> dict:
    """Quality review of recommendations: saved ones for the brand, or rows you send. Reports only; it
    never changes saved data. mode=rules needs no model; mode=full also uses the model reviewer."""
    if req.scope == "rows" and not req.recommendations:
        raise HTTPException(422, "recommendations is required when scope is 'rows'")
    if req.mode == "full":
        _guard()
    try:
        return await run_judge(token, req.domain, scope=req.scope, rows=req.recommendations, mode=req.mode,
                               use_feedback=req.use_feedback, revise=req.revise)
    except ollama.LLMBusy:
        raise
    except ollama.LLMError as e:
        raise HTTPException(502, str(e))
    except Exception:
        raise HTTPException(502, "Review failed (ThriveStack MCP or model call error)")


@router.get("/feedback")
async def feedback(domain: Annotated[str, Query(pattern=DOMAIN_PATTERN, max_length=253)], token: str = Depends(get_token)) -> dict:
    """What the team decided about earlier recommendations (accepted, dismissed) and what the agent
    learns from it. No decisions yet means nothing is learned."""
    try:
        fb = await feedback_loop.load(token, domain)
    except Exception:
        raise HTTPException(502, "ThriveStack MCP call failed")
    return {"domain": domain, **fb.summary()}


async def _judge_saved(token: str, domain: str, mode: str) -> dict:
    try:
        return await run_judge(token, domain, scope="saved", mode=mode)
    except ollama.LLMBusy:
        raise
    except ollama.LLMError as e:
        raise HTTPException(502, str(e))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Review failed (ThriveStack MCP or model call error)")


@router.post("/ask")
async def ask(req: AskRequest, token: str = Depends(get_token)) -> dict:
    """Ask about the domain's recommendations, e.g. 'which 3 should we do first, and why?'"""
    result = await _ask(token, req.domain, req.question)
    if result["refused"]:
        raise HTTPException(422, {"refused": True, "reason": result["refusal_reason"], "message": result["answer"]})
    return result


@router.post("/chat")
async def chat(req: ChatRequest, token: str = Depends(get_token)) -> dict:
    """One entry point: asks to generate ('generate earned recommendations') or to prioritise."""
    # Screen first, for every intent: an override or off-task message must not be routed anywhere.
    verdict = guard.screen(req.message)
    if not verdict.allowed:
        guard.log_refusal("chat", verdict, req.message)
        return {"intent": "refused", "reply": verdict.message, "result": {"reason": verdict.reason}}
    intent = chat_router.parse(req.message)
    if intent["intent"] == "generate":
        result = await _generate(token, GenerateRequest(domain=req.domain, kinds=intent["kinds"],
                                                        max_per_side=intent["max_per_side"], save=intent["save"]))
        s = result["summary"]
        reply = (f"Generated {s['owned_written']} owned and {s['earned_written']} earned recommendations "
                 f"for {req.domain} ({s['dropped']} dropped by checks).")
        if s.get("held_back"):
            reply += f" {s['held_back']} more were held back by the quality check (duplicates or repeats of what the team dismissed)."
        q = result.get("quality") or {}
        if q.get("revise"):
            reply += f" {q['revise']} need a closer look (see each card's quality notes)."
        if intent["save"]:
            saved = result.get("save", {})
            if "error" in saved:
                reply += f" Saving failed: {saved['error']}"
            elif "skipped" in saved:
                reply += " Nothing to save."
            else:
                reply += (f" Saved {saved['created']} new, updated {saved['updated']}, left {saved['unchanged']} "
                          f"unchanged, {saved['rejected']} not accepted.")
        return {"intent": "generate", "reply": reply, "result": result}
    if intent["intent"] == "judge":
        if intent["mode"] == "full":
            _guard()
        result = await _judge_saved(token, req.domain, intent["mode"])
        q = result["summary"]
        top = ", ".join(f"{t['code'].replace('_', ' ')} ({t['count']})" for t in q["top_issues"][:3]) or "none"
        reply = (f"Reviewed {q['reviewed']} saved recommendations for {req.domain}: {q['pass']} pass, {q['revise']} need revising, "
                 f"{q['reject']} should be dropped. Most common problems: {top}.")
        return {"intent": "judge", "reply": reply, "result": result}
    if intent["intent"] == "feedback":
        try:
            fb = (await feedback_loop.load(token, req.domain)).summary()
        except Exception:
            raise HTTPException(502, "ThriveStack MCP call failed")
        reply = (f"For {req.domain}: {fb['accepted']} accepted or on a to-do list, {fb['dismissed']} dismissed, "
                 f"{fb['suggested']} not yet decided. {fb['note']}")
        return {"intent": "feedback", "reply": reply, "result": fb}
    if intent["intent"] == "save_hint":
        reply = ("I don't keep earlier results, so I can't save 'these'. Ask me to generate and save in one go, "
                 "for example 'generate 2 earned recommendations and save them', or send the rows to "
                 "POST /v1/recommendations/save.")
        return {"intent": "save_hint", "reply": reply}
    result = await _ask(token, req.domain, req.message)
    if result["refused"]:
        return {"intent": "refused", "reply": result["answer"], "result": {"reason": result["refusal_reason"]}}
    return {"intent": "ask", "reply": result["answer"], "result": result}


@router.get("/brand")
async def brand_profile(domain: Annotated[str, Query(pattern=DOMAIN_PATTERN, max_length=253)], token: str = Depends(get_token)) -> dict:
    """The saved brand profile the agent uses for category-based ideas."""
    from agent import brand

    try:
        return await brand.load_context(token, domain)
    except Exception:
        raise HTTPException(502, "ThriveStack MCP call failed")
