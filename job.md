# JOB: Create & Manage the AI Visibility Application

This file is the single job spec for building, deploying and maintaining the **AI Visibility** application across all of its repositories. An agent (or human) reading this file should be able to create a new component, wire its dependencies, and ship it to dev/prod without further context.

> Use this file as the source of truth. When a repo, path, port, bucket or pipeline variable changes, update it here in the same change.

---

## 1. Repositories & Locations

| Role | Local path | Azure DevOps repo | Notes |
|---|---|---|---|
| Orchestration / job spec (this repo) | `/Users/admin/Thrive/ai-visibility-agent` | `ai-visibility-agent` | Holds `job.md` only (README is a stub) |
| Backend API (Go) | `/Users/admin/Thrive/ai-visibility-api` | `ai-visibility-api` | Module `ai_visibility_api`, entry `main.go`, `cmd/dev` for local run |
| UI (React MFE) | `/Users/admin/Thrive/UI/crmv2UI/plg-crm-ui` | `plg-crm-ui` | AI Visibility screens in `src/components/AiVisibility`, routes in `src/routing`, nav in `src/components/Sidebar` |
| CI/CD pipeline templates | `/Users/admin/Thrive/template-pipelines` | `template-pipelines` | Backend: `ci/backend.yaml`; also `ci/main.yaml`, `ci/init.yaml`, `ci/frontend.yaml`, `prod-backend-promotion/` |
| K8s config & deploy manifests | `/Users/admin/Thrive/k8s-deployment-manifests` | `k8s-deployment-manifests` | Branch **`azure-aks`**. API lives at `workflow/backend/ai-visibility-api/` |
| Shared Go libs | Go module `dev.azure.com/SaasTraction/SaasTraction/_git/backend-common.git/*` | `backend-common` | postgres, secretstores, tracing, zerolog, telemetryapis |

Reference services to copy patterns from: `plg-crm-dashboard`, `public-api-events`, `ops-mgmt-api` (see `k8s-deployment-manifests/plg-crm/backend/*` and `workflow/backend/*`).

---

## 2. Architecture & Dependency Map

```
 plg-crm-ui (React MFE)
     │  REST  /ai-visibility/...
     ▼
 Istio Gateway ── VirtualService (ai-visibility-api, prefix routes) ──► ai-visibility-api (Go)
                                                                          │ :5201 REST external
                                                                          │ :5202 REST internal / health
                                                                          │ :5200 gRPC
        ┌─────────────────────┬───────────────────┬──────────────────────┤
        ▼                     ▼                   ▼                      ▼
  Postgres (RDS)        ClickHouse         S3 (reports)          Anthropic API
  schema Ai_Visibility  ai_visibility DB   prefix ai-visibility/ (LLM calls)
                        (+ clickhouse_ddl/)
        ▼
  AWS Secrets Manager (IRSA via aws-secrets-reader SA + AKSWorkloadIdentityRole)
```

### Runtime dependencies (backend)
- **Postgres** – shared `postgresdb`, schema `"Ai_Visibility"`; migrations in `ai-visibility-api/migrations/NNN_*.sql` (apply in numeric order; note duplicate prefixes like `013_`, `016_`, `019_`, `021_`, `026_` exist — apply all, order by full filename).
- **ClickHouse** – DDL in `clickhouse_ddl/`; analytics/probe data.
- **S3** – report HTML; dev bucket `thrivestack-temp-static-assets`, prod bucket `thrivestack-prod-product-static-assests-krlm`, both with prefix `ai-visibility/` (`s3Prefix` config key).
- **AWS Secrets Manager** – dev `thrivestack-dev-builder-secrets-manager`, prod `thrivestack-prod-builder-secrets-manager`; DB creds `thrivestack-dev-rds-credentials`.
- **Anthropic API** – key via config/Secrets Manager (`anthropicApiKey`). Models and per-audit cost ceiling are in `config/config.yml` (`saas.audit.costCeilingUsd`).
- **Go modules** – private `dev.azure.com` modules (`GOPRIVATE=dev.azure.com`, needs `DEVOPS_TOKEN` at build). Refresh with `make get-latest-common`.

### Build-time dependencies
- Backend image: `golang:1.21-alpine` builder → `alpine:3.17.1`, binary `bin/main`, `CMD ./main config config/config.yml`.
- UI: Node LTS, `npm install --legacy-peer-deps` with secure `.npmrc`, Webpack Module Federation (`PlgCRM` remote), config injected via `CONFIG_FILE` build-arg (`config/th-dev-config.yml`, `config/th-prod-config.yml`).

