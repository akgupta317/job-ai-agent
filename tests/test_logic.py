from agent import candidates as cand
from agent import chat as chat_router
from agent import prompting
from agent.gaps.earned import extract_earned_signals
from agent.gaps.owned import extract_owned_signals, gap_score, priority_from_gap_score
from agent.validators.earned import validate_earned_citation
from agent.validators.owned import validate_owned_citation
from agent.writers import run_batches


def q(pid, our, total, brands=(), cites=(), topic="T"):
    return {"prompt_id": pid, "result_id": "r" + pid, "topic": topic, "prompt_text": "q " + pid,
            "our_citations": our, "total_citations": total,
            "brands": [{"name": n, "brand_domain": d, "brand_rank": i + 1, "is_customer": c} for i, (n, d, c) in enumerate(brands)],
            "citations": [{"citation_domain": d, "title": t, "uri": u} for d, t, u in cites]}


def test_gap_score_and_priority():
    assert gap_score("silent", 11) == 9 and priority_from_gap_score(9) == "HIGH"
    assert gap_score("losing_share", 3) == 3 and priority_from_gap_score(3) == "LOW"
    assert gap_score("silent", 50) == 10


def test_tiers():
    qs = [q("a", 0, 5), q("b", 0, 5, [("Us", "us.com", True)]), q("c", 2, 5, [("Rival", "r.com", False)]), q("d", 0, 0)]
    tiers = {s["prompt_id"]: s["severity_tier"] for s in extract_owned_signals(qs)}
    assert tiers == {"a": "silent", "b": "mentioned_uncited", "c": "losing_share"}


def test_earned_threshold_and_exclusions():
    cites = [("reddit.com", "t", "u1"), ("reddit.com", "t2", "u2"), ("us.com", "own", "u3"), ("rival.com", "x", "u4"),
             ("lonely.com", "x", "u5")]
    qs = [q("a", 0, 5, [("Rival", "rival.com", False)], cites)]
    out = extract_earned_signals(qs, "us.com")
    assert [(c["source_type"], c["evidence_domains"][0], c["tier"]) for c in out] == [("UGC", "reddit.com", "high")]


def test_dedup_and_best_per_prompt():
    sigs = [{"prompt_id": "a", "gap_score": 5, "total_citations": 1}, {"prompt_id": "a", "gap_score": 9, "total_citations": 1}]
    assert cand.best_owned_per_prompt(sigs)[0]["gap_score"] == 9
    s = {"prompt_id": "a", "severity_tier": "silent", "gap_score": 9}
    assert cand.filter_unchanged_owned([s], [{"prompt_id": "a", "severity_tier": "silent", "gap_score": 9}]) == ([], 1)
    assert cand.filter_unchanged_owned([s], [{"prompt_id": "a", "severity_tier": "silent", "gap_score": 7}])[1] == 0


def owned_sig(pid, comps):
    return {"prompt_id": pid, "result_id": "r", "topic": "T", "prompt_text": "q", "severity_tier": "silent", "gap_score": 9,
            "our_citations": 0, "total_citations": 11, "citation_domains": ["x.com"], "evidence_type": "citation_backed",
            "competitors": [{"name": n, "domain": "", "rank": i + 1} for i, n in enumerate(comps)]}


def test_owned_validation_drops_foreign_competitor_and_fixes_enums():
    sigs = [owned_sig("a", ["Peec"]), owned_sig("b", ["Otterly"])]
    replies = [{"prompt_id": "a", "title": "T1", "rationale": "Otterly wins here", "role": "bogus", "content_type": "??"},
               {"prompt_id": "b", "title": "T2", "rationale": "Otterly wins", "channel": "Reddit"}]
    rows, dropped = validate_owned_citation(sigs, replies)
    assert [d["id"] for d in dropped] == ["a"] and "outside" in dropped[0]["reason"]
    assert rows[0]["channel"] == "Blog" and rows[0]["priority"] == "HIGH" and rows[0]["role"] == "CMO & SEO Manager"


def test_earned_validation_blocks_other_domain():
    c1 = {"id": "e1", "topic": "T", "source_type": "Editorial", "tier": "high", "source_usage": 5, "our_citations": 0,
          "total_citations": 9, "evidence_domains": ["alpha.com"], "evidence_pages": [], "evidence_type": "citation_backed"}
    c2 = {**c1, "id": "e2", "evidence_domains": ["beta.com"]}
    rows, dropped = validate_earned_citation([c1, c2], [
        {"cluster_id": "e1", "title": "Pitch alpha.com", "rationale": "ok"},
        {"cluster_id": "e2", "title": "Pitch beta.com", "rationale": "also see alpha.com"}])
    assert [r["channel"] for r in rows] == ["Alpha"] and dropped[0]["id"] == "e2"


