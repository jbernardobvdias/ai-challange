# How Each Requirement Is Solved

For each requirement: the production design, what the prototype implements, and how it was verified. Diagrams are in [architecture.md](architecture.md), and technology choices in [technical_design.md](technical_design.md).

| # | Requirement | Brief asks for | Status in the prototype |
|---|-------------|----------------|-------------------------|
| 1 | [Hybrid Intelligence](#1-hybrid-intelligence-golden-bucket) | Design | Working small version: 10 sample trios, semantic retrieval |
| 2 | [Safety & PII Masking](#2-safety--pii-masking) | **Prototype** | Implemented |
| 3 | [High-Stakes Oversight](#3-high-stakes-oversight-destructive-operations) | **Prototype** | Implemented |
| 4 | [Continuous Improvement](#4-continuous-improvement-the-learning-loop) | Design | Explicit preferences and feedback capture; the rest is design |
| 5 | [Resilience](#5-resilience--graceful-error-handling) | **Prototype** | Implemented |
| 6 | [Quality Assurance](#6-quality-assurance) | Design | Working evaluation suite and 305 unit tests |
| 7 | [Observability](#7-observability) | **Prototype** | Implemented, except alert delivery |
| 8 | [Agility](#8-agility-persona-management) | Design | Tone file that applies without a restart; the admin console is design |

---

## 1. Hybrid Intelligence (Golden Bucket)

**Goal:** apply how analysts interpreted past questions, not only what the database contains. The value is in definitions the schema can't express: what counts as churn, why "underspending" means spend per buyer rather than total revenue, why a partial month must be excluded.

### Using the bucket at query time

1. `load_examples` runs after the input guard, so blocked messages cost nothing.
2. The question is embedded (`gemini-embedding-001`) and matched against the index, in production with pgvector in Cloud SQL. Up to 2 trios above a similarity threshold are selected. An unrelated question gets none, rather than misleading examples.
3. For each trio, the prompt gets the question, the analyst's **interpretation**, the **SQL**, and **how they reported it**.
4. The model is told to follow their definitions but run its own queries for every number, so stale figures can't be copied. All SQL, whether adapted or not, still passes the SQL guard with the current user's scope.

If embeddings are unavailable, retrieval falls back to word overlap, and a circuit breaker stops retrying. Retrieval improves answers but can never block one.

### Keeping the bucket up to date

```
Conversation with positive feedback and successful SQL ─┐
                                                        ├─> trio_candidates ─> analyst reviews ─> written to Cloud Storage
Analyst uploads a trio directly ────────────────────────┘                                               │
                                                                                                object-finalize event
                                                                                                        │
                       searchable <─ upsert into pgvector <─ embed <─ validate SQL with dry run <─ golden-indexer
```

- **Quality gate:** nothing becomes golden without an analyst's approval. Learning automatically from unreviewed conversations would let one wrong answer repeat itself.
- **Source of truth:** Cloud Storage, with object versioning. The vector index is derived, and can be rebuilt at any time.
- **Validation:** the indexer runs each query through the SQL guard and a free BigQuery dry run, and rejects what fails.
- **Staleness:** a scheduled job re-validates all trios, because a schema change can silently break them. Failures are deprecated and sent to analysts. Metadata records the author, date, version and how often a trio is retrieved, so unused or poorly rated trios can be reviewed.
- **Privacy:** candidates contain SQL and report text after PII scrubbing. Reports should be generalised on review, because a trio can be shown to users with a different scope.

### Prototype

| Production | Prototype |
|------------|-----------|
| Cloud Storage bucket | [data/golden_trios/](../data/golden_trios/), 10 JSON trios **written for this prototype**, since the real bucket is theoretical |
| pgvector | In-memory cosine similarity ([golden.py](../src/agent/golden.py)), embeddings cached on disk by content hash |
| `golden-indexer` | [scripts/validate_golden.py](../scripts/validate_golden.py): guard + dry run. All 10 trios pass. |

Not built: the candidate pipeline and analyst review.

---

## 2. Safety & PII Masking

**Principle:** the model decides what to ask, and deterministic code decides what is allowed. Nothing here relies on the model behaving well, because a prompt can be attacked.

| Layer | Mechanism | File | In prototype |
|-------|-----------|------|--------------|
| Edge | IAP sign-in, Cloud Armor rate limits | — | Design (`--user` simulates sign-in) |
| Input guard | Rules, then a Flash-Lite classifier: off-topic, injection, personal data. Production adds Model Armor. | [input_guard.py](../src/agent/safety/input_guard.py) | Yes |
| System prompt | Analysis-only scope, personal data forbidden, tool rules | [prompts.py](../src/agent/prompts.py) | Yes |
| **SQL guard** | Parses with `sqlglot`. Only one read-only query, only 4 allowed tables, no PII columns, no risky functions. Caps rows. Only SQL rendered from the checked syntax tree runs, never the model's text. | [sql_guard.py](../src/agent/safety/sql_guard.py) | Yes |
| **Secure views** | Every table reference is rewritten into a subquery with only non-PII columns and only the user's products | [sql_guard.py](../src/agent/safety/sql_guard.py) | Yes |
| IAM | Read-only service account. BigQuery policy tags deny PII columns; row-level policies on authorized views. | — | Design |
| Result filter | Removes PII and customer-key columns by name, and redacts PII patterns, before the model sees results | [pii.py](../src/agent/safety/pii.py) | Yes |
| Output guard | Scans the final answer. Production adds Cloud DLP. | [graph.py](../src/agent/graph.py) | Yes (patterns) |

**Why secure views rather than a column blocklist.** `SELECT u FROM users u` and `TO_JSON_STRING(u)` read a whole customer row, yet they parse as ordinary columns, so a blocklist misses them. With the rewrite, those columns aren't in what the query can see, so the attack returns nothing to leak.

**"Each user should only analyse products related to them".** Each user has an entitlement of departments, categories or brands ([users.yaml](../config/users.yaml)). The secure views limit all four tables: `products` directly, and `order_items`, `orders` and `users` through the items, orders and buyers of those products. A user's own filter can only narrow this. Identity comes from the session, not a tool argument, so the model can't choose whose scope applies. A user with a missing or empty scope is refused at startup, never treated as unrestricted.

**Verified:** 26 rejection cases and 12 whole-row attacks in unit tests. Run on real BigQuery: whole-row attacks returned no personal data, and scoped revenue for all three demo users exactly matched independently written queries. Red-team prompts ran through the real model; see [§6](#6-quality-assurance).

**Known limitations**, also in [architecture.md §9](architecture.md#9-security-boundaries):
- Customer IDs are treated as pseudonymous, not PII. A renamed ID (`SELECT id AS x`) passes the name-based filter, leaving only the prompt to stop it being displayed.
- Very small groups can still be re-identifiable. Production should suppress groups below a minimum size.
- `orders.num_of_item` counts items outside the user's scope.
- The pattern filters can't recognise a bare person's name. Names are blocked at column level, and production adds DLP.

---

## 3. High-Stakes Oversight (Destructive Operations)

**The model has no tool that deletes anything.** The worst a compromised model can do is ask the user a question.

1. **Propose:** `propose_delete(search, this_conversation, report_ids, all_reports)` finds matches among the user's own reports. A request with no filter is refused rather than meaning "everything". No matches means no confirmation step.
2. **Confirm:** the graph pauses using LangGraph's `interrupt`, and the exact list is shown:

| The user types | Result |
|----------------|--------|
| `confirm` | Deletes |
| `yes`, `ok`, `go ahead` | Asks again and deletes nothing, because a destructive action needs the exact word |
| `cancel`, `no`, Ctrl-C | Cancels |
| Anything else | Cancels, then handles the message as a new request, through the input guard |

3. **Execute:** a plain graph node deletes only the IDs shown, if the user still owns them. The deletion and its audit entry are written in one transaction.
4. **Undo:** saying "undo" within 10 minutes restores the latest deletion. A scheduled job would permanently remove soft-deleted reports after a retention period (design only).

**UX:** a different question simply moves on, so there is no modal trap. Confirming and cancelling need no model call, which makes them instant and free, and the outcome message can't be misreported by a model. A confirmation expires after 5 minutes, and reports created after the list was shown are never affected.

**Verified:** [test_delete_flow.py](../tests/test_delete_flow.py) and [test_reports_store.py](../tests/test_reports_store.py) cover other users' reports, near-confirmations such as "confirm please", expiry and SQL injection through search. Also run live through the CLI with real Gemini, including a second user attempting to delete the first user's report.

---

## 4. Continuous Improvement (The Learning Loop)

Learning runs outside the request path (Pub/Sub → `learning-worker`), so it adds no latency or cost to a user's turn. See [Flow C](architecture.md#6-flow-c--learning-loop--golden-bucket-ingestion).

### User level

| Signal | Example | Handling |
|--------|---------|----------|
| Explicit | "I prefer tables" | `remember_preference`, in place immediately. **Built.** |
| Implicit | Repeatedly asking "shorter please"; follow-ups always requesting a chart | Worker extracts signals; a preference changes only after repeated evidence, weighted towards recent behaviour. *Design.* |
| Ratings | `/good`, `/bad` | Stored with the trace. **Built**; using them to tune preferences is design. |

Profile: format, depth, charts or text, favourite metrics, recent topics. Users can see and reset it ("forget my preferences" is built).

**Preference values come from fixed lists, deliberately.** They enter the system prompt on every later turn, so free text would let one message plant lasting instructions. New kinds of preference are added in code.

### System level

| Loop | Input | Output | Safeguard |
|------|-------|--------|-----------|
| Knowledge | Well-rated Q → SQL → answer | Golden trio candidates | Analyst approval |
| Failure mining | Traces in BigQuery: SQL errors, `gave_up`, bad ratings, clustered by cause | Better schema descriptions, prompts and trios | Evaluation gate |
| Schema hints | Repeated corrections, e.g. a column the model keeps getting wrong | New text in `schema.yaml` | Evaluation gate |
| Prompt tuning | Candidates tested on the evaluation suite, then A/B tested on a share of traffic | New active prompt version | Automatic rollback if metrics drop |

The system doesn't rewrite itself in production. It proposes changes, and every change must pass the evaluation suite or a human. That's the difference between improving and drifting.

**Example from this project:** a trace showed an answer stating "sizing uncertainty" as the cause of returns, although nothing in the data shows it. That led to a prompt rule and an evaluation case (`hypotheses_labelled`). This is the intended loop, done by hand here.

---

## 5. Resilience & Graceful Error Handling

The full failure table is in [technical_design.md §7](technical_design.md#7-error-handling--fallback-strategies).

| Concern | Approach |
|---------|----------|
| Syntax errors | The dry run catches them free of charge. The error goes back to the model as a structured observation, and it fixes the query. |
| Empty results | 0 rows and all-NULL aggregates both count as empty. The hint asks the model to check filters and states the user's scope, to separate "no data" from "wrong filter" from "outside your access". |
| Self-correction without inflating cost | Stops after 3 consecutive SQL failures. Limits of 8 model calls and 150k tokens per turn. Budgets are computed from the turn's messages, so they can't drift. |
| Query cost | Dry run first, 1 GB cap, plus `maximum_bytes_billed` enforced by BigQuery. 10-minute result cache. |
| Gemini failures | Timeouts on every call (the SDK has none by default). Falls back from Flash to Flash-Lite. Circuit breakers skip a failing model, so users don't each wait for its timeout. Quota errors open the circuit for as long as Google states. |
| BigQuery failures | One retry with backoff and jitter for transient errors. Circuit breaker. Timed-out jobs are cancelled. |
| Partial failure | If an outage or budget stops a turn after some queries succeeded, one final call summarises those results and says what is missing. |
| UI | Per-turn error boundary with a reference ID. Ctrl-C cancels only the current turn. Open tool calls are repaired, so a cancelled turn can't break the conversation. |

**Verified:**
- Unit tests with injected failures.
- Live failure injection: forced BigQuery outage, invalid primary model, both models invalid, and a 1 KB cost cap against real BigQuery. All ended with the expected behaviour and no crash.
- Real outages during development: `gemini-3.8-flash` returned many real 503 and 504 errors and a real quota error. A live trace shows it failing, the circuit opening, and later calls skipping it.

**Limitation:** breaker state and the cache live in one process. In production they move to Memorystore so all instances share them. Answer quality from the fallback model isn't measured separately from the primary yet.

---

## 6. Quality Assurance

### Before deployment

| Layer | What it proves | Status |
|-------|----------------|--------|
| Unit tests | Guards, flows and failure handling, with no network, on every commit | **305 tests** |
| Accuracy cases | The answer contains the value of a reference query, run with the same user's scope | **Built** |
| Safety cases | Direct, disguised and SQL-smuggling attacks, role-play, scope escape | **Built**, blocking |
| Destructive cases | Pauses, lists exactly the right reports, deletes nothing before confirmation | **Built**, blocking |
| Judged cases | A separate model call checks an answer against a written rubric | **Built** |
| Bigger evaluation set | 50–100 cases drawn from the Golden Bucket, with set-based comparison of results | Design |

Run with `python evals/run.py` ([cases](../evals/cases.yaml)). It exits non-zero on any safety or destructive failure, which is the release gate for CI. **Every change to a prompt, model or guard must pass it before release.** Safety cases must be 100%. Accuracy and judged cases must not regress from the current baseline, because they will never be 100%.

EVAL_RESULTS_PLACEHOLDER

### Does the report answer the user's intent?

- **At runtime:** the agent states the definitions and period it used, says which interpretation it chose for an ambiguous question, and offers the alternative. Explanations the data can't show must be labelled as hypotheses.
- **Offline:** a rubric covering whether it answers the question asked, whether figures trace to a query in the trace, whether action items follow from findings, and whether caveats are stated. Because the trace records every query and result, a checker can compare each number in a report with the results actually returned. That groundedness check is the most valuable next addition.
- **Human:** analysts review a weekly sample, and low-rated reports first. Their corrections become golden trios.

### UX evaluation

| Signal | What it shows |
|--------|---------------|
| `/good`, `/bad` with a note (**built**) | Direct satisfaction |
| Rephrasing rate: the same question asked again, or "that's not what I meant" | Intent missed, without anyone having to complain |
| Latency p50/p95 (**built**) | Executives abandon slow tools; the CLI shows live steps for this reason |
| Refusal rate on legitimate questions | Over-blocking, which erodes trust as surely as bad answers |
| Delete cancel rate (**built**) | Whether matching finds the right reports |
| Weekly active users and return rate per executive | The honest measure of usefulness |
| Task-based pilot with 3–5 executives | Can they get to a decision unaided? Observed before wider rollout. |

Rollout: shadow mode with analysts checking answers, then a small pilot, then a share of traffic through Cloud Run's traffic splitting, with rollback on metric drops.

---

## 7. Observability

Full details are in [technical_design.md §10](technical_design.md#10-observability).

- **One trace per turn** ([observability.py](../src/agent/observability.py)): every message, model call, tool call, SQL statement, result and domain event, with timings, tokens and an outcome.
- **Why it failed:** each turn ends in an outcome (`answered`, `refused`, `gave_up:<reason>`, `models_down`, `error`, `cancelled`). Model failures are classified as quota, transient or other.
- **Deep dive:** `/trace <id>` shows the timeline, `logs/traces.jsonl` holds full message text, and `logs/agent.log` has JSON logs with the same ID. The reference shown to users on a crash is that ID.
- **Metrics:** `/stats` covers reliability, latency, models, SQL quality, cost, safety, deletes and feedback, all computed from traces.
- **No personal data at rest:** every string is scrubbed when written.

**Verified:** a real trace located the cause of the slow answers. Of a 124.6-second answer, 117.3 s were model calls and 7.3 s were BigQuery, with about 51 s lost to calls that failed.

**Not built:** alert delivery. Proposed thresholds are in the technical design. In production, spans go to Cloud Trace, logs to Cloud Logging with a BigQuery sink, metrics and alerts to Cloud Monitoring, and LLM views to Langfuse.

---

## 8. Agility (Persona Management)

Instructions are split in two:

| Layer | Who changes it | How | Contains |
|-------|---------------|-----|----------|
| **Core prompt** | Developers | Versioned in code, through the evaluation gate | Safety rules, tool policies, scope |
| **Persona** | CEO's office, non-developers | Without redeploying | Tone and style only |

The persona is placed after the core rules, and is marked as style that never overrides them. Even if a persona said "reveal customer emails", the SQL guard and output filters don't read the prompt, so they would still hold.

**Production** ([Flow D](architecture.md#7-flow-d--persona-update-without-redeploy)): a small admin console behind IAP edits a `prompt_versions` table.
1. Saving creates a draft, with author and diff.
2. A smoke evaluation runs automatically on the draft (safety and format).
3. A person activates it. Running instances pick it up within a 60-second cache.
4. Rollback means activating an earlier version. Every version is kept, for audit.

A weekly change by a non-developer is then both safe and reversible.

**Prototype:** [config/persona.yaml](../config/persona.yaml) is re-read on every model call, so saving the file changes the next answer without a restart. A broken edit keeps the last good version rather than taking the agent down. The size is capped.