---

## 3. Contracts (do not change one side alone)

| Contract | Owner | Consumers | Where defined |
|---|---|---|---|
| REST routes `/ai-visibility/...` (e.g. `POST /ai-visibility/audits`, `GET /ai-visibility/audits/{id}`) | API `app/workflow/rest_endpoints.go` | UI, Istio VirtualService | VirtualService prefix in `k8s-deployment-manifests/workflow/backend/ai-visibility-api/base/virtual-service.yaml` |
| Ports 5200 / 5201 / 5202 | API `config/config.yml` | Dockerfile `EXPOSE`, k8s Service, probes | `base/service.yaml`, `base/deployment.yaml` |
| Config schema `saas.*` | API `config/config.yml` | k8s overlay `config-map.yaml` (dev/prod) | Keep overlay config in sync with `config/config.yml` |
| DB schema | API `migrations/` | API runtime | Never edit applied migrations; add new numbered file |
| Image name | pipeline: `$(Build.Repository.Name)` | k8s overlay `deployment.yaml` | `thrivestack.azurecr.io/thrivestack:ai-visibility-api-{build_number}` |

If you add a REST route: update API handler → VirtualService prefix (if new prefix) → UI service/API client → this file.

---

## 4. Job: Create a New Component (API / service)

Follow in order. Mark each step done in the checklist (Section 9).

1. **Code** (`ai-visibility-api`)
   - Add package under `internal/<name>`, wire in `app/options/*` and `app/workflow/*`.
   - Use `server.SaaSResponseWriter` handler signatures.
   - Add migration `migrations/NNN_<desc>.sql` (next free number) if schema changes; add ClickHouse DDL under `clickhouse_ddl/` if needed.
   - `make test`, `make go-lint`, `make build`.
   - Local run without Postgres/AWS: `make dev` (port 9090).
2. **Config**
   - Add keys to `config/config.yml` **and** both overlay ConfigMaps (dev/prod). No secrets in git — use Secrets Manager.
3. **Container** – `Dockerfile` must expose 5201/5202 and copy `config/`.
4. **K8s manifests** (`k8s-deployment-manifests`, branch `azure-aks`)
   - Path: `<SUB_SYSTEM>/<APP_TYPE>/<image_name>/` = `workflow/backend/ai-visibility-api/`
   - `base/`: kustomization, deployment, service, configMap, serviceaccount, role, role-binding, virtual-service, scaledobject.
   - `overlays/thrivestack-dev` and `overlays/thrivestack-prod`: kustomization, deployment (IRSA env + config volume), config-map, scaledobject.
   - Pipeline rewrites `image:` in the overlay `deployment.yaml` with `sed`, so the overlay must contain exactly the image line it updates.
5. **Pipeline** (`template-pipelines`) – no YAML change needed for a standard backend. Repo's `azure-pipelines.yml` extends `ci/main.yaml@template-pipelines`; `prod-pipelines.yml` extends `prod-backend-promotion/main.yaml@template-pipelines`.
6. **Azure DevOps pipeline variables** (set in the UI, not YAML) – without them `isBE` is false and build/deploy stages are skipped:
   - `APP_TYPE = backend`
   - `SUB_SYSTEM = workflow`
   - Variable group `ORG_TOKEN` must be linked (`DEVOPS_TOKEN`, `GITHUB_TOKEN`, `Notification_Webhook_URL`).
7. **UI** (`plg-crm-ui`) – see Section 5.
8. **Check in & release** – Section 6.

---

## 5. Job: UI Integration (`plg-crm-ui`)

1. Add/modify components in `src/components/AiVisibility/`.
2. Register routes in `src/routing/routes.tsx` (lazy-loaded with `<Suspense>`); add nav entry in `src/components/Sidebar`.
3. API base URL / endpoints come from the per-env config (`config/th-dev-config.yml`, `config/th-prod-config.yml`) — do not hardcode hosts.
4. Verify: `npm run lint`, `npm run build`, and Playwright E2E where applicable (`npx playwright test`).
5. Pipeline: `ci/frontend.yaml` builds on `develop` using `th-dev-config.yml`, syncs `dist` to the S3 app bucket and invalidates CloudFront. Prod flow is `ci/main/prod-frontend.yaml` on `main`.
6. Document UI/API handoffs in the UI repo (existing pattern: `*_HANDOFF.md`).

---

## 6. Release Flow (Branch → Environment)

