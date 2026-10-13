# AI Visibility Agent 

Generates owned (on-site) and earned (third-party) content recommendations from a brand's
latest AI-visibility probe, and answers questions such as "which should we do first, and why?".
Data comes from the ThriveStack MCP; the model runs on Ollama.

## Run
```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
cp .env.example .env            # set OLLAMA_MODEL if you want a different model
ollama serve & ollama pull llama3.1:8b
make run                        # http://localhost:5301  (docs at /docs)
make test
```

## Use
Every `/v1` and `/mcp` call needs the caller's token: `Authorization: Bearer <token>`.
(For local testing only, set `ALLOW_ENV_TOKEN=true` and `THRIVESTACK_MCP_TOKEN` in `.env`.)

```sh
H='-H "Authorization: Bearer $TOKEN" -H "content-type: application/json"'

# Ask in plain English (generates, or answers a question)
curl -X POST localhost:5301/v1/chat $H -d '{"domain":"thrivestack.ai","message":"generate 3 earned recommendations"}'
curl -X POST localhost:5301/v1/chat $H -d '{"domain":"thrivestack.ai","message":"which should we do first and why?"}'

# Direct endpoints
curl -X POST localhost:5301/v1/recommendations/generate $H -d '{"domain":"thrivestack.ai","kinds":["owned","earned"],"max_per_side":5}'
curl -X POST localhost:5301/v1/ask $H -d '{"domain":"thrivestack.ai","question":"owned or earned first?"}'

# Generate and save in one go (rows are stored as 'suggested' through the MCP tool + API)
curl -X POST localhost:5301/v1/chat $H -d '{"domain":"thrivestack.ai","message":"generate 2 earned recommendations and save them"}'
curl -X POST localhost:5301/v1/recommendations/generate $H -d '{"domain":"thrivestack.ai","kinds":["earned"],"max_per_side":2,"save":true}'

# Save rows from an earlier generate call (the agent keeps no results between calls)
curl -X POST localhost:5301/v1/recommendations/save $H -d '{"domain":"thrivestack.ai","run_id":"<run_id from generate>","recommendations":[...]}'

# Quality: the Judge reviews recommendations (rules need no model; full adds a model reviewer)
curl -X POST localhost:5301/v1/judge $H -d '{"domain":"thrivestack.ai"}'                              # review what is saved
curl -X POST localhost:5301/v1/judge $H -d '{"domain":"thrivestack.ai","scope":"rows","recommendations":[...]}'
curl -X POST localhost:5301/v1/judge $H -d '{"domain":"thrivestack.ai","mode":"full","revise":true}'  # slow on CPU
curl -X POST localhost:5301/v1/chat $H -d '{"domain":"thrivestack.ai","message":"review the quality of my recommendations"}'

# Feedback loop: what the team approved or dismissed, and what the agent learns from it
curl "localhost:5301/v1/feedback?domain=thrivestack.ai" $H

# Debug helpers
curl "localhost:5301/gaps?domain=thrivestack.ai" $H       # gaps found, no model used
```

## Guardrails

The agent only does one job: owned and earned content recommendations. Anything else is refused.

| Layer | Where | What it stops |
|---|---|---|
| Request limits | `api/v1.py` | questions over 500 chars, `domain` that is not a hostname, oversized `brand_context` |
| Screen | `graphs/ask/graph.py` (first node, and again inside `answer`) and the top of `/v1/chat` | "ignore your instructions", role changes, code/poem/joke/trivia requests (refused before the model, no data fetched) |
| On-topic | `guard.on_topic` | questions with no recommendation vocabulary and no word from the brand's own titles |
| Hardened prompt | `prompts.json` `ask_prioritize` | the model answers `OUT_OF_SCOPE` to anything else; the question is delimited as data |
| Answer check | `guard.check_answer` | code blocks, the sentinel, or an answer that names no recommendation |
| Model limits | `llm/ollama.py` | one model call at a time (429 + Retry-After when busy), prompt size cap |

Refused: `/v1/ask` returns 422, `/v1/chat` returns 200 with `intent: "refused"`. Logs say why (`guard refused ... reason=...`) and never include the message. Patterns and refusal text live in `prompts.json` under `guard`.
`tests/test_guardrails.py` includes tests that fail if any new module calls the model without being listed and reviewed.

## Layout
- `src/agent/gaps/` finds owned and earned gaps (no model). Port of `ai-visibility-api/internal/contentrecs`.
- `src/agent/prompts/prompts.json` is the single file holding every prompt.
- `src/agent/validators/` checks model output (allowed values, no invented competitors or sites).
- `src/agent/graphs/generate/` and `graphs/ask/` are the two LangGraph workflows.
- `src/agent/api/` has the REST routes. By default the agent only returns recommendations (a preview).
- `src/agent/graphs/judge/` + `src/agent/judge/` are the **Judge agent**: deterministic checks (numbers vs evidence,
  invented sites, near-duplicates, filler, repeats of what the team dismissed), an optional model reviewer, and one
  rewrite pass. Generate runs it by default (`judge: "rules"`): rows it rejects are returned under `held_back` and
  never saved; every other row carries a `quality` review. Settings live in `prompts.json` under `judge`.
- `src/agent/feedback.py` is the **feedback loop**. There is no separate store: it reads which recommendations the team
  moved to to-do/approved (accepted) or dismissed, through the MCP with the caller's token, and uses that to steer the
  writers (what to avoid, what they act on) and the Judge. It only counts patterns after 5 decisions. It cannot learn
  *why* something was dismissed, because the API stores no reason. With no decisions yet it does nothing.
- `src/agent/saving.py` saves them on request: it calls the ThriveStack MCP tool `save_content_recommendations`,
  which calls `POST /ai-visibility/content-recommendations/import` in ai-visibility-api with the caller's own token.
  The agent has no database. The API re-checks every citation-backed row against the probe run and takes the
  evidence from its own analysis, so only the wording is trusted. Needs the API and MCP changes deployed first.
