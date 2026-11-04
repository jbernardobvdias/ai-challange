# Technical Design — Retail Data Analysis Chat Assistant

Companion to [architecture.md](architecture.md) (diagrams and flows). This document explains **why** each building block was chosen and how the system behaves in production. The requirement-by-requirement walkthrough is in [requirements.md](requirements.md) *(written in a later step)*.

**Contents**
1. [Guiding principles](#1-guiding-principles)
2. [Cloud platform](#2-cloud-platform)
3. [Service choices](#3-service-choices)
4. [LLM models](#4-llm-models)
5. [Agent framework](#5-agent-framework)
6. [Data flow summary](#6-data-flow-summary)
7. [Error handling & fallback strategies](#7-error-handling--fallback-strategies)
8. [Extensibility](#8-extensibility)
9. [Scaling & cost](#9-scaling--cost)

---

## 1. Guiding principles

1. **Deterministic safety, probabilistic intelligence.** The LLM decides *what* to ask; deterministic code decides *what is allowed*. PII blocking, data scoping, SQL validation and destructive-action execution never depend on the model behaving well.
2. **Keep the user path short; learn asynchronously.** Everything that is not needed to answer the current turn (preference learning, trio indexing, evals) runs on Pub/Sub workers.
3. **Few managed services, each with a clear job.** One operational database (Postgres + pgvector) instead of separate document, vector and relational stores.
4. **Every turn is traceable.** One `trace_id` links the user message, each LLM call, each SQL statement and the final answer.
5. **Capabilities are tools.** New features are added as tools in a registry, not as changes to the graph.

---

## 2. Cloud platform

**Google Cloud.** The data already lives in BigQuery and the client prefers Gemini, so staying on GCP avoids cross-cloud egress, keeps IAM in one place (the agent's service account can be granted BigQuery, Vertex AI and Cloud SQL access directly) and gives enterprise terms for Gemini through Vertex AI (no training on customer data, regional processing, SLA).

---

## 3. Service choices

| Building block | Choice | Why | Alternatives considered |
|----------------|--------|-----|-------------------------|
| Agent compute | **Cloud Run** | Serverless containers, autoscaling (including to zero), long request timeouts and streaming (SSE) for chat, per-revision traffic splitting for safe rollouts. The agent is I/O-bound (waiting on LLM/BigQuery), which suits Cloud Run's concurrency model. | GKE (more ops overhead), Cloud Functions (weaker for long, streaming requests), Vertex AI Agent Engine (less control over the graph and guards). |
| Async workers | **Cloud Run Jobs / services + Pub/Sub** | Same container tooling as the API; Pub/Sub gives retries, dead-letter topics and fan-out for new consumers (e.g. an email-report worker). | Cloud Tasks (point-to-point only), Dataflow (overkill). |
| Auth | **IAP** + Google Workspace / Identity Platform | SSO without writing auth code; the app receives a signed identity header. Group membership maps to data entitlements. | Custom OAuth in the app. |
| Edge | **HTTPS LB + Cloud Armor** | WAF, rate limiting and DDoS protection in front of the agent — a cheap first defense against abusive users. | — |
| Warehouse | **BigQuery** | Given. Adds **policy tags** (column-level security) for PII, **row-level access policies** for scoping, **dry runs** for cost estimation and `maximum_bytes_billed` as a hard cost cap. | — |
| Operational DB | **Cloud SQL for PostgreSQL + pgvector** | Transactions for reports/audit/confirmations, official LangGraph Postgres checkpointer, and vector search in the same database. Golden trios number in the thousands, not billions, so pgvector is plenty fast. | Firestore (no vector joins or transactions across tables), AlloyDB (stronger but pricier; a natural upgrade path), Vertex AI Vector Search (built for much larger corpora). |
| Golden Bucket | **Cloud Storage** | Cheap, versioned, analyst-friendly (files you can open and edit) and triggers Pub/Sub events on change. The vector index is derived and can be rebuilt from it. | Storing trios only in the DB (harder for analysts to manage). |
| Cache & limits | **Memorystore (Redis)** | Shared across Cloud Run instances: per-user rate limits, circuit-breaker state, schema and repeated-query cache. | In-process cache (not shared between instances). |
| Input safety | **Model Armor** + in-graph classifier | Managed detection of prompt injection and jailbreaks, plus our own analysis-only scope check. | Classifier only. |
| Output PII | **Sensitive Data Protection (DLP)** | Mature detectors (names, emails, phones, addresses) as a final safety net after the deterministic filters. | Regex only (used in the prototype). |
| Observability | **Cloud Logging, Trace, Monitoring** + **Langfuse** | Native GCP telemetry for infrastructure-level alerting; Langfuse for LLM-specific views (prompt/response per step, token cost, scores, prompt versions). Log sink to BigQuery enables SQL analysis of failures. | LangSmith (SaaS; data leaves GCP), Arize Phoenix. |
| Secrets | **Secret Manager** | Standard; mounted into Cloud Run. | — |
| CI/CD | **Cloud Build + Artifact Registry** | Builds the container, runs unit tests **and the eval suite** as a release gate, deploys a new Cloud Run revision with gradual traffic shift. | GitHub Actions (equally fine). |

---

## 4. LLM models

| Role | Model | Why |
|------|-------|-----|
| Main agent (planning, SQL, analysis, reports) | **Gemini 3.8 Flash** | Strong tool calling and SQL generation, long context (schema + few-shot trios + history fit easily), low latency and cost for interactive chat. |
| Fallback + cheap steps (input classification, preference extraction) | **Gemini 3.5 Flash-Lite** | Faster and cheaper; used automatically when the primary model is rate-limited or unavailable. |
| Embeddings (golden trios) | **Gemini embeddings** | Same provider and auth; good multilingual semantic retrieval. |

- **Production** calls Gemini through **Vertex AI** (IAM auth, regional endpoints, enterprise data terms). The **prototype** uses a Gemini API key from Google AI Studio; the code path is the same SDK (`google-genai`), so switching is configuration only.
- Models are set by configuration (`GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`), not hard-coded, so we can upgrade or A/B test without code changes.
- During development the primary Flash model repeatedly returned `503 UNAVAILABLE` / `504 DEADLINE_EXCEEDED` on the free tier, which confirmed the need for an automatic fallback model and bounded timeouts (see §7).

---

## 5. Agent framework

**LangGraph** (Python).

| Need | How LangGraph covers it |
|------|-------------------------|
| Deterministic guard nodes around an LLM loop | The agent is an explicit state graph: guard, tool, confirmation and fallback nodes are plain Python functions with conditional edges. |
| Strict confirmation for destructive actions | Built-in **human-in-the-loop `interrupt()`**: the graph pauses, state is checkpointed, and resumes with the user's decision. |
| Conversation memory & resumability | **Checkpointers** (SQLite for the prototype, Postgres in production) persist state per conversation thread. |
| Self-correction with a bounded budget | Retry counters live in the graph state; conditional edges route to a graceful fallback when exhausted. |
| Extensibility | New capabilities are new tools bound to the model; the graph stays the same. |
| Observability | Every node/tool call is a discrete step that we can wrap with tracing; native integration with Langfuse/OpenTelemetry. |

Alternatives considered:
- **Google ADK** — good Gemini/Vertex integration, but less explicit control over custom guard and confirmation flows at the time of writing.
- **Plain `google-genai` SDK with a hand-written loop** — minimal dependencies, but we would re-implement checkpointing, interrupts and state management.
- **CrewAI / multi-agent frameworks** — more abstraction than needed; a single agent with well-designed tools is easier to secure and debug.

**My experience with LangGraph:** _TODO — describe your level of experience (e.g. projects built, how long you have used it)._

---

## 6. Data flow summary

Detailed sequence diagrams are in [architecture.md §4–7](architecture.md#4-flow-a--analysis-question-with-sql-self-correction). Summary of one analysis turn:

1. **Request** — Client sends the message over HTTPS; IAP authenticates and forwards the identity to `agent-api`.
2. **Context load** (parallel) — conversation checkpoint, user entitlements and preferences, active persona (cached 60 s), schema description (cached), top-k golden trios by vector similarity, filtered to the user's scope.
3. **Input guard** — Model Armor + Flash-Lite classifier: analysis request / off-topic / injection / PII request.
4. **Agent loop** — Gemini plans and calls tools. `run_sql` passes each statement through the SQL Guard, then a BigQuery dry run (cost check), then execution with a timeout and `maximum_bytes_billed`. Results are PII-filtered and truncated before the model sees them.
5. **Output guard** — PII scrub (regex + DLP) and format checks on the final text.
6. **Response** — streamed to the client; checkpoint saved.
7. **After the turn** — a turn event (trace ID, SQL, outcome, latency, tokens, feedback) is published to Pub/Sub for the learning worker and analytics.

---

## 7. Error handling & fallback strategies

| Failure | Detection | Strategy | User sees |
|---------|-----------|----------|-----------|
| SQL rejected by the guard (PII column, DML, unknown table) | SQL Guard | Structured error + hint returned to the model as a tool observation; counts toward the failure limit. | Nothing (internal), unless the request itself is disallowed → polite refusal. |
| BigQuery syntax / semantic error | `BadRequest` from the dry run, which is free | Error message returned to the model, which already has the schema; **stops after 3 consecutive SQL failures**. No retry of the same SQL. | Final answer, or a clear explanation after the limit. |
| Empty result | 0 rows, or an aggregate returning only NULLs | Hint telling the model to check filters and date range, plus the user's scope; counts toward the failure limit, so it cannot loop. | "No data" stated honestly, or an explanation of the access limit. |
| Query too expensive | Dry-run bytes above the cap (default 1 GB) | Not executed; model asked to filter or aggregate. `maximum_bytes_billed` is a second cap enforced by BigQuery itself. | Nothing, or a request to narrow the question. |
| Gemini overloaded / timed out | SDK error / timeout | Primary gets one 30 s attempt, then the **fallback model** (2 attempts, 60 s). **Circuit breaker**: after 2 consecutive failures the primary is skipped for 60 s, so users don't each wait for its timeout. | Slight delay; a note saying the backup model answered. |
| Gemini quota exhausted | `429` with `retryDelay` | Circuit opens immediately for the delay Google states, which can be hours on the free tier. | As above. |
| All models down | Every link in the chain fails or is open | Canned message without crashing; conversation preserved. | "I can't reach the AI service right now…" |
| Input classifier down | Same breaker mechanism | Skipped for 120 s after 2 failures; rules, the SQL guard and output filters keep working. | Nothing. |
| BigQuery unavailable / timeout | Client error / timeout | One retry with exponential backoff and jitter, 30 s timeout, timed-out jobs cancelled; circuit breaker; recent identical queries served from a 10-minute cache. Credential or permission errors take the same path, since the model can't fix them. | Partial answer if some queries succeeded, otherwise "The data warehouse is unavailable right now…" |
| Budget exhausted (8 LLM calls or 150k tokens per turn) | Counted from the turn's messages | Stop the loop; one final call without tools summarises what was found. | Partial answer + what is missing, or a request to split the question. |
| Turn cancelled or crashed mid-tool-call | Tool call without a result in the history | Open tool calls are closed before the next model call, since Gemini would otherwise reject the conversation permanently. | Ctrl-C: "Cancelled". Crash: friendly message with a reference ID (details in `logs/agent.log`). |
| Async worker failure *(design only)* | Pub/Sub delivery failure | Automatic redelivery; dead-letter topic + alert after N attempts. User path unaffected. | Nothing. |

**Cost control:** failure and call limits per turn, BigQuery dry run + `maximum_bytes_billed`, result cache, at most 50 rows sent to the model, circuit breakers that avoid doomed calls, Flash-Lite for classification. Budgets are derived from the turn's messages rather than kept as separate counters, so they cannot drift out of sync with what actually happened.

**Prototype limitation:** breaker state and the result cache live in one process. Production keeps them in Memorystore so all instances share them.

---

## 8. Extensibility

A capability is a **tool** with a typed schema, a description for the model, and an implementation. Adding one requires:

1. Implement the tool (e.g. `create_chart(data_ref, chart_type)` → PNG in GCS, `send_report_email(report_id, recipients)` → Pub/Sub job, `web_search(query)`).
2. Register it in the tool registry (optionally restricted by user role).
3. Add eval cases for it.

Tools with side effects outside the system (email, deletion) reuse the same **propose → confirm → execute** pattern as report deletion. Long-running tools publish a job to Pub/Sub and return immediately, so the chat stays responsive.

---

## 9. Scaling & cost

- `agent-api` is stateless; Cloud Run scales horizontally on concurrent requests. Conversation state is in Postgres, so any instance can serve any turn.
- The main cost drivers are LLM tokens and BigQuery bytes scanned, both capped per turn and tracked per user in metrics.
- BigQuery: aggregation queries over `thelook_ecommerce` scan megabytes, well within the free tier; production tables would use partitioning/clustering on `created_at` and the guard would require a date filter on large tables.