| Branch | Pipeline | Result |
|---|---|---|
| `feature/*` | `ci/feature/feature-backend.yaml` | Feature build only |
| `develop` | `ci/main.yaml` → `ci/backend.yaml` | Build & push image → update dev overlay in `k8s-deployment-manifests@azure-aks` → `kubectl apply -k` on **dev AKS** (`tsdev01-rg-aks`) |
| `main` | `prod-pipelines.yml` → `prod-backend-promotion` + `Promote_Backend_Image_to_Prod_Envirnoment` stage | **Manual approval** (environment `aks-promote-backend-images-to-prod-env`) → update prod overlay → apply on **prod AKS** (`prod-aks-cluster`) |

Order of check-in for a cross-repo change:
1. `k8s-deployment-manifests` → push to `azure-aks` (manifests must exist before the pipeline looks for the overlay path, otherwise Deploy fails with `ls` error).
2. `ai-visibility-api` → `develop` → PR to `main`.
3. `plg-crm-ui` → `develop` → PR to `main` (after API routes are live in dev).

Never push directly to `main`. Do not commit or push without explicit user approval.

---

## 7. Environments

| | Dev | Prod |
|---|---|---|
| Overlay | `overlays/thrivestack-dev` | `overlays/thrivestack-prod` |
| AWS IRSA role | `AKSWorkloadIdentityRole` (dev account) | `AKSWorkloadIdentityRole` (prod account) |
| Replicas (KEDA) | min 1 / max 5 | min 2 / max 10 |
| `env` in config | `DEV` | `PROD` |
| Secrets Manager | `thrivestack-dev-builder-secrets-manager` | `thrivestack-prod-builder-secrets-manager` |
| S3 reports | `thrivestack-temp-static-assets` + `ai-visibility/` | `thrivestack-prod-product-static-assests-krlm` + `ai-visibility/` |

Shared service account for AWS access: `aws-secrets-reader`.

---

## 8. Verification (Definition of Done)

Backend:
- [ ] `make test` and `make go-lint` pass; `make build` succeeds.
- [ ] Migrations applied to dev schema `"Ai_Visibility"`.
- [ ] Pipeline `Build_and_Push_Backend` + `Deploy` green; image in ACR as `thrivestack.azurecr.io/thrivestack:ai-visibility-api-<build_number>`.
- [ ] `kubectl get pods -l app=ai-visibility-api` is Running.
- [ ] `GET :5202/health` returns `ok`.
- [ ] `POST /ai-visibility/audits` → poll `GET /ai-visibility/audits/{id}` until `completed`; report URL renders.

UI:
- [ ] `npm run lint` and `npm run build` pass.
- [ ] Feature works against dev API through the gateway.

Manifests:
- [ ] `kustomize build overlays/thrivestack-dev` and `...-prod` render; kyverno policy check passes (pipeline runs it).

---

## 9. Task Checklist (status snapshot — update as work progresses)

Source of detail: `/Users/admin/Thrive/ai-visibility-api/deployement.md`.

- [x] Config `config/config.yml`
- [x] Dockerfile (pure Go renderer, no Node)
- [x] Production wiring code fixes (renderer, S3 uploader, secret store, handlers)
- [x] K8s base manifests
- [x] K8s dev overlay
- [x] K8s prod overlay
- [x] Anthropic key configured
- [x] S3 prefix `ai-visibility/` support
- [x] Azure DevOps variables `APP_TYPE`, `SUB_SYSTEM`
- [x] Postgres schema + initial migration (dev)
- [x] Manifests pushed to `azure-aks`
- [ ] **Check in `ai-visibility-api` (develop → PR → main)**
- [ ] **First deployment smoke test (dev)**
- [ ] Apply later migrations (`002`+) and ClickHouse DDL to dev, then prod
- [ ] Prod promotion & approval

---

## 10. Rules for the Agent

1. **Read before write**: inspect a reference service (`plg-crm-dashboard`, `public-api-events`) before inventing a pattern.
2. **Never commit secrets.** `config/config-local.yml`, `claude-cookies.json` and API keys must stay out of git. Flagged: the Anthropic key is currently inline in the k8s ConfigMaps — prefer moving it to Secrets Manager (`anthropicApiKey`) and clearing the ConfigMap value.
3. **Keep config in sync** between `config/config.yml` and both overlay ConfigMaps.
4. **Migrations are append-only.**
5. **Change one contract, update all consumers** (Section 3) in the same job.
6. **No git commit/push/PR, no kubectl apply, no pipeline trigger without explicit approval.** Prod actions always require user confirmation.
7. After finishing, update Section 9 and any changed paths/ports/buckets in this file.
