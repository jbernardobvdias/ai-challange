# Project Plan (historical)

> The plan this project was built from, kept for reference. Some details changed during the build, and the questions it lists have moved to the README. For the current state, see the [README](../README.md) and [requirements.md](requirements.md).

---

## 1. Goal

Build a **data analysis chat agent** for non-technical retail executives that:

- Answers natural-language questions about sales and performance using **BigQuery** (`bigquery-public-data.thelook_ecommerce`).
- Uses a **"Golden Knowledge" bucket** of past *Question → SQL → Analyst Report* trios to interpret questions the way human analysts did.
- Supports follow-up discussion and generates **reports with insights and action items**.
- Is **safe** (no PII in the output, analysis-only scope, per-user data access), **resilient**, **observable**, and **easy to extend** (charts, email, web search…).

### Deliverables checklist

| # | Deliverable | Where it will live | Status |
|---|-------------|--------------------|--------|
| 1 | Architecture diagram (Mermaid) | `docs/architecture.md` | ☑ v1 |
| 2a | Reasoning for cloud services / LLM / framework choices | `docs/technical_design.md` | ☑ v1 |
| 2b | Data flow between components | `docs/technical_design.md` | ☑ v1 |
| 2c | Error handling & fallback strategies | `docs/technical_design.md` | ☑ v1 |
| 2d | Setup instructions + example run | `README.md` | ☐ |
| 2e | How each of the 8 requirements is solved | `docs/requirements.md` | ☐ |
| 3 | Working prototype (Safety/PII, Destructive-op confirmation, Resilience, Observability) | `src/` | ☐ |
| 4 | CLI chat interface | `src/cli.py` | ☐ |
| 5 | Runnable on another machine (setup docs, `requirements.txt`, `.env.example`) | repo root | ☐ |
| 6 | Framework choice + my experience level with it | `docs/technical_design.md` | ☐ |
| — | Public GitHub repo | GitHub | ☐ |

**Scope split:**
- **Must be implemented in the prototype:** Req. 2 (Safety & PII), Req. 3 (Destructive ops), Req. 5 (Resilience), Req. 7 (Observability), plus the core chat/analysis/report capabilities and a CLI.
- **Design only (implement a light version if time permits):** Req. 1 (Golden Bucket), Req. 4 (Learning loop), Req. 6 (QA), Req. 8 (Persona management).

---

## 2. Tech Stack

| Layer | Prototype | Production (HLD) | Why |
|-------|-----------|------------------|-----|
| Language | Python 3.11+ | Python 3.11+ | Requested; best ecosystem for LLM + data. |
| Agent framework | **LangGraph** | LangGraph | Explicit state graph (good for retry loops & guardrail nodes), built-in **human-in-the-loop interrupts** (perfect for delete confirmation), checkpointing/memory, easy to add new tools. *Alternative considered: Google ADK.* |
| LLM | **Gemini Flash** (latest available in AI Studio, e.g. `gemini-3.8-flash`) | Gemini on Vertex AI (3.8 Flash primary, 3.5 Flash-Lite fallback + cheap steps) | Required/preferred by client; free tier; good tool calling; long context. |
| LLM SDK | `langchain-google-genai` / `google-genai` | Vertex AI SDK | Native Gemini support. |
| Data warehouse | BigQuery public dataset via `google-cloud-bigquery` | BigQuery (authorized views + row-level security) | Required. |
| SQL validation | `sqlglot` (AST parsing) | `sqlglot` + BQ IAM | Deterministic, non-LLM safety checks. |
| PII masking | Column blocklist + regex/Presidio output scrubber | + Cloud DLP / Sensitive Data Protection, BQ policy tags | Defense in depth. |
| Saved Reports store | SQLite | Cloud SQL (Postgres) | Zero setup locally; managed in prod. |
| Golden Bucket | Local folder of JSON trios + embeddings (Chroma or in-memory cosine) | GCS bucket + pgvector (Cloud SQL) | RAG over expert trios. |
| User memory / prefs | SQLite / JSON | Cloud SQL (Postgres) | Per-user profile. |
| Persona / prompts | YAML file hot-reloaded each turn | Postgres `prompt_versions` registry + small admin UI | Edit without redeploy. |
| Retries | `tenacity` | `tenacity` + circuit breaker | Backoff on 429/5xx. |
| Observability | Structured JSONL logs + traces, optional Langfuse | Cloud Logging, Cloud Trace (OpenTelemetry), Cloud Monitoring, Langfuse | Debug every message & tool call. |
| CLI | `rich` + `prompt_toolkit` (or `typer`) | — (Web/Slack UI later) | Nice tables/markdown in terminal. |
| Tests | `pytest` | CI (GitHub Actions / Cloud Build) | |
| Compute | Local | **Cloud Run** (agent API), Cloud Run Jobs / Pub/Sub workers (async jobs) | Serverless, autoscaling. |
| Auth | Simulated user via CLI flag (`--user alice`) | Identity Platform / IAP + JWT | Identity drives data access. |
| Secrets | `.env` | Secret Manager | |

