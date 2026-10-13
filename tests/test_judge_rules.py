from agent.feedback import Feedback
from agent.judge import core
from agent.judge.rules import check_row, score_and_verdict


def owned(**over):
    row = {
        "channel_type": "onsite", "prompt_id": "p-1", "topic": "AEO tools", "priority": "HIGH", "gap_score": 9,
        "title": "How to choose AEO tools for a marketing agency",
        "rationale": "0 of 12 citations (evidence score 9/10); Peec is cited instead of the brand.",
        "our_citations": 0, "total_citations": 12, "competitors_winning": ["Peec"], "evidence_type": "citation_backed",
    }
    return {**row, **over}


def earned(**over):
    row = {
        "channel_type": "earned", "prompt_id": "earned:citation:UGC:aeo:reddit.com", "topic": "aeo", "priority": "HIGH",
        "gap_score": 4, "channel": "Reddit", "source_type": "UGC", "source_url": "https://www.reddit.com/r/aeo/1",
        "title": "Join the discussion on reddit.com about AEO tracking tools",
        "rationale": "reddit.com was cited 4 times on this topic and the brand is cited in 0 of 20.",
        "our_citations": 0, "total_citations": 20, "competitors_winning": ["reddit.com"], "evidence_type": "citation_backed",
    }
    return {**row, **over}


def codes(row, **kw):
    return {i.code: i.severity for i in check_row(row, **kw)}


def test_a_clean_row_passes():
    assert check_row(owned()) == []
    assert check_row(earned()) == []
    assert score_and_verdict([]) == (5.0, "pass")


def test_number_that_disagrees_with_the_evidence_is_flagged():
    # real case from the stored data: the text says 0 of 16 while the card shows 0 of 15
    assert codes(owned(rationale="0 of 16 citations (evidence score 9/10); Peec leads.")) == {"number_mismatch": "revise"}
    assert codes(owned(rationale="0 of 12 citations (evidence score 7/10); Peec leads.")) == {"number_mismatch": "revise"}
    assert codes(earned(rationale="reddit.com was cited 9 times and the brand is cited in 0 of 20.")) == {"number_mismatch": "revise"}


def test_citation_counts_in_a_brand_profile_idea_are_invented():
    row = owned(evidence_type="category_inferred", prompt_id="category:idea:t:0", competitors_winning=[],
                rationale="Competitors are cited 0 of 40 times, so this matters for the category.")
    assert codes(row)["invented_number"] == "revise"


def test_domains_must_come_from_the_evidence_the_page_or_the_brand_profile():
    assert "unverified_domain" in codes(earned(title="Get featured on ledgerup.ai this quarter"))
    assert "unverified_domain" not in codes(earned(title="Get featured on reddit.com this quarter"))
    assert "unverified_domain" not in codes(earned(title="Join the thread on old.reddit.com today"))         # subdomain of evidence
    assert "unverified_domain" not in codes(earned(title="Join the r/aeo thread on www.reddit.com today"))   # the cited page's host
    assert "unverified_domain" not in codes(earned(title="Compare notes with peec.ai users today"), allowed_names={"peec.ai"})
    assert "unverified_domain" not in codes(earned(title="Ask the g2.com reviewers about AEO tracking tools"))  # a known review platform


def test_invented_brand_names_are_only_a_note_and_only_for_profile_ideas():
    row = earned(evidence_type="category_inferred", competitors_winning=[], title="Partner with StartupCookie on an AEO playbook",
                 rationale="Shows how ThriveStack unifies telemetry for startup teams worldwide.")
    assert codes(row)["possible_invented_name"] == "note"
    assert "possible_invented_name" not in codes(earned(title="Partner with StartupCookie on an AEO playbook, reddit.com"))


def test_generic_filler_one_phrase_is_a_note_two_is_a_revision():
    assert codes(owned(rationale="0 of 12 citations; this can help establish ourselves as a thought leader."))["generic_filler"] == "revise"
    assert codes(owned(rationale="0 of 12 citations; Peec is cited and a compelling answer is missing."))["generic_filler"] == "note"


