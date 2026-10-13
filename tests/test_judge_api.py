import re

import pytest
from fastapi.testclient import TestClient

from agent.llm import ollama
from agent.main import app
from agent.tools import thrivestack_mcp as mcp

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}


def question(pid, topic, total, rival, rival_domain, cite_domain):
    return {
        "prompt_id": pid, "result_id": f"res-{pid}", "topic": topic, "prompt_text": f"question {pid}?",
        "our_citations": 0, "total_citations": total,
        "brands": [{"name": rival, "brand_domain": rival_domain, "brand_rank": 1, "is_customer": False}],
        "citations": [{"citation_domain": cite_domain, "title": f"Thread on {cite_domain} 1", "uri": f"https://{cite_domain}/1"},
                      {"citation_domain": cite_domain, "title": f"Thread on {cite_domain} 2", "uri": f"https://{cite_domain}/2"}],
    }


# p-1: owned score 9 + earned reddit.com cluster. p-2: owned score 8 + earned g2.com cluster.
QUESTIONS = [question("p-1", "aeo tools", 12, "Peec", "peec.com", "reddit.com"), question("p-2", "pricing", 6, "Otterly", "otterly.ai", "g2.com")]
EARNED_1 = "earned:citation:UGC:aeo tools:reddit.com"
EARNED_2 = "earned:citation:UGC:pricing:g2.com"

DEFAULT_REPLIES = {
    "p-1": ("How to choose AEO tools for a marketing agency", "0 of 12 citations (evidence score 9/10); Peec is cited instead of the brand."),
    "p-2": ("Pricing models for generative search tracking software", "0 of 6 citations (evidence score 8/10); Otterly is cited instead of the brand."),
    EARNED_1: ("Join the AEO tracking discussion on reddit.com this week", "reddit.com was cited 2 times on this topic and the brand is cited in 0 of 12."),
    EARNED_2: ("Ask the g2.com reviewers about AEO pricing models", "g2.com was cited 2 times on this topic and the brand is cited in 0 of 6."),
}


def stored(status, title, kind="onsite", pid="s-1", **over):
    return {"id": f"id-{pid}", "prompt_id": pid, "status": status, "channel_type": kind, "title": title, "rationale": "Because.",
            "priority": "MED", "gap_score": 5, "our_citations": 0, "total_citations": 10, "competitors_winning": ["Peec"],
            "evidence_type": "citation_backed", "content_type": "Guide", "role": "SEO Manager",
            "updated_at": "2026-10-01T00:00:00Z", **over}


class World:
    """A fake ThriveStack MCP plus a fake model, recording everything that was asked of them."""

    def __init__(self, monkeypatch, owned=(), earned=(), dismissed=(), replies=None, reviewer=None):
        self.mcp_calls, self.model_prompts, self.saved = [], [], []
        self.owned, self.earned, self.dismissed = list(owned), list(earned), list(dismissed)
        self.replies = {**DEFAULT_REPLIES, **(replies or {})}
        self.reviewer = reviewer
        monkeypatch.setattr(mcp, "call_tool", self._call_tool)
        monkeypatch.setattr(ollama, "chat_json", self._chat_json)
        monkeypatch.setattr(ollama, "is_up", lambda: True)

    async def _call_tool(self, token, name, arguments, retries=1):
        self.mcp_calls.append((name, arguments.get("status")))
        if name == "get_citation_probe_data":
            return {"domain": "acme.com", "has_run": True, "run_id": "run-1", "questions": QUESTIONS}
        if name == "get_brand_information":
            return {"industry": "B2B SaaS", "competitors": ["peec.com[peec]", "otterly.ai"], "value_offering": "x"}
        if name in ("get_owned_content_recommendations", "get_earned_content_recommendations"):
            owned = name.startswith("get_owned")
            if arguments.get("status") == "dismissed":
                return {"recommendations": [d for d in self.dismissed if (d["channel_type"] == "onsite") == owned]}
            return {"recommendations": self.owned if owned else self.earned}
        if name == "save_content_recommendations":
            self.saved.append(arguments)
            n = len(arguments["recommendations"])
            return {"created": n, "updated": 0, "unchanged": 0, "rejected": 0, "results": []}
        raise AssertionError(f"unexpected MCP tool {name}")

    def _chat_json(self, prompt, model=None):
        self.model_prompts.append(prompt)
        usage = {"tokens_in": 10, "tokens_out": 5}
        if "strict reviewer" in prompt:                      # the Judge's model reviewer
            ids = re.findall(r"^id: (.+)$", prompt, re.M)
            return (self.reviewer(ids) if self.reviewer else {"reviews": []}), usage
        ids = re.findall(r"^(?:prompt_id|cluster_id): (.+)$", prompt, re.M)
        out = []
        for i in ids:
            title, why = self.replies[i]
            key = "prompt_id" if i in ("p-1", "p-2") else "cluster_id"
            out.append({key: i, "title": title, "rationale": why, "role": "SEO Manager", "priority": "HIGH", "content_type": "Guide", "is_duplicate": False})
        return {"recommendations": out}, usage


