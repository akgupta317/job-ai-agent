import asyncio

import pytest
from fastapi.testclient import TestClient

from agent import chat as chat_router
from agent import saving
from agent.main import app
from agent.tools import thrivestack_mcp as mcp

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}


def row(i=1, **extra):
    return {
        "channel_type": "onsite", "prompt_id": f"p-{i}", "title": f"Title {i}", "rationale": "Because.",
        "role": "SEO Manager", "content_type": "Guide", "evidence_type": "citation_backed",
        # evidence a generate step returns: must never be sent on
        "gap_score": 9, "severity_tier": "silent", "our_citations": 0, "total_citations": 12,
        "competitors_winning": ["Peec"], "source_url": "https://x.example", "status": "suggested", **extra,
    }


def fake_tool(calls, reply=None):
    async def call_tool(token, name, arguments, retries=1):
        calls.append((token, name, arguments))
        n = len(arguments["recommendations"])
        return reply or {"created": n, "updated": 0, "unchanged": 0, "rejected": 0, "probe_run_id": "run-1",
                         "results": [{"prompt_id": r["prompt_id"], "outcome": "created"} for r in arguments["recommendations"]]}
    return call_tool


def test_only_accepted_fields_are_sent():
    assert set(saving.pick_fields(row())) == {"channel_type", "prompt_id", "title", "rationale", "role", "content_type", "evidence_type"}
    assert saving.pick_fields({"title": "t", "topic": None}) == {"title": "t"}


def test_save_calls_the_mcp_tool_with_the_callers_token(monkeypatch):
    calls = []
    monkeypatch.setattr(saving.mcp, "call_tool", fake_tool(calls))
    out = asyncio.run(saving.save_recommendations("tok", "acme.com", "run-1", [row(1), row(2)]))
    token, name, args = calls[0]
    assert (token, name) == ("tok", "save_content_recommendations")
    assert args["domain"] == "acme.com" and args["probe_run_id"] == "run-1" and len(args["recommendations"]) == 2
    assert "gap_score" not in args["recommendations"][0]
    assert out["created"] == 2 and len(out["results"]) == 2


def test_no_probe_run_id_is_omitted(monkeypatch):
    calls = []
    monkeypatch.setattr(saving.mcp, "call_tool", fake_tool(calls))
    asyncio.run(saving.save_recommendations("tok", "acme.com", None, [row()]))
    assert "probe_run_id" not in calls[0][2]


def test_more_than_30_rows_are_split_and_the_counts_added(monkeypatch):
    calls = []
    monkeypatch.setattr(saving.mcp, "call_tool", fake_tool(calls))
    out = asyncio.run(saving.save_recommendations("tok", "acme.com", "run-1", [row(i) for i in range(45)]))
    assert [len(c[2]["recommendations"]) for c in calls] == [30, 15]
    assert out["created"] == 45 and len(out["results"]) == 45


def test_a_tool_reported_error_is_not_retried_but_a_network_error_is(monkeypatch):
    attempts = {"n": 0}

    async def tool_error(token, name, arguments):
        attempts["n"] += 1
        raise mcp.McpToolError("MCP tool x failed: nope")

    monkeypatch.setattr(mcp, "_call_tool_once", tool_error)
    with pytest.raises(mcp.McpToolError):
        asyncio.run(mcp.call_tool("t", "x", {}, retries=1))
    assert attempts["n"] == 1

    attempts["n"] = 0

    async def flaky(token, name, arguments):
        attempts["n"] += 1
        raise ConnectionError("blip")

    monkeypatch.setattr(mcp, "_call_tool_once", flaky)
    with pytest.raises(ConnectionError):
        asyncio.run(mcp.call_tool("t", "x", {}, retries=1))
    assert attempts["n"] == 2


def test_save_endpoint_needs_a_token():
    assert client.post("/v1/recommendations/save", json={"domain": "a.com", "recommendations": [row()]}).status_code == 401


def test_save_endpoint_validates_the_body():
    assert client.post("/v1/recommendations/save", headers=AUTH, json={"domain": "a.com", "recommendations": []}).status_code == 422
    assert client.post("/v1/recommendations/save", headers=AUTH, json={"recommendations": [row()]}).status_code == 422


