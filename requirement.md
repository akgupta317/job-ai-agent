# Requirements: Content Recommendation Agent

## 1. Purpose

An AI agent that does two jobs for a brand tracked in AI Visibility:

1. **Generate content recommendations**, both **owned** (content on the brand's own site) and **earned** (placements on third-party sites, forums and review platforms), from the brand's latest AI-visibility probe results.
2. **Answer questions about those recommendations**, mainly *"which should I do first, and why?"*

Nothing else: no drafting, no outlines, no outreach emails, no planning tools.

It reuses the logic already in `ai-visibility-api` (`internal/contentrecs`). Finding gaps, scoring and priority stay deterministic in that code; the agent writes the recommendations and explains them.

---

## 2. User Experience and Use Cases

### 2.1 Users
| User | Need |
|---|---|
| CMO / marketing lead | Know what to do first and why |
| SEO / AEO manager | Evidence-backed content ideas |
| PR / community manager | Which outside sites to target |

### 2.2 Use cases

**UC-1. Generate recommendations (automatic)**
- A probe finishes and recommendations are generated without any click.
- The user sees a live "generating" state, then new items in the Recommendations table, tagged **Owned** or **Earned**.

**UC-2. Regenerate (manual)**
- The user can regenerate for a domain. Only new or changed evidence creates or updates items; unchanged ones stay.

**UC-3. Read a recommendation**
- Each item shows: title, owned or earned, topic, content type, priority (HIGH / MED / LOW), owner role, and the reason.
- Evidence is shown: our citations vs. total, competitors winning (owned) or the cited sites and pages (earned).
- Items based only on the Brand Kit, not citation evidence, are labelled as such.

**UC-4. Ask which to prioritise, and why**
- The user asks questions such as:
  - "Which 3 should we do first?"
  - "Why is this one high priority?"
  - "Owned or earned, which matters more right now?"
  - "What should we do for [topic]?"
- The agent answers from the stored evidence and scores, names the specific recommendations, and gives the reason (for example: "we have 0 of 24 citations here while two competitors are cited").
- It does not invent numbers. If the data can't answer, it says so.

**UC-5. Act on recommendations (existing behaviour)**
- Move to To-do, dismiss, or assign, as today. The agent does not change status unless the user confirms.

### 2.3 Rules the agent follows
- Only use facts from the evidence (counts, competitor names, cited sites). No invented stats, competitors or platforms.
- Never repeat a recommendation that is already active.
- Priority comes from the evidence, not from the AI's opinion; the agent explains it, it does not override it.
- Always at least 10 owned and 10 earned recommendations per run; when evidence is thin, fill from the Brand Kit and label them.

### 2.4 Success measures
- Share of recommendations approved vs. dismissed.
- Runs completing without error.
- Share of "which first" answers users rate helpful.
- Cost and time per run.

---

## 3. Technical Components

### 3.1 Overview

```
Probe completes ─► ai-visibility-api (Go)
                    1. finds gaps, scores, dedups, sets priority   (existing code)
                    2. sends candidates + evidence ─► Content Recommendation Agent (Python, LangGraph)
                                                        a. writes recommendations
                                                        b. checks and fixes them
                    3. saves rows, records cost, sends realtime event   (existing code)

User question in UI ─► ai-visibility-api ─► Agent (answers from stored recommendations + evidence)
```

### 3.2 Components

| Component | Role |
|---|---|
| **Existing Go logic** (kept) | Gap detection (silent / mentioned_uncited / losing_share), gap score, priority, earned source classification, dedup, floors, persistence, job tracking, realtime events |
| **Generate graph** | Writes title, reason, owner role and content type for owned and earned candidates; validates; repairs failures; returns recommendations and a list of dropped items |
| **Question-answer graph** | Loads the domain's active recommendations and evidence, ranks them using stored priority and gap score, and answers prioritisation questions in plain language |
| **Tools (read-only)** | `list_recommendations(domain)`, `get_recommendation(id)`, `get_evidence(id)`, `get_brand_context(domain)` |
| **Validators** | Port of the existing checks: allowed values, no competitor or platform outside the evidence, no invented numbers |
| **Model layer** | Config-driven model per task, structured output, per-run cost limit, usage returned to Go for `audit_costs` |
| **UI** | Existing Recommendations table and detail view; one added chat input for questions (UC-4) |
| **Observability** | Trace per run, with cost, latency and drop counts |
| **Evals** | Test set of real probe runs to check output quality before release |

### 3.3 Interfaces

- Go → Agent: `POST /v1/generate` with candidates, evidence, active titles and brand context. Returns recommendations, dropped items and token usage.
- UI → Go → Agent: `POST /ai-visibility/content-recommendations/ask` with a question and the domain. Returns the answer plus the recommendation IDs it refers to, so the UI can link to them.
- No database changes required; the agent returns the same fields Go already stores.

### 3.4 Fallback and safety
- The existing Go generation stays as a fallback behind a feature flag.
- The agent only reads data; changing a recommendation's status or assignment happens through the existing endpoints after the user confirms.
- Each request is scoped to one product and domain, and the agent never sees other customers' data.

### 3.5 Delivery
1. Generate graph behind a flag, run alongside the existing code, and compare output.
2. Switch generation over, keep the fallback.
3. Add the question-and-answer panel.