def test_a_rationale_that_ignores_the_evidence_is_flagged():
    row = owned(rationale="This is an important topic that many buyers care about a great deal today.")
    assert codes(row)["ignores_evidence"] == "revise"
    assert "ignores_evidence" not in codes(owned(rationale="Buyers ask this and only Peec answers it well today."))


def test_an_earned_title_must_say_where_to_act():
    assert codes(earned(title="Write a thoughtful answer to the AEO tracking question", channel="Reddit"))["missing_target"] == "revise"
    assert "missing_target" not in codes(earned(title="Answer the AEO tracking question on Reddit today"))   # channel name
    row = earned(competitors_winning=["blog.hubspot.com"], channel="Hubspot", title="Pitch inclusion in HubSpot's guide to AEO tools",
                 source_url="https://blog.hubspot.com/x")
    assert "missing_target" not in codes(row)                                                              # brand label of the domain


def test_length_limits():
    assert codes(owned(title="Too short"))["title_too_short"] == "revise"
    assert codes(owned(rationale="0 of 12."))["rationale_too_short"] == "revise"
    assert codes(owned(title="x" * 201))["title_too_long"] == "note"   # style, not an error


def test_near_duplicates_are_rejected_but_only_within_the_same_kind():
    first = owned()
    twin = owned(prompt_id="p-2", title="How to choose AEO tools for a marketing agency?", priority="MED", gap_score=6)
    assert codes(twin, earlier=[first])["duplicate_in_batch"] == "reject"
    assert "duplicate_in_batch" not in codes(earned(title=first["title"] + " on reddit.com"), earlier=[first])
    assert codes(twin, existing=[{"prompt_id": "stored", "title": first["title"]}])["duplicate_of_saved"] == "reject"
    assert "duplicate_of_saved" not in codes(first, existing=[{"prompt_id": "p-1", "title": first["title"]}])   # itself


def test_repeating_what_the_team_dismissed():
    fb = Feedback([], [{"status": "dismissed", "channel_type": "onsite", "title": "How to choose AEO tools for a marketing agency"}])
    assert codes(owned(), feedback=fb)["similar_to_dismissed"] == "reject"
    fb_loose = Feedback([], [{"status": "dismissed", "channel_type": "onsite", "title": "Choosing AEO tools: an agency checklist for buyers"}])
    assert codes(owned(), feedback=fb_loose).get("similar_to_dismissed") in ("note", None)
    fb_earned = Feedback([], [{"status": "dismissed", "channel_type": "earned", "title": owned()["title"]}])
    assert "similar_to_dismissed" not in codes(owned(), feedback=fb_earned)    # other kind


def test_verdicts():
    from agent.judge.rules import Issue
    assert score_and_verdict([Issue("x", "note", "")]) == (4.75, "pass")
    assert score_and_verdict([Issue("x", "revise", ""), Issue("y", "revise", "")]) == (3.0, "revise")
    assert score_and_verdict([Issue("x", "revise", ""), Issue("d", "reject", "")]) == (1.0, "reject")


def test_the_stronger_row_is_kept_and_a_rejected_duplicate_does_not_shield_later_rows():
    weak = owned(prompt_id="weak", priority="LOW", gap_score=3, title="How to choose AEO tools for a marketing agency!")
    strong = owned(prompt_id="strong")
    other = owned(prompt_id="other", title="Pricing models for generative search tracking software", priority="MED", gap_score=6,
                  rationale="0 of 12 citations (evidence score 6/10); Peec is cited instead of the brand.")
    reviews = core.review_rules([weak, other, strong])   # given in a different order
    verdicts = {r.id: r.verdict for r in reviews}
    assert verdicts == {"onsite:weak": "reject", "onsite:other": "pass", "onsite:strong": "pass"}
    assert [r.id for r in reviews] == ["onsite:weak", "onsite:other", "onsite:strong"]   # original order kept


def test_allowed_names_understands_the_profile_format():
    names = core.allowed_names("www.Acme.com", {"named_competitors": ["usermaven.com[usermaven]", "otterly.ai", "Peec AI"]})
    assert {"acme.com", "acme", "usermaven.com", "usermaven", "otterly.ai", "otterly", "peec"} <= names