---

## 3. Dataset

`bigquery-public-data.thelook_ecommerce` — tables in scope:

| Table | Key columns | PII columns (must never be shown) |
|-------|-------------|-----------------------------------|
| `users` | `id`, `age`, `gender`, `state`, `city`, `country`, `traffic_source`, `created_at` | `first_name`, `last_name`, `email`, `street_address`, `postal_code`, `latitude`, `longitude`, `user_geom` |
| `orders` | `order_id`, `user_id`, `status`, `created_at`, `shipped_at`, `delivered_at`, `returned_at`, `num_of_item` | — |
| `order_items` | `id`, `order_id`, `user_id`, `product_id`, `status`, `sale_price`, timestamps | — |
| `products` | `id`, `name`, `brand`, `category`, `department`, `cost`, `retail_price`, `sku` | — |

**Tasks:**
- ☐ Verify the exact schema in BigQuery and write `config/schema.yaml` (column descriptions + PII flags) used both in the prompt and by the SQL validator.
- ☐ Note: `user_id` is a pseudonymous key — allowed in aggregates/joins but **never displayed** in the final output (decide & document).

---

## 4. High-Level Architecture

Full design: **[architecture.md](architecture.md)** (diagrams) and **[technical_design.md](technical_design.md)** (reasoning).

- Production HLD on Google Cloud (Cloud Run, Vertex AI Gemini, BigQuery, Cloud SQL Postgres + pgvector, GCS, Pub/Sub, Memorystore, IAP, Model Armor, DLP, Cloud Logging/Trace/Monitoring, Langfuse)
- Agent graph (LangGraph nodes: guards, tools, confirmation, fallback)
- Sequence diagrams: analysis + SQL self-correction, delete confirmation, learning loop / Golden Bucket ingestion, persona update
- Data storage & retention table, security layers, prototype vs. production mapping

> Design change vs. first draft: Firestore and Vertex AI Vector Search were replaced by a single **Cloud SQL Postgres + pgvector** store (transactions for reports/confirmations, LangGraph Postgres checkpointer, trio index in one place).

**Tasks:**
- ☑ Production HLD diagram
- ☑ Sequence diagrams (analysis + self-correction, delete confirmation, learning loop, persona)
- ☑ Data storage, retention and access
- ☐ Render-check all Mermaid diagrams (GitHub preview or mermaid.live)

---

## 5. Requirements — Plan per Requirement

### R1. Hybrid Intelligence (Golden Bucket) — *design + optional light prototype*