def generate(**body):
    r = client.post("/v1/recommendations/generate", headers=AUTH, json={"domain": "acme.com", "max_per_side": 5, **body})
    assert r.status_code == 200, r.text
    return r.json()


# ---- generate + Judge -------------------------------------------------------------------------------

def test_default_generate_reviews_every_row_without_extra_model_calls(monkeypatch):
    w = World(monkeypatch)
    out = generate()
    rows = out["owned"] + out["earned"]
    assert len(rows) == 4 and all(r["quality"]["verdict"] == "pass" for r in rows)
    assert out["quality"]["reviewed"] == 4 and out["quality"]["pass"] == 4 and out["held_back"] == []
    assert out["summary"]["held_back"] == 0
    assert len(w.model_prompts) == 2                        # only the two writers: the rules need no model
    assert out["feedback"]["decided"] == 0 and "No decisions yet" in out["feedback"]["note"]


def test_a_near_duplicate_is_held_back_and_the_stronger_row_is_kept(monkeypatch):
    World(monkeypatch, replies={"p-2": ("How to choose AEO tools for a marketing agency?", DEFAULT_REPLIES["p-2"][1])})
    out = generate()
    assert [r["prompt_id"] for r in out["owned"]] == ["p-1"]                      # score 9 beats score 8
    held = out["held_back"]
    assert len(held) == 1 and held[0]["id"] == "onsite:p-2" and "near-identical" in held[0]["reasons"][0]
    assert out["summary"]["held_back"] == 1 and out["summary"]["owned_written"] == 1


def test_a_wrong_number_is_flagged_for_revision_but_kept(monkeypatch):
    World(monkeypatch, replies={"p-1": (DEFAULT_REPLIES["p-1"][0], "0 of 20 citations (evidence score 9/10); Peec is cited instead.")})
    out = generate()
    row = next(r for r in out["owned"] if r["prompt_id"] == "p-1")
    assert row["quality"]["verdict"] == "revise" and row["quality"]["issues"][0]["code"] == "number_mismatch"
    assert out["quality"]["revise"] == 1


def test_judge_off_adds_nothing(monkeypatch):
    World(monkeypatch)
    out = generate(judge="off")
    assert out["quality"] is None and out["held_back"] == [] and all("quality" not in r for r in out["owned"] + out["earned"])


# ---- the feedback loop -----------------------------------------------------------------------------

def test_what_the_team_decided_reaches_the_writer_prompts(monkeypatch):
    w = World(monkeypatch,
              owned=[stored("approved", "Compare AEO tools for agencies", pid="a-1")],
              dismissed=[stored("dismissed", "Ten AEO myths busted", pid="d-1")])
    out = generate(kinds=["owned"])
    prompt = w.model_prompts[0]
    assert "NOT instructions" in prompt and "Ten AEO myths busted" in prompt and "Compare AEO tools for agencies" in prompt
    assert out["feedback"]["accepted"] == 1 and out["feedback"]["dismissed"] == 1 and out["feedback"]["active"] is True