def test_repair_retries_only_grounding_failures(monkeypatch):
    calls = []

    def fake_chat_json(prompt, model=None):
        calls.append(prompt)
        if len(calls) == 1:
            return {"recommendations": [{"prompt_id": "a", "title": "T", "rationale": "Otterly wins"}]}, {"tokens_in": 1, "tokens_out": 1}
        return {"recommendations": [{"prompt_id": "a", "title": "T", "rationale": "Peec wins"}]}, {"tokens_in": 1, "tokens_out": 1}

    monkeypatch.setattr("agent.llm.ollama.chat_json", fake_chat_json)
    sigs = [owned_sig("a", ["Peec"]), owned_sig("b", ["Otterly"])]
    rows, dropped, usage = run_batches(sigs[:1] + sigs[1:], lambda ch, note: "p" + note,
                                       validate_owned_citation, batch_size=5)
    assert len(calls) == 2 and "previous answer had problems" in calls[1]
    assert [r["prompt_id"] for r in rows] == ["a"] and usage["repaired"] == 1


def test_prompt_builds_without_missing_values():
    s = owned_sig("a", ["Peec"])
    text = prompting.owned_citation("x.com", [s], ["Old title"])
    assert "prompt_id: a" in text and "Old title" in text and "<<" not in text
    e = {"id": "e1", "topic": "T", "source_type": "UGC", "tier": "high", "source_usage": 3, "our_citations": 0,
         "total_citations": 5, "evidence_domains": ["reddit.com"], "evidence_type": "citation_backed",
         "evidence_pages": [{"title": "P", "uri": "https://r/1", "domain": "reddit.com"}]}
    assert "<<" not in prompting.earned_citation("x.com", [e], [])


def test_chat_router():
    assert chat_router.parse("generate earned content recommendations") == {"intent": "generate", "kinds": ["earned"], "max_per_side": None, "save": False}
    assert chat_router.parse("create 3 owned recommendations")["kinds"] == ["owned"]
    assert chat_router.parse("generate recommendations")["kinds"] == ["owned", "earned"]
    assert chat_router.parse("which should we do first and why?")["intent"] == "ask"
    assert chat_router.parse("which of the recommendations is most important?")["intent"] == "ask"


def test_brand_info_maps_to_prompt_context():
    from agent.brand import to_context
    ctx = to_context({"industry": "B2B SaaS", "value_offering": "v", "competitors": ["a.com"], "intent_commercial": 55,
                      "ai_topics": ["t"], "language": "English"})
    assert ctx["category"] == "B2B SaaS" and ctx["named_competitors"] == ["a.com"] and ctx["intent_commercial_pct"] == 55
    text = prompting.owned_category("x.com", [{"prompt_id": "p"}], [], ctx, ["t"])
    assert "Category: B2B SaaS. v" in text and "55% commercial" in text and "<<" not in text


def test_llm_ping_handles_chat_returning_text_and_usage(monkeypatch):
    from fastapi.testclient import TestClient

    from agent.main import app

    monkeypatch.setattr("agent.llm.ollama.is_up", lambda: True)
    monkeypatch.setattr("agent.llm.ollama.chat", lambda prompt, system=None, model=None: (" pong \n", {"tokens_in": 1, "tokens_out": 1}))
    r = TestClient(app).get("/llm/ping")
    assert r.status_code == 200 and r.json()["reply"] == "pong"


def test_existing_titles_capped_with_same_topic_first():
    active = [{"prompt_id": f"p{i}", "title": f"title {i}", "topic": "other"} for i in range(30)]
    active += [{"prompt_id": "m1", "title": "same topic 1", "topic": "T"}, {"prompt_id": "m2", "title": "same topic 2", "topic": "T"},
               {"prompt_id": "inbatch", "title": "own old title", "topic": "T"}]
    titles = cand.titles_excluding(active, {"inbatch"}, {"T"}, 5)
    assert titles[:2] == ["same topic 1", "same topic 2"] and len(titles) == 5 and "own old title" not in titles
    assert len(cand.titles_excluding(active, {"inbatch"})) == 32          # no limit = old behaviour
    assert cand.titles_excluding(active, set(), None, 3) == ["title 0", "title 1", "title 2"]


def test_capped_titles_shrink_the_prompt():
    sig = owned_sig("a", ["Peec"])
    active = [{"prompt_id": f"p{i}", "title": "A fairly long existing recommendation title number %d about AEO tools" % i, "topic": "T"}
              for i in range(80)]
    full = prompting.owned_citation("x.com", [sig], cand.titles_excluding(active, {"a"}))
    capped = prompting.owned_citation("x.com", [sig], cand.titles_excluding(active, {"a"}, {"T"}, 15))
    assert len(capped) < len(full) * 0.4