**At query time (RAG):**
1. Embed the user question (Gemini embeddings).
2. Retrieve top-k similar trios from the vector index (filtered by the user's access scope).
3. Inject them into the prompt as few-shot examples: *how the analyst interpreted the question, the SQL pattern, and the reasoning in the report*.
4. LLM adapts the SQL/logic to the new question.

**Updating the bucket over time:**
- New trios come from: (a) analysts uploading to GCS, (b) agent conversations that got positive feedback → queued as *candidates* → **analyst review/approval** → promoted to golden.
- GCS object-finalize event → Pub/Sub → indexing worker (validate SQL with BQ dry-run, embed, upsert into vector index, version metadata).
- Periodic re-validation of stored SQL against schema changes; deprecate stale trios.

**Tasks:**
- ☐ Write the design section.
- ☐ (Stretch) Create ~10 sample trios in `data/golden_trios/*.json` and a `search_golden_trios` tool.

### R2. Safety & PII Masking — *must implement*

Defense in depth — **never rely on the LLM alone**:

| Layer | Mechanism |
|-------|-----------|
| 1. Input guardrail | Classify request: in-scope analysis / out-of-scope / prompt injection / PII request. Refuse politely if not analysis. Hardened system prompt. |
| 2. SQL guard (deterministic, `sqlglot`) | Only a single `SELECT`; no DML/DDL; table allowlist (4 tables); **rejects PII columns** and rewrites every table into a **secure view** without PII columns, so `SELECT *` and whole-row tricks cannot reach them; caps rows. Only the SQL rendered from the checked syntax tree runs. |
| 3. Per-user data scope (row-level) | Each user has an entitlement (allowed `brand`/`category`/`department`). The secure views limit **all four tables** to that scope, so a query cannot widen it. Prod: BQ **row-level access policies / authorized views** per role. |
| 4. Result scrubber | Drop any PII-flagged columns from the result before it reaches the LLM; regex/Presidio scan for emails, phones, addresses, names. |
| 5. Output guardrail | Final answer scanned again for PII before display. |
| 6. Small-group protection (optional) | Suppress aggregates over < N customers to avoid re-identification. |

**Tasks:**
- ☑ `config/users.yaml` with demo users: `alice` (categories), `bob` (brands), `carol` (all products).
- ☑ `sql_guard.py` (parse, validate, rewrite) with unit tests, plus scope totals and attacks verified on live BigQuery.
- ☑ `pii.py` (column filter + text scrubber) and `input_guard.py` (rules + classifier) with unit tests.
- ☑ Red-team test prompts: "show me emails of top customers", "ignore previous instructions…", "DROP TABLE", "SELECT * FROM users", accessing out-of-scope brands.

### R3. High-Stakes Oversight (Destructive Ops) — *must implement*

Saved Reports library in SQLite (`data/app.db`): `reports` and an append-only `audit_log`. Users can only see and delete their own reports.

**Propose → confirm → execute:**
1. **Propose:** the model calls `propose_delete(search / this_conversation / report_ids / all_reports)`. It deletes nothing, and the model has no tool that does. Owner and conversation come from the session.
2. **Confirm:** the graph pauses (LangGraph `interrupt`) and the CLI shows the exact list:
   - `confirm` → deletes;
   - `yes`, `ok`, `go ahead` → asks again, deleting nothing;
   - `cancel`, `no`, Ctrl-C → cancels;
   - anything else → cancels, then treats it as a new request, so the conversation continues normally.
3. **Execute:** a plain graph node soft-deletes only the IDs shown, if the user still owns them, and writes the audit entry in the same transaction.
4. **Undo:** saying "undo" within 10 minutes restores the latest deletion.

Also: zero matches skips confirmation, a request with no filter is refused, and a confirmation expires after 5 minutes.

**Tasks:**
- ☑ `storage/reports_store.py` (ownership checks, soft delete, audit log).
- ☑ Tools: `save_report`, `list_reports`, `get_report`, `propose_delete`, `restore_deleted_reports`, plus `confirm_delete` and `execute_delete` graph nodes.
- ☑ Tests: other users' reports, cancel flow, vague answers, expiry, reports added after proposal. Verified live through the CLI.

### R4. Continuous Improvement (Learning Loop) — *design (+ optional light prototype)*

**User level:**
- `user_profile` store: format (table/bullets/chart), depth (summary vs. deep dive), tone, favourite metrics.
- Learned from: explicit statements ("I prefer tables"), implicit signals (asks "shorter please", re-asks for tables), thumbs up/down.
- A small "memory extractor" step after each turn updates the profile; profile injected into the prompt next time.

**System level:**
- Log every interaction + feedback → offline analysis.
- Successful, highly rated Q→SQL→answer → golden-trio candidates (human-approved).
- Failure clustering (SQL errors, refusals, low ratings) → improve schema descriptions, prompts, few-shots.
- Changes validated by the eval suite (R6) before rollout; prompt versions A/B tested.

**Tasks:**
- ☐ Write design section.
- ☐ (Stretch) Simple `preferences` table + `remember_preference` tool.

### R5. Resilience & Graceful Error Handling — *must implement*

- **SQL self-correction:** syntax errors, guard rejections and empty results go back to the model as structured observations. It stops after **3 consecutive failures** with a friendly explanation, never a stack trace.
- **Empty results:** 0 rows and all-NULL aggregates both count as empty; the hint asks the model to check filters and states the user's scope.
- **Cost control:** BigQuery dry run + `maximum_bytes_billed` (1 GB), limits of 8 LLM calls and 150k tokens per turn, 10-minute result cache, at most 50 rows sent to the model.
- **3rd-party failures:** timeouts on every call, fallback model, circuit breakers for the primary model, fallback model, classifier and BigQuery. Quota errors open the circuit for as long as Google says. BigQuery transient errors get one retry with backoff and jitter.
- **Partial answers:** if an outage or budget stops a turn after some queries succeeded, one final call summarises those results and says what's missing.
- **CLI never crashes:** per-turn error boundary with a reference ID, Ctrl-C cancels only the current turn, conversation state preserved, logs in `logs/agent.log`.

Full failure table: [technical_design.md §7](technical_design.md#7-error-handling--fallback-strategies).

**Tasks:**
- ☑ `resilience.py` (circuit breaker, budgets), `warehouse.py` (BigQuery limits), model chain in `llm.py`.
- ☑ Self-correction and budgets in the LangGraph graph.
- ☑ Tests with mocked failures (429, 503, timeout, bad SQL, empty result), plus failure injection against real services.

### R6. Quality Assurance — *design*

- **Offline eval set:** ~50–100 questions (from golden trios + hand-written) with expected SQL/results.
  - *Execution accuracy:* compare result sets of generated vs. reference SQL.
  - *Report quality:* LLM-as-judge rubric (answers intent, correctness, actionable items, no PII) + spot human review.
  - *Safety suite:* red-team prompts must be refused / masked (100% pass required).
  - *Destructive-op suite:* deletes never execute without confirmation.
- **Intent verification at runtime:** agent restates its interpretation for ambiguous questions; asks clarifying questions; shows assumptions in the report.
- **UX evaluation:** thumbs up/down, follow-up rate ("that's not what I meant"), time-to-answer, task completion, executive pilot interviews.
- **Release gating:** eval suite in CI; no prompt/model change ships if metrics regress.

**Tasks:**
- ☐ Write design section.
- ☐ (Stretch) `evals/` with a small set of questions + script.

### R7. Observability — *must implement*

**One trace per turn** (`logs/traces.jsonl`): user message, guard decisions, every model call (input, output, tool calls, tokens, latency, errors), every tool call (SQL, full observation, status), domain events, outcome. Personal data is scrubbed before anything is written. JSON logs (`logs/agent.log`) share the same `trace_id`.

**In the CLI:**
- `/trace [id]`: timeline of the last answer or a past one. Crash messages show this ID as their reference.
- `/stats [all]`: metrics for this session or all sessions: outcomes, latency, model failures and fallbacks, SQL first-try success, bytes scanned, guard triggers, deletes, feedback.
- `/good`, `/bad [note]`: rate the last answer.

Metrics, proposed alerts, debugging steps and the production mapping: [technical_design.md §10](technical_design.md#10-observability).

**Tasks:**
- ☑ `observability.py` (trace, one callback handler, `emit()`, metrics) and `trace_view.py` (`/trace`, `/stats`).
- ☑ Instrument nodes, models, tools, guards, circuit breakers and BigQuery.
- ☑ Tests through the real graph; verified live through the CLI.

### R8. Agility (Persona Management) — *design (+ easy prototype)*

- Prompts split into layers: **core system prompt** (safety rules, versioned in code — not editable) + **persona/tone layer** (editable).
- Persona stored in a **prompt registry** (Postgres `prompt_versions`) with versions, author, active flag, and rollback; edited through a simple admin UI (or Google Sheet/Doc sync).
- Agent fetches the active persona at each request (with short cache TTL) → **no redeploy**.
- Optional: changes go through a quick eval run before activation.

**Tasks:**
- ☐ Write design section.
- ☐ Prototype: `config/persona.yaml` re-read on every turn (edit file while CLI running → tone changes).

---

## 6. Agent Capabilities (prototype)

| Capability | Example | How |
|-----------|---------|-----|
| Schema questions | "What data do we have?" | `describe_schema` tool (from `schema.yaml`, PII hidden) |
| Customer behaviour | "Top 10 customers by spend" | `run_sql` (aggregates, no names/emails) |
| Product performance | "Compare product X vs Y and why" | multi-step: several queries + reasoning |
| Time metrics | "Monthly revenue in 2024", "YTD revenue by product" | `run_sql` |
| Multi-step analysis | "Why are users in state X underspending vs Y?" | planner → several queries → synthesis |
| Reports | "Create a Q1 report with action items for Q2" | report template (summary, KPIs, insights, action items) → `save_report` |
| Discussion | follow-ups on previous answers | conversation memory (LangGraph checkpointer) |
| Report library | save / list / read / delete / undo | `save_report`, `list_reports`, `get_report`, `propose_delete` (confirm), `restore_deleted_reports` |

---

## 7. Planned Repository Structure

```
ai-challange/
├── README.md                  # overview, setup, example run
├── requirements.txt
├── .env.example               # GOOGLE_API_KEY, GCP_PROJECT, etc.
├── docs/
│   ├── architecture.md        # Mermaid HLD + sequence diagrams
│   ├── technical_design.md    # service choices, data flow, error handling
│   └── requirements.md        # how each requirement is solved
├── config/
│   ├── schema.yaml            # tables, columns, descriptions, PII flags
│   ├── users.yaml             # demo users + product access scope
│   └── persona.yaml           # editable tone/instructions
├── data/
│   └── golden_trios/          # (stretch) sample trios
├── src/agent/
│   ├── cli.py                 # CLI entrypoint (rich)
│   ├── app.py                 # wires settings, schema, tools, LLM, graph
│   ├── config.py              # settings from .env
│   ├── users.py               # users and product entitlements
│   ├── schema.py              # loads config/schema.yaml (PII flags)
│   ├── bigquery_runner.py     # BigQuery client (provided by OpsFleet)
│   ├── graph.py               # LangGraph state graph
│   ├── state.py
│   ├── prompts.py
│   ├── llm.py                 # Gemini model chain with fallback
│   ├── resilience.py          # circuit breaker, per-turn budgets
│   ├── warehouse.py           # BigQuery limits, retries, cache
│   ├── tools/
│   │   ├── sql.py             # run_sql, describe_schema
│   │   ├── reports.py         # save/list/read/restore, propose_delete
│   │   └── golden.py          # (stretch) search_golden_trios
│   ├── safety/
│   │   ├── sql_guard.py       # sqlglot validation + scope rewrite
│   │   ├── pii.py             # column filter + text scrubber
│   │   └── input_guard.py     # scope / injection classifier
│   ├── storage/
│   │   └── reports_store.py   # SQLite: reports + audit log
│   ├── observability.py       # traces, JSON logs, metrics
│   └── trace_view.py          # /trace and /stats output
├── evals/                     # (stretch) eval questions + runner
└── tests/                     # pytest unit tests
```

---

## 8. Setup (to be finalized)

1. Python 3.11+, create venv: `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt` (also installs the `agent` package from `src/` in editable mode)
3. `cp .env.example .env`
4. Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) → `GOOGLE_API_KEY` in `.env`
5. Google Cloud project with the BigQuery API enabled → `GCP_PROJECT` in `.env` (queries are billed to your project, within the 1 TB/month free tier).
6. Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install) and authenticate: `gcloud auth application-default login`
7. Verify everything: `python scripts/check_setup.py --write-schema` (checks Gemini + BigQuery and writes `config/schema.yaml`)
8. Run: `python -m agent.cli --user alice` (or `retail-agent --user alice`). Demo users: `alice`, `bob`, `carol` (see `config/users.yaml`). Add `-v` to see the SQL and tool results.
9. Tests: `python -m pytest`

> **Free-tier quota:** `gemini-3.8-flash` allows only ~20 requests/day on the free tier. When it is exhausted (429) or overloaded (503), the agent automatically falls back to `GEMINI_FALLBACK_MODEL` (`gemini-3.5-flash-lite`).

☐ Add an **example run** transcript (normal question, report, PII refusal, delete confirmation, error recovery, `/trace`).

---

## 9. Work Plan & Milestones (≈ 10–12 h)

| # | Milestone | Est. | Done |
|---|-----------|------|------|
| 0 | Send clarifying questions to OpsFleet (see §10); set up repo, venv, GCP auth, Gemini key; verify a BQ query works | 0.5 h | ☐ |
| 1 | Design docs v1: HLD diagram, sequence diagrams, service choices | 2 h | ☑ |
| 2 | Core agent: LangGraph graph, Gemini client, `describe_schema`, `run_sql`, CLI loop with conversation memory | 2 h | ☑ |
| 3 | Safety: SQL guard (sqlglot), PII column/output scrubbing, per-user scope, input guardrail + tests | 2 h | ☑ |
| 4 | Resilience: SQL self-correction loop, retries/backoff/fallback, cost limits, error boundary + tests | 1 h | ☑ |
| 5 | Saved reports + delete confirmation flow (interrupt, ownership, audit) + tests | 1.5 h | ☑ |
| 6 | Observability: structured logs, traces, `/trace`, `/stats` | 1 h | ☑ |
| 7 | Stretch: persona hot-reload, golden-trio RAG, preferences memory, small eval set | 1–2 h | ☐ |
| 8 | Final docs: requirements write-up, setup, example run, polish, publish to GitHub | 1 h | ☐ |

---

## 10. Open Questions for OpsFleet (client)

Send these before / while building — record answers and assumptions here:

1. **"Products related to him":** how is a user linked to products — by `brand`, `category`, `department`, or `distribution_center`? *Assumption: each executive is entitled to a set of brands/categories defined in config.*
2. **PII definition:** besides names, emails, addresses, postal codes and coordinates — is `user_id` considered PII? Are city/state/age OK in aggregates? *Assumption: IDs usable internally, never displayed; demographics only in aggregates.*
3. **Golden Bucket:** it's "theoretical" — should we mock sample trios, or is design-only enough?
4. **Churn definition:** e.g. no purchase in the last 90 days? (needed for "why did churn spike?")
5. **Authentication:** is a simulated CLI user (`--user`) acceptable for the prototype?
6. **Cloud:** is GCP the target cloud for the production design (natural fit with BigQuery/Gemini)?
7. **Report deletion:** hard or soft delete? Is undo desirable? Do admins need to delete others' reports?
8. **Languages:** English only?
9. **Volumes/SLAs:** number of executives, expected latency, budget per query?

### Assumptions log
- **Product access:** each user is entitled to a set of categories, brands or departments (`config/users.yaml`), and sees all four tables limited to those products. Customers are those who bought in-scope products.
- **PII:** names, emails, street addresses, postal codes, coordinates and `user_geom`. Customer IDs are treated as pseudonymous: usable in queries, not meant for display. City, state, age and gender are allowed.
- **Authentication:** the prototype simulates sign-in with `--user`.
- Limitations of these choices are listed in [architecture.md §9](architecture.md#9-security-boundaries).