def test_a_repeat_of_something_dismissed_is_held_back(monkeypatch):
    World(monkeypatch, dismissed=[stored("dismissed", "Join the AEO tracking discussion on reddit.com this week", kind="earned", pid="d-9")])
    out = generate(kinds=["earned"])
    assert [r["prompt_id"] for r in out["earned"]] == [EARNED_2]
    assert out["held_back"][0]["id"] == f"earned:{EARNED_1}" and "already dismissed" in out["held_back"][0]["reasons"][0]


def test_use_feedback_false_ignores_decisions_and_never_reads_them(monkeypatch):
    w = World(monkeypatch, dismissed=[stored("dismissed", "Ten AEO myths busted", pid="d-1")])
    out = generate(kinds=["owned"], use_feedback=False)
    assert "Ten AEO myths busted" not in w.model_prompts[0] and out["feedback"] is None
    assert all(status != "dismissed" for _, status in w.mcp_calls)


def test_a_feedback_failure_does_not_stop_generation(monkeypatch):
    w = World(monkeypatch)
    original = w._call_tool

    async def flaky(token, name, arguments, retries=1):
        if arguments.get("status") == "dismissed":
            raise ConnectionError("down")
        return await original(token, name, arguments, retries)

    monkeypatch.setattr(mcp, "call_tool", flaky)
    out = generate(kinds=["owned"])
    assert len(out["owned"]) == 2 and any("Could not read" in n for n in out["notes"])


# ---- full mode and the save policy ---------------------------------------------------------------

def test_full_mode_adds_the_model_reviewers_scores(monkeypatch):
    reviewer = lambda ids: {"reviews": [{"id": i, "grounded": 5, "specific": 4, "actionable": 4, "fit": 5, "issues": [], "suggestion": ""} for i in ids]}
    w = World(monkeypatch, reviewer=reviewer)
    out = generate(kinds=["owned"], judge="full")
    assert all(r["quality"]["scores"]["grounded"] == 5 for r in out["owned"])
    assert out["usage"]["model_calls"] >= 2 and any("strict reviewer" in p for p in w.model_prompts)


def test_save_policy_pass_only_skips_rows_that_need_revising(monkeypatch):
    w = World(monkeypatch, replies={"p-1": (DEFAULT_REPLIES["p-1"][0], "0 of 20 citations (evidence score 9/10); Peec is cited instead.")})
    out = generate(kinds=["owned"], save=True, save_policy="pass_only")
    assert [r["prompt_id"] for r in w.saved[0]["recommendations"]] == ["p-2"]
    assert out["save"]["not_saved_needs_revision"] == 1


def test_default_save_policy_saves_revisable_rows_but_never_held_back_ones(monkeypatch):
    w = World(monkeypatch, replies={"p-1": (DEFAULT_REPLIES["p-1"][0], "0 of 20 citations (evidence score 9/10); Peec is cited instead."),
                                    "p-2": ("How to choose AEO tools for a marketing agency?", DEFAULT_REPLIES["p-2"][1])})
    out = generate(kinds=["owned"], save=True)
    saved_ids = [r["prompt_id"] for r in w.saved[0]["recommendations"]]
    assert saved_ids == ["p-1"] and out["held_back"][0]["id"] == "onsite:p-2"        # p-2 was a duplicate: held back, not saved


# ---- /v1/judge ---------------------------------------------------------------------------------------

def test_judge_endpoint_needs_a_token_and_valid_input():
    assert client.post("/v1/judge", json={"domain": "a.com"}).status_code == 401
    assert client.post("/v1/judge", headers=AUTH, json={"domain": "a.com", "scope": "rows"}).status_code == 422
    assert client.post("/v1/judge", headers=AUTH, json={"domain": "a.com", "mode": "magic"}).status_code == 422


