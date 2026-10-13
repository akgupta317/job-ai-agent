# Tasks: Generate recommendations with the agent, and save them through an API + MCP tool

**Theme:** saving is a capability of the platform, not of one screen.

```
User / any client            Agent                    thrivestack-mcp                  ai-visibility-api
-----------------            -----                    ---------------                  -----------------
"generate 2 earned
 recommendations and save"
        │──────────────►  1. generate (reads data via MCP tools, writes with the model)
                          2. call tool ───────────►  save_content_recommendations ───►  POST .../content-recommendations/import
                                                                                         validate + upsert
                          3. reply: saved / updated / rejected  ◄────────────────────────────────────┘
```

Why this shape:
- **One write path.** The API owns validation and the table. The MCP tool is a thin wrapper. The agent just calls the tool, so it needs no database and no new credentials.
- **Same auth everywhere.** The agent already forwards the caller's token to the MCP, and the MCP forwards it to the API, so each user can only save to their own brands.
- **Reusable.** Any MCP client can use the tool. A UI screen later is a thin client that calls the agent.

The UI work that saved from the browser has been **reverted** (nothing was committed or pushed).

## Status (built locally on branches; not yet pushed, merged or deployed)

- **Done:** A1-A4, A7 (ai-visibility-api branch `feature/import-content-recommendations`), B1, B2, B5 (thrivestack-mcp branch `feature/save-content-recommendations`), C1-C4, C7 (this repo, branch `feature/save-via-mcp`).
- **A4 decided:** `priority_reason` is produced by the server from the evidence (same text generate writes), never accepted from the caller.
- **B3 partly done:** the MCP `README.md` is updated. The "read-only" wording on the UI's MCP setup page (`McpSetupPage.tsx`) and the public MCP docs still needs changing.
- **Verified:** unit tests in all three repos, plus a cross-check with real `thrivestack.ai` probe data: the Python agent built 61 rows and the Go API accepted all 61 with identical evidence.
- **Not verified:** a real call through a deployed MCP and API (nothing is deployed yet), and the Postgres write itself (the data layer's existing `Upsert` is reused unchanged).
- **Still open:** A5, A6, B4, C5, C6, D1, D2.

### Quality judge and feedback loop (branch `feature/quality-judge-feedback`, stacked on `feature/save-via-mcp`)
- [x] **Judge agent** (`POST /v1/judge`, plus a step inside generate): deterministic checks, optional model reviewer, one rewrite pass. Tested on all 175 saved `thrivestack.ai` recommendations: 126 pass, 40 revise, 9 reject (the rejects are all genuine duplicates).
- [x] **Feedback loop** (`GET /v1/feedback`, guidance in the writer prompts, Judge check against dismissed items). Reads accepted/dismissed from the existing statuses; needs no new storage.
- [ ] **Needs real decisions to do anything:** today 1 of 175 saved recommendations is decided (1 to-do, 0 approved, 0 dismissed), so the loop has almost nothing to learn from yet.
- [ ] **Dismiss reason:** the API stores a status only. Capturing *why* needs a column in ai-visibility-api, an endpoint, and an MCP tool, then the UI to ask for it.
- [ ] **Show quality in the UI:** the chat page ignores `quality` and `held_back` for now; cards should show the verdict and notes.
- [ ] **Evaluation set:** a fixed set of real rows with expected verdicts, run in CI, to tune thresholds and compare Ollama with Claude as the reviewer. The local 8B reviewer was lenient in a real run (it gave 5/5/5/5 to a row whose text contradicted its own evidence), so treat its scores as a second opinion; the rules are the safety net.
- [ ] **Existing data:** the saved list already contains 9 duplicate titles and 45 rationales that quote numbers different from their card (written by the Go pipeline). Fixing those is a separate cleanup.

## Decisions to make first

1. **The MCP is documented as read-only.** `thrivestack-mcp/README.md` line 52 says "All currently registered tools are read-only", and the MCP setup page in the UI promises "read-only access" to your data. A save tool is the first write tool. Anyone who connects Claude, ChatGPT or Cursor could then ask it to save. Decide: (a) accept, update the docs and the setup page wording, and keep the write narrow (see B1), or (b) keep the MCP read-only and let the agent call the API directly with a service credential.
2. **The agent has no memory.** "Save these" in a later message only works if the client sends the rows back, or if the agent keeps the last run. Simplest: a `save` option on the generate call ("generate and save"), plus a save endpoint that accepts rows.
3. **Automatic generation after a probe** has no user token, so it can't use the caller-token path. That case needs a service credential (not planned here).

---

## A. API: `ai-visibility-api` (Go)

- [x] **A1 [MUST] `POST /ai-visibility/content-recommendations/import`** (bulk, up to about 30 rows).
  - Body: `{domain, probe_run_id, recommendations: [...]}`, with the row shape the agent already returns.
  - The server takes product id, environment id and domain from the request context. It forces `status = suggested`. It **ignores** client-supplied `id`, `status`, `assignees`, `created_at`, `run_id`.
  - Saves through the existing `RecsDB.Upsert` (unique on product, domain, prompt_id), so repeating a save updates and never duplicates.
  - Response: `{saved, updated, rejected: [{prompt_id, reason}]}`.
  - The existing gateway rule for `/ai-visibility/content-recommendations` already covers this path.
- [x] **A2 [MUST] Validate every row on the server.** Allowed values: channel `Blog` for owned; owned roles `CMO | SEO Manager | CMO & SEO Manager`; earned roles `PR | Community/Social | Content`; the content type lists; priority `HIGH | MED | LOW`; `channel_type` in `onsite | earned`; maximum lengths. The lists are in `src/agent/prompts/prompts.json` under `enums`.
- [x] **A3 [MUST] Cross-check citation-backed rows against the probe data** (prompt exists in the run, tier and gap score match, run belongs to this product). The caller can now be any MCP client, so the server must not trust claimed evidence. Rows with `evidence_type = category_inferred` skip this.
- [x] **A4 [MUST] Decide `priority_reason`.** The API stores it (latest commit on the checked-out branch) and the agent does not produce it. Either compute it from tier and score, or leave it empty for these rows.
- [ ] **A5 [SHOULD] Mark who wrote the row** (for example `generated_by = agent`), so it can be told apart later. A new column needs a migration after `057`.
- [ ] **A6 [SHOULD] Limits:** entitlement and credit rules (`internal/entitlement`), and a cap on rows per call and calls per minute.
- [x] **A7 [MUST] Tests:** valid, partial, duplicate, tampered and cross-brand rows.

## B. MCP: `thrivestack-mcp` (Go, `mcp-go`)

- [x] **B1 [MUST] New tool `save_content_recommendations`** in `internal/tools/ai_visibility.go`, next to the existing recommendation tools.
  - Arguments: `domain` (required), `probe_run_id`, `recommendations` (array of objects).
  - Calls A1 with the caller's own auth, like the other tools.
  - Keep the write **narrow**: it can only create or refresh `suggested` rows. It cannot approve, dismiss, assign or delete.
  - Annotations: not read-only, not destructive, idempotent. (Today only read-only hints exist in the codebase.)
  - Description tells the model to show the user the recommendations and get a yes before saving.
- [x] **B2 [MUST] Brand check:** confirm the domain belongs to the caller (`brand_scope.go`) before calling the API.
- [~] **B3 [MUST] Update the "read-only" wording** (decision 1): `README.md` line 52, the MCP docs, and `McpSetupPage.tsx` in the UI.
- [ ] **B4 [SHOULD] Decide whether the tool needs a separate permission** (for example a write scope on tokens) so read-only connections stay read-only.
- [x] **B5 [MUST] Tests,** in the style of `ai_visibility_*_test.go`.

## C. Agent: `ai-visibility-agent` (Python)

- [x] **C1 [MUST] `save_recommendations(token, domain, run_id, rows)`** in `tools/thrivestack_mcp.py`, calling the B1 tool. The agent already forwards the caller's token.
- [x] **C2 [MUST] `POST /v1/recommendations/save`:** accepts rows from an earlier generate, re-checks their shape, and calls C1. Returns `{saved, updated, rejected}`.
- [x] **C3 [MUST] `save: true` on `POST /v1/recommendations/generate`** ("generate and save" in one request). Default stays `false`, so today's preview behaviour is unchanged.
- [x] **C4 [SHOULD] Chat:** teach `/v1/chat` to recognise "generate … and save", and make "save these" work (decision 2).
- [ ] **C5 [MUST] Run endpoints (start and poll).** A CPU run takes minutes and a connection can be cut near 4 minutes. `POST /v1/runs` returns `202`, `GET /v1/runs/{id}` returns the result. Prod runs 2 replicas, so run state needs to be shared (or use 1 replica to begin with).
- [ ] **C6 [SHOULD]** One run at a time with a clear "busy" answer; `401` for a rejected token (today a wrong token gives `502`).
- [x] **C7 [MUST] Tests:** save success, partial rejection, API down, token rejected.

## D. Platform

- [ ] **D1 [MUST before wide use] Protect the public route:** rate limit per user or IP. Capacity is one CPU model run at a time.
- [ ] **D2 [SHOULD]** Prod capacity: the node Ollama sits on is at 99% of its requested CPU. Decide on replicas, requests, or the GPU pool.
- [ ] **D3 [LATER] A thin UI screen** (route, button, preview) that only calls the agent. No save logic in the browser.

## E. Acceptance test (dev first)

1. `POST /v1/recommendations/generate` with `{"kinds":["earned"],"max_per_side":1,"save":true}` and a valid token.
2. The reply shows 1 recommendation and `saved: 1`.
3. `get_earned_content_recommendations` (MCP) and the existing recommendations screen show it, status Suggested, with the right priority and channel.
4. Run step 1 again: the saved item is skipped as unchanged, or updated, and no duplicate appears.
5. Call the save endpoint with a made-up `prompt_id` or a changed gap score: rejected with a reason.
6. Use a token for another brand: rejected.
7. Use the tool from Claude or another MCP client: it asks for confirmation, then saves.

## Order of work
1. **A1, A2, A4** (one agreed row shape: the agent's current output plus `probe_run_id`).
2. **B1, B2** against dev, then **C1–C3**.
3. **A3, A5–A7, B3–B5**, then **C5**, and **D1** before wider use.
