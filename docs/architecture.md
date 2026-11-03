# Architecture — Retail Data Analysis Chat Assistant

This document describes the production High-Level Design (HLD) on Google Cloud, the internal agent graph, the main runtime flows, and where data lives. The reasoning behind each technology choice is in [technical_design.md](technical_design.md).

**Contents**
1. [Production HLD](#1-production-hld)
2. [Component responsibilities](#2-component-responsibilities)
3. [Agent graph (LangGraph)](#3-agent-graph-langgraph)
4. [Flow A — analysis question with SQL self-correction](#4-flow-a--analysis-question-with-sql-self-correction)
5. [Flow B — destructive operation with confirmation](#5-flow-b--destructive-operation-with-confirmation)
6. [Flow C — learning loop & Golden Bucket ingestion](#6-flow-c--learning-loop--golden-bucket-ingestion)
7. [Flow D — persona update without redeploy](#7-flow-d--persona-update-without-redeploy)
8. [Data storage & handling](#8-data-storage--handling)
9. [Security boundaries](#9-security-boundaries)
10. [Prototype vs. production](#10-prototype-vs-production)

---

## 1. Production HLD

```mermaid
flowchart TB
    %% ---------- Clients ----------
    subgraph CLIENTS["Clients"]
        CLI["CLI (prototype)"]
        WEB["Web chat / Slack (future)"]
        ADMIN["Admin console<br/>(persona, golden-trio review)"]
    end

    %% ---------- Edge ----------
    subgraph EDGE["Edge & Identity"]
        LB["External HTTPS Load Balancer<br/>+ Cloud Armor (WAF, rate limit)"]
        IAP["Identity-Aware Proxy<br/>(Google Workspace / Identity Platform SSO)"]
    end

    %% ---------- Compute ----------
    subgraph COMPUTE["Compute — Cloud Run (serverless, autoscaling)"]
        API["agent-api<br/>FastAPI + LangGraph<br/>(SSE streaming)"]
        ADMINAPI["admin-api<br/>prompt registry & trio review"]
        WORKER["learning-worker<br/>Cloud Run Job / Pub/Sub push"]
        INDEXER["golden-indexer<br/>Pub/Sub push"]
        EVAL["eval-runner<br/>Cloud Run Job (CI + nightly)"]
    end

    %% ---------- AI services ----------
    subgraph AI["Vertex AI"]
        GEM["Gemini Flash (primary)<br/>Gemini Flash-Lite (fallback)"]
        EMB["Gemini Embeddings"]
        ARMOR["Model Armor<br/>(prompt-injection / jailbreak screening)"]
        DLP["Sensitive Data Protection (DLP)<br/>output PII inspection"]
    end

    %% ---------- Data ----------
    subgraph DATA["Data stores"]
        BQ[("BigQuery<br/>thelook_ecommerce<br/>(read-only, policy tags on PII)")]
        PG[("Cloud SQL — PostgreSQL + pgvector<br/>checkpoints · saved_reports · audit_log<br/>user_profiles · entitlements<br/>prompt_versions · golden_trios index")]
        GCS[("Cloud Storage — Golden Bucket<br/>trios (source of truth, versioned)")]
        REDIS[("Memorystore (Redis)<br/>cache · rate limits · circuit-breaker state")]
        SM["Secret Manager"]
    end

    %% ---------- Async ----------
    subgraph ASYNC["Async messaging"]
        PS_EV{{"Pub/Sub: interaction-events"}}
        PS_GB{{"Pub/Sub: golden-bucket-changes"}}
        SCHED["Cloud Scheduler"]
    end

    %% ---------- Observability ----------
    subgraph OBS["Observability"]
        LOG["Cloud Logging<br/>(structured JSON)"]
        TRACE["Cloud Trace<br/>(OpenTelemetry)"]
        MON["Cloud Monitoring<br/>dashboards + alerts"]
        BQLOG[("BigQuery: agent_logs<br/>(log sink, analytics)")]
        LF["Langfuse (self-hosted on Cloud Run)<br/>LLM traces, prompt versions, scores"]
    end

    CLI & WEB --> LB --> IAP --> API
    ADMIN --> LB
    IAP --> ADMINAPI

    API <--> GEM
    API --> EMB
    API --> ARMOR
    API --> DLP
    API -- "validated SELECT<br/>(dry-run + bytes cap)" --> BQ
    API <--> PG
    API <--> REDIS
    API -. secrets .-> SM
    API -- "turn events" --> PS_EV

    ADMINAPI <--> PG
    ADMINAPI --> GCS

    PS_EV --> WORKER
    SCHED --> WORKER
    SCHED --> EVAL
    WORKER --> PG
    WORKER -- "approved trio candidates" --> GCS
    GCS -- "object finalize" --> PS_GB --> INDEXER
    INDEXER --> EMB
    INDEXER -- "dry-run SQL validation" --> BQ
    INDEXER --> PG
    EVAL --> API
    EVAL --> LF

    API & WORKER & INDEXER -. logs/traces .-> LOG & TRACE
    LOG --> BQLOG
    LOG & TRACE --> MON
    API -. LLM spans .-> LF
```

**Reading the diagram**
- **Synchronous path** (user waiting): Client → Load Balancer/IAP → `agent-api` → Gemini / BigQuery / Postgres → streamed answer.
- **Asynchronous path** (nobody waiting): turn events → Pub/Sub → `learning-worker` (preferences, trio candidates); Golden Bucket changes → `golden-indexer` (embed + index).
- **Control plane**: Admin console → `admin-api` → prompt registry & trio review (non-developers, no redeploy).
- **Observability** is a side channel from every service; it never blocks the user path.

---

## 2. Component responsibilities

| Component | Service | Responsibility |
|-----------|---------|----------------|
| Edge | HTTPS LB + **Cloud Armor** | TLS, WAF rules, per-IP rate limiting, DDoS protection. |
| Identity | **IAP** (+ Workspace / Identity Platform) | SSO; injects a signed JWT with the user's identity. The agent never trusts a user ID from the request body. |
| `agent-api` | **Cloud Run** (Python, FastAPI, LangGraph) | Runs the agent graph per turn; stateless (state in Postgres checkpointer), scales to zero, streams via SSE. |
| LLM | **Vertex AI Gemini** Flash / Flash-Lite | Planning, SQL generation, synthesis, reports. Flash-Lite is the fallback and handles cheap classification steps. |
| Input screening | **Model Armor** + in-graph classifier | Detects prompt injection/jailbreaks and off-topic (non-analysis) requests. |
| SQL Guard | In-process library (`sqlglot`) | Deterministic validation and rewriting of every SQL statement before it reaches BigQuery (see §9). |
| Warehouse | **BigQuery** | Read-only analytics source. PII columns protected by **policy tags** (column-level security) so the agent's service account *cannot* read them. |
| Operational DB | **Cloud SQL for PostgreSQL** + **pgvector** | LangGraph checkpoints (conversation state), saved reports, audit log, user profiles/preferences, entitlements, prompt versions, golden-trio vector index. One managed, transactional store keeps the footprint small. |
| Golden Bucket | **Cloud Storage** (object versioning) | Source of truth for expert trios (`question.md`, `query.sql`, `report.md`, `metadata.json`). |
| Cache / limits | **Memorystore (Redis)** | Schema & query-result cache, per-user rate limits, shared circuit-breaker state across instances. |
| Messaging | **Pub/Sub** | Decouples the chat path from learning/indexing; retries + dead-letter topics. |
| Workers | **Cloud Run Jobs / services** | `learning-worker`, `golden-indexer`, `eval-runner`. |
| Output PII check | **Sensitive Data Protection (DLP)** | Last-line inspection of the final answer (emails, phones, names, addresses). |
| Observability | **Cloud Logging / Trace / Monitoring**, **Langfuse** | Logs, distributed traces, metrics, alerts; LLM-level traces and scoring. |
| Secrets | **Secret Manager** | API keys, DB credentials. |
| CI/CD | **Cloud Build** + **Artifact Registry** | Build, test, run eval gate, deploy new Cloud Run revision. |

---

## 3. Agent graph (LangGraph)

Each user turn executes this state graph inside `agent-api`. Nodes in **bold** are deterministic code (not LLM-controlled).

```mermaid
flowchart TD
    START([User message]) --> LOAD["<b>load_context</b><br/>identity · entitlements · persona<br/>user prefs · schema · golden trios (RAG)"]
    LOAD --> IG{"<b>input_guard</b><br/>in scope? injection? PII request?"}
    IG -- "blocked" --> REFUSE["<b>refuse</b><br/>polite, scoped message"]
    IG -- "ok" --> AGENT["agent (Gemini)<br/>plan · choose tools · synthesize"]

    AGENT -- "tool calls" --> TOOLS["<b>tools</b><br/>describe_schema · run_sql<br/>search_golden_trios · save_report<br/>list_reports · propose_delete"]
    TOOLS -- "observation<br/>(rows / structured error)" --> AGENT

    TOOLS -- "propose_delete returned<br/>pending action" --> CONFIRM["<b>confirm_delete</b><br/>interrupt() → show exact list<br/>wait for user decision"]
    CONFIRM -- "confirmed (token matches)" --> EXEC["<b>execute_delete</b><br/>owner check · soft delete · audit"]
    CONFIRM -- "cancelled / other input" --> AGENT
    EXEC --> AGENT

    AGENT -- "budget exceeded /<br/>max SQL retries" --> GIVEUP["<b>graceful_fallback</b><br/>explain what failed, suggest next step"]
    AGENT -- "final answer" --> OG["<b>output_guard</b><br/>PII scrub · format check"]
    GIVEUP --> OG
    REFUSE --> OUT
    OG --> OUT([Stream answer to user])
    OUT --> POST["<b>post_turn</b> (async)<br/>emit event · update metrics"]
```

Key design points:
- **The LLM can never execute a delete.** It can only *propose* one; execution happens in a deterministic node after a human confirmation bound to the exact list of report IDs.
- **Every SQL statement passes through the SQL Guard inside `run_sql`**, regardless of what the LLM wrote.
- **Budgets are part of the graph state**: `sql_attempts`, `llm_calls`, `tokens_used`. Exceeding them routes to `graceful_fallback` instead of looping.
- New capabilities (charts, email, web search) are **added as tools** in the registry; the graph does not change.

---

## 4. Flow A — analysis question with SQL self-correction

```mermaid
sequenceDiagram
    autonumber
    actor U as Executive
    participant API as agent-api (LangGraph)
    participant PG as Postgres
    participant LLM as Gemini
    participant G as SQL Guard
    participant BQ as BigQuery

    U->>API: "Why are users in CA underspending vs. NY?"
    API->>PG: load checkpoint, entitlements, prefs, active persona
    API->>PG: vector search golden trios (top-k, within user scope)
    API->>LLM: input_guard classification (Flash-Lite)
    LLM-->>API: in_scope = true
    API->>LLM: system prompt + persona + prefs + schema + trios + history
    LLM-->>API: tool_call run_sql(sql_v1)
    API->>G: validate(sql_v1, user_scope)
    G-->>API: rejected — column `email` is PII
    API->>LLM: observation: structured error + hint
    LLM-->>API: tool_call run_sql(sql_v2)
    API->>G: validate(sql_v2)
    G-->>API: ok → rewritten SQL (scope filter + LIMIT)
    API->>BQ: dry run (bytes estimate ≤ cap?)
    API->>BQ: execute (timeout, maximum_bytes_billed)
    BQ-->>API: error: Unrecognized name `order_date`
    API->>LLM: observation: BQ error + relevant schema excerpt (attempt 2/3)
    LLM-->>API: tool_call run_sql(sql_v3)
    API->>G: validate(sql_v3) → ok
    API->>BQ: execute
    BQ-->>API: rows (truncated to N for the LLM)
    API->>LLM: observation: result summary
    LLM-->>API: final analysis
    API->>API: output_guard (PII scrub)
    API-->>U: streamed answer (table / bullets per user preference)
    API--)PG: save checkpoint
    API--)API: emit turn event → Pub/Sub
```

Notes:
- Guard rejections, BigQuery errors and **empty results** are all returned to the LLM as structured observations (`{"error_type": ..., "message": ..., "hint": ...}`), never as exceptions to the user.
- For empty results, the hint includes cheap diagnostics (e.g. the real date range of the table or distinct values of the filtered column) so the model can tell "no data" apart from "wrong filter".
- After the attempt cap (default 3 SQL attempts per question), the graph routes to `graceful_fallback`.

---

## 5. Flow B — destructive operation with confirmation

```mermaid
sequenceDiagram
    autonumber
    actor U as Executive
    participant API as agent-api (LangGraph)
    participant LLM as Gemini
    participant RS as saved_reports (Postgres)
    participant AU as audit_log

    U->>API: "Delete all reports mentioning Client X"
    API->>LLM: messages + tools
    LLM-->>API: tool_call propose_delete(filter = {text: "Client X"})
    API->>RS: SELECT ... WHERE owner_id = :me AND deleted_at IS NULL AND matches filter
    RS-->>API: 3 reports (ids 12, 17, 31)
    API->>API: create pending_action {ids, sha256(ids), expires_at = now + 5 min}
    API-->>U: interrupt: table of 3 reports + "Type 'confirm' to delete these 3 reports"
    Note over U,API: Graph is paused and checkpointed. The user may also ask something else.

    alt User confirms
        U->>API: "confirm"
        API->>API: verify token, TTL, ids unchanged
        API->>RS: re-check ownership; UPDATE SET deleted_at = now() WHERE id IN (12, 17, 31)
        API->>AU: insert {user, action: delete, ids, conversation_id, trace_id}
        API-->>U: "Deleted 3 reports. Type 'undo' within 10 min to restore."
    else User cancels or sends anything else
        U->>API: "no, actually show me Q1 revenue"
        API->>API: discard pending_action
        API-->>U: "Deletion cancelled." + continues with the new request
    end
```

Rules enforced in code:
- Ownership filter `owner_id = current_user` is applied in SQL by the tool — the LLM cannot widen it.
- Zero matches → no confirmation step, just an informative message.
- Only the **exact IDs shown** are deleted; if the set changed (e.g. a report was added), the confirmation is invalidated and re-proposed.
- Soft delete + audit log → reversible and traceable. A scheduled job hard-deletes after the retention window.

---

## 6. Flow C — learning loop & Golden Bucket ingestion

```mermaid
sequenceDiagram
    autonumber
    participant API as agent-api
    participant PS as Pub/Sub
    participant W as learning-worker
    participant LLM as Gemini (Flash-Lite)
    participant PG as Postgres
    actor A as Analyst (admin console)
    participant GCS as Golden Bucket (GCS)
    participant IX as golden-indexer
    participant BQ as BigQuery

    API--)PS: turn event {trace_id, question, sql, answer, feedback, prefs signals}
    PS->>W: push
    W->>LLM: extract preference signals ("prefers tables", "wants short answers")
    W->>PG: upsert user_profiles (weighted, with timestamps)
    W->>PG: if positive feedback & SQL succeeded → insert trio_candidates
    A->>PG: review candidate (approve / edit / reject)
    A->>GCS: approved trio written (question.md, query.sql, report.md, metadata.json)
    GCS--)PS: object finalize notification
    PS->>IX: push
    IX->>BQ: dry-run query.sql (still valid against current schema?)
    IX->>LLM: embed question + report summary
    IX->>PG: upsert golden_trios (embedding, tags, version, scope)
```

Learning happens **outside the request path**, so it never adds latency or cost to a user's turn. System-level changes (prompts, few-shots, schema descriptions) are validated by the `eval-runner` before activation.

---

## 7. Flow D — persona update without redeploy

```mermaid
sequenceDiagram
    autonumber
    actor C as CEO's office (non-developer)
    participant ADM as Admin console / admin-api
    participant PG as prompt_versions (Postgres)
    participant EV as eval-runner
    participant API as agent-api instances

    C->>ADM: edit "tone" text, click Save
    ADM->>PG: insert version n+1 (status = draft, author, diff)
    ADM->>EV: run smoke eval with draft persona
    EV-->>ADM: pass (safety + format checks)
    C->>ADM: Activate
    ADM->>PG: set version n+1 active
    API->>PG: next turn: cache TTL (60 s) expired → load active persona
    Note over API: New tone applies within ~1 min. Rollback = activate previous version.
```

The persona is only the **tone/style layer**. Safety rules, tool policies and output constraints live in the core system prompt, which is versioned in code and cannot be edited from the console.

---

## 8. Data storage & handling

| Data | Store | Written by | Read by | Retention / notes |
|------|-------|-----------|---------|-------------------|
| Transaction data (orders, items, products, users) | BigQuery | (source system) | `agent-api`, `golden-indexer` (dry-run) | Read-only. PII columns behind policy tags. |
| Conversation state (messages, tool results, pending actions) | Postgres (LangGraph checkpointer) | `agent-api` | `agent-api` | 90 days, then purged. Tool results stored **after** PII scrubbing. |
| Saved reports | Postgres `saved_reports` | `agent-api` | `agent-api` (owner only) | Soft delete; hard delete after 30 days. |
| Audit log (deletes, guard blocks) | Postgres `audit_log` → exported to BigQuery | `agent-api` | Security / ops | Append-only, 1 year. |
| User profile & preferences | Postgres `user_profiles` | `learning-worker`, explicit user commands | `agent-api` | User can view/reset ("forget my preferences"). |
| Entitlements (user → brands/categories) | Postgres `entitlements` (synced from IdP groups) | Admin / IdP sync | SQL Guard | Source of truth for data scoping. |
| Persona / prompt versions | Postgres `prompt_versions` | `admin-api` | `agent-api` (60 s cache) | All versions kept for rollback & audit. |
| Golden trios (raw) | Cloud Storage | Analysts, `learning-worker` (approved only) | `golden-indexer` | Object versioning on. |
| Golden trios (index) | Postgres + pgvector | `golden-indexer` | `agent-api` | Rebuildable from GCS at any time. |
| Cache, rate limits, breaker state | Memorystore | `agent-api` | `agent-api` | Ephemeral (TTL). |
| Logs, traces, metrics | Cloud Logging / Trace / Monitoring → BigQuery sink; Langfuse | all services | Engineers, dashboards | Logs contain **no raw PII** (scrubbed before logging). 30 days hot, 1 year in BigQuery. |

---

## 9. Security boundaries

Defense in depth — each layer works even if the one before it fails.

```mermaid
flowchart LR
    M[User message] --> L1["1 · Edge<br/>IAP auth, Cloud Armor rate limit"]
    L1 --> L2["2 · Input guard<br/>Model Armor + scope classifier"]
    L2 --> L3["3 · Hardened system prompt<br/>analysis-only, no PII, tool rules"]
    L3 --> L4["4 · SQL Guard (sqlglot)<br/>SELECT-only · table allowlist<br/>PII column block · scope rewrite<br/>LIMIT · bytes cap"]
    L4 --> L5["5 · BigQuery IAM<br/>read-only SA · policy tags deny PII"]
    L5 --> L6["6 · Result filter<br/>drop PII columns, cap rows"]
    L6 --> L7["7 · Output guard<br/>regex + DLP scan of final text"]
    L7 --> R[Answer]
```

- **Per-user data scope**: the SQL Guard rewrites every reference to `products` into a filtered subquery limited to the user's entitled brands/categories, so joined `order_items`/`orders` are scoped through the product. In production this is backed by BigQuery **row-level access policies** on authorized views per entitlement group.
- **PII**: blocked at SQL level (layer 4), unreadable at IAM level (layer 5), and scrubbed at output (layers 6–7). `user_id` may be used in joins and aggregates but is never displayed.

---

## 10. Prototype vs. production

The prototype keeps the same graph, tools and guard logic, and swaps managed services for local equivalents so it runs on any machine.

| Concern | Production | Prototype |
|---------|-----------|-----------|
| Interface | Web / Slack via LB + IAP | CLI (`rich`), user chosen with `--user` |
| Identity & entitlements | IAP JWT + Postgres `entitlements` | `config/users.yaml` |
| Compute | Cloud Run | Local Python process |
| LLM | Vertex AI Gemini | Gemini API (AI Studio key), primary + fallback model |
| Input screening | Model Armor + classifier | Gemini classifier + rules |
| PII column protection | Policy tags + SQL Guard | SQL Guard + result filter |
| Output PII check | DLP | Regex scrubber |
| Conversation state | Postgres checkpointer | LangGraph SQLite / in-memory checkpointer |
| Saved reports, audit log | Postgres | SQLite (`data/app.db`) |
| Golden trios | GCS + pgvector | `data/golden_trios/*.json` + in-memory similarity *(stretch)* |
| Persona | `prompt_versions` + admin console | `config/persona.yaml`, re-read every turn *(stretch)* |
| Observability | Cloud Logging/Trace/Monitoring + Langfuse | JSONL logs & traces in `logs/`, `/trace` and `/stats` CLI commands |

```mermaid
flowchart LR
    U[Terminal user] --> CLI["cli.py (rich)"]
    CLI --> G["LangGraph agent graph"]
    G <--> GEM["Gemini API"]
    G --> GUARD["SQL Guard + PII filter"] --> BQ[("BigQuery public dataset")]
    G <--> DB[("SQLite<br/>reports · audit · checkpoints")]
    G --> CFG["config/*.yaml<br/>schema · users · persona"]
    G -. events .-> LOGS[("logs/*.jsonl<br/>traces & metrics")]
```