def test_save_endpoint_success(monkeypatch):
    monkeypatch.setattr(saving.mcp, "call_tool", fake_tool([]))
    r = client.post("/v1/recommendations/save", headers=AUTH, json={"domain": "a.com", "run_id": "run-1", "recommendations": [row()]})
    assert r.status_code == 200 and r.json()["created"] == 1


@pytest.mark.parametrize("message,status", [
    ("MCP tool save_content_recommendations failed: you don't have access to domain \"x.com\"", 403),
    ("MCP tool save_content_recommendations failed: ai-visibility-api returned 404: probe_not_found: probe run not found for this brand", 404),
    ("MCP tool save_content_recommendations failed: subscription_paused: AI Visibility subscription is paused", 409),
    ("MCP tool save_content_recommendations failed: invalid_request: at most 30", 422),
    ("MCP tool save_content_recommendations failed: something else", 502),
])
def test_save_errors_map_to_useful_statuses(monkeypatch, message, status):
    async def boom(*a, **k):
        raise mcp.McpToolError(message)
    monkeypatch.setattr(saving.mcp, "call_tool", boom)
    r = client.post("/v1/recommendations/save", headers=AUTH, json={"domain": "a.com", "recommendations": [row()]})
    assert r.status_code == status and "failed" in r.json()["detail"]


def fake_generate_result():
    return {"domain": "a.com", "run_id": "run-1", "model": "m", "owned": [row(1)], "earned": [],
            "dropped": [], "notes": [], "usage": {},
            "summary": {"owned_written": 1, "earned_written": 0, "dropped": 0}}


@pytest.fixture
def generate_stub(monkeypatch):
    async def run_generate(*a, **k):
        return fake_generate_result()
    monkeypatch.setattr("agent.api.v1.run_generate", run_generate)
    monkeypatch.setattr("agent.llm.ollama.is_up", lambda: True)


def test_generate_without_save_does_not_touch_the_mcp(generate_stub, monkeypatch):
    calls = []
    monkeypatch.setattr(saving.mcp, "call_tool", fake_tool(calls))
    r = client.post("/v1/recommendations/generate", headers=AUTH, json={"domain": "a.com"})
    assert r.status_code == 200 and "save" not in r.json() and calls == []


def test_generate_with_save_attaches_the_outcome(generate_stub, monkeypatch):
    calls = []
    monkeypatch.setattr(saving.mcp, "call_tool", fake_tool(calls))
    r = client.post("/v1/recommendations/generate", headers=AUTH, json={"domain": "a.com", "save": True})
    body = r.json()
    assert r.status_code == 200 and body["save"]["created"] == 1 and len(body["owned"]) == 1
    assert calls[0][2]["probe_run_id"] == "run-1"


def test_a_failed_save_keeps_the_generated_rows(generate_stub, monkeypatch):
    async def boom(*a, **k):
        raise mcp.McpToolError("MCP tool save_content_recommendations failed: nope")
    monkeypatch.setattr(saving.mcp, "call_tool", boom)
    r = client.post("/v1/recommendations/generate", headers=AUTH, json={"domain": "a.com", "save": True})
    body = r.json()
    assert r.status_code == 200 and len(body["owned"]) == 1 and "nope" in body["save"]["error"]


def test_chat_parse_save_flag():
    p = chat_router.parse("generate 2 earned recommendations and save them")
    assert p == {"intent": "generate", "kinds": ["earned"], "max_per_side": 2, "save": True}
    assert chat_router.parse("generate 2 earned recommendations")["save"] is False
    assert chat_router.parse("save these")["intent"] == "save_hint"
    assert chat_router.parse("which should we save first?")["intent"] == "ask"


def test_chat_generate_and_save_reply(generate_stub, monkeypatch):
    monkeypatch.setattr(saving.mcp, "call_tool", fake_tool([]))
    r = client.post("/v1/chat", headers=AUTH, json={"domain": "a.com", "message": "generate 1 owned recommendation and save it"})
    assert r.status_code == 200 and r.json()["intent"] == "generate" and "Saved 1 new" in r.json()["reply"]


def test_chat_save_these_explains_instead_of_guessing(monkeypatch):
    called = []
    monkeypatch.setattr(saving.mcp, "call_tool", fake_tool(called))
    r = client.post("/v1/chat", headers=AUTH, json={"domain": "a.com", "message": "save these"})
    assert r.json()["intent"] == "save_hint" and "/v1/recommendations/save" in r.json()["reply"] and called == []