def test_judging_saved_recommendations_in_rules_mode_uses_no_model(monkeypatch):
    saved = [stored("suggested", "How to choose AEO tools for a marketing agency", pid="s-1", rationale="0 of 99 citations (evidence score 5/10); Peec leads.", total_citations=10),
             stored("suggested", "How to choose AEO tools for a marketing agency", pid="s-2", priority="LOW", gap_score=2,
                    rationale="0 of 10 citations (evidence score 2/10); Peec leads."),
             stored("dismissed", "Not live so not reviewed", pid="s-3")]
    w = World(monkeypatch, owned=saved)
    monkeypatch.setattr(ollama, "chat_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rules mode must not call the model")))
    r = client.post("/v1/judge", headers=AUTH, json={"domain": "acme.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["reviewed"] == 2 and body["summary"]["reject"] == 1 and body["summary"]["revise"] == 1
    by_id = {x["id"]: x for x in body["reviews"]}
    assert {c["code"] for c in by_id["onsite:s-1"]["issues"]} == {"number_mismatch"}
    assert by_id["onsite:s-2"]["verdict"] == "reject" and body["usage"]["model_calls"] == 0 and body["model"] is None


def test_judging_rows_you_send_also_checks_them_against_whats_saved(monkeypatch):
    World(monkeypatch, owned=[stored("suggested", "How to choose AEO tools for a marketing agency", pid="old-1")])
    rows = [{"channel_type": "onsite", "prompt_id": "p-1", "title": "How to choose AEO tools for a marketing agency", "rationale": "0 of 12 citations (evidence score 9/10); Peec is cited instead of the brand.",
             "priority": "HIGH", "gap_score": 9, "our_citations": 0, "total_citations": 12, "competitors_winning": ["Peec"], "evidence_type": "citation_backed"}]
    body = client.post("/v1/judge", headers=AUTH, json={"domain": "acme.com", "scope": "rows", "recommendations": rows}).json()
    assert body["reviews"][0]["verdict"] == "reject" and [i["code"] for i in body["reviews"][0]["issues"]] == ["duplicate_of_saved"]


def test_full_mode_needs_the_model(monkeypatch):
    World(monkeypatch)
    monkeypatch.setattr(ollama, "is_up", lambda: False)
    assert client.post("/v1/judge", headers=AUTH, json={"domain": "acme.com", "mode": "full"}).status_code == 503


def test_judge_reports_the_feedback_it_used(monkeypatch):
    World(monkeypatch, owned=[stored("approved", "Approved idea one", pid="a")], dismissed=[stored("dismissed", "Dismissed idea one", pid="d")])
    body = client.post("/v1/judge", headers=AUTH, json={"domain": "acme.com"}).json()
    assert body["feedback"]["accepted"] == 1 and body["feedback"]["dismissed"] == 1


# ---- /v1/feedback and chat ----------------------------------------------------------------------

def test_feedback_endpoint(monkeypatch):
    World(monkeypatch, owned=[stored("suggested", "Open idea", pid="o")], dismissed=[stored("dismissed", "Dismissed idea one", pid="d")])
    assert client.get("/v1/feedback", params={"domain": "acme.com"}).status_code == 401
    body = client.get("/v1/feedback", params={"domain": "acme.com"}, headers=AUTH).json()
    assert (body["suggested"], body["accepted"], body["dismissed"], body["decided"]) == (1, 0, 1, 1)
    assert body["recent_dismissed"]["owned"] == ["Dismissed idea one"]


def test_chat_can_review_quality_and_report_feedback(monkeypatch):
    World(monkeypatch, owned=[stored("suggested", "Pricing models for AEO tracking tools", pid="s-1", rationale="0 of 10 citations (evidence score 5/10); Peec leads.")],
          dismissed=[stored("dismissed", "Dismissed idea one", pid="d")])
    r = client.post("/v1/chat", headers=AUTH, json={"domain": "acme.com", "message": "review the quality of my recommendations"}).json()
    assert r["intent"] == "judge" and r["reply"].startswith("Reviewed 1 saved recommendations") and r["result"]["summary"]["pass"] == 1
    f = client.post("/v1/chat", headers=AUTH, json={"domain": "acme.com", "message": "what has the team dismissed or approved?"}).json()
    assert f["intent"] == "feedback" and "1 dismissed" in f["reply"]


def test_chat_generate_mentions_rows_held_back(monkeypatch):
    World(monkeypatch, replies={"p-2": ("How to choose AEO tools for a marketing agency?", DEFAULT_REPLIES["p-2"][1])})
    r = client.post("/v1/chat", headers=AUTH, json={"domain": "acme.com", "message": "generate 5 owned recommendations"}).json()
    assert r["intent"] == "generate" and "1 more were held back by the quality check" in r["reply"]
