# TachyonIQ vs Databricks Genie — Competitive Gap Analysis

> Historical analysis: superseded by [the 12 September 2026 target design](TACHYONIQ_TARGET_DESIGN.md). Claims below about competitor internals, model providers, architectural superiority and production readiness are not verified and must not be used as current product claims.

> Audit date: 2026-09-05  
> Reference: Databricks Genie (Lakehouse IQ / AI/BI Genie)  
> Goal: Identify where TachyonIQ should match, exceed, or strategically differ from Genie

---

## Executive Summary

Databricks Genie is the gold standard for enterprise conversational analytics on a lakehouse. It wins on deep Unity Catalog integration, native governance, and the Databricks ecosystem. TachyonIQ's strategic advantage is **database-agnostic connectivity** — Genie only works on Databricks; TachyonIQ works on any database. The gaps below identify where TachyonIQ must close the experience gap to be a credible alternative.

---

## Feature Comparison Matrix

| Capability | Databricks Genie | UADA (current) | TachyonIQ (target) | Priority |
|---|---|---|---|---|
| **Core NL → SQL** | ✅ Production | ✅ Production | ✅ Keep & improve | — |
| **Multi-turn conversation** | ✅ Full CoE-SQL | ✅ CoE-SQL (6 turns) | ✅ Extend to 20+ turns | P2 |
| **Database connectivity** | ❌ Databricks only | ⚠️ Single DB (config) | ✅ Any DB, runtime | **P0** |
| **Automatic schema understanding** | ✅ Unity Catalog | ✅ SCL + reflection | ✅ + profiling | P1 |
| **Semantic layer** | ✅ Unity Catalog metrics | ✅ SCL (YAML) | ✅ SCL + auto-gen | P1 |
| **SQL validation / safety** | ✅ Databricks ACL | ✅ 6-rule AST validator | ✅ Keep (best-in-class) | — |
| **Result visualization** | ✅ 15+ chart types | ⚠️ 8 types | ✅ 20+ types | P1 |
| **KPI cards** | ✅ Native | ❌ No_CHART fallback | ✅ Add | **P1** |
| **Follow-up suggestions** | ✅ Contextual chips | ❌ None | ✅ Add | **P1** |
| **Insight narration** | ✅ GPT-4 powered | ⚠️ Deterministic templates | ✅ LLM + deterministic | P2 |
| **Drivers / anomalies** | ✅ Auto-detected | ❌ Not implemented | ✅ Add in analyser | P2 |
| **Streaming UI updates** | ✅ Incremental | ❌ Synchronous only | ✅ SSE streaming | P2 |
| **Data profiling** | ✅ Unity Catalog stats | ❌ Not implemented | ✅ Add DataProfiler | **P1** |
| **Schema search / RAG** | ✅ Unity Catalog search | ✅ Hybrid (ChromaDB+BM25) | ✅ Keep (strong) | — |
| **Saved analyses** | ✅ Genie Spaces | ❌ None | ✅ Add | P2 |
| **Sharing / collaboration** | ✅ Databricks workspace | ❌ None | ✅ Add permalinks | P2 |
| **RBAC / governance** | ✅ Unity Catalog | ❌ Single api_key | ✅ Add RBAC layer | P2 |
| **Audit logging** | ✅ Native | ❌ None | ✅ Add audit trail | P2 |
| **Forecasting** | ✅ Databricks ML | ❌ Not implemented | ✅ Add module | P2 |
| **Snowflake support** | ❌ No | ❌ No | ✅ Add adapter | **P1** |
| **BigQuery support** | ❌ No | ❌ No | ✅ Add adapter | **P1** |
| **Redshift support** | ❌ No | ❌ No | ✅ Add adapter | **P1** |
| **PostgreSQL support** | ❌ No | ✅ Production | ✅ Keep | — |
| **MySQL / SQLite support** | ❌ No | ✅ Production | ✅ Keep | — |
| **Multi-DB in one session** | ❌ No | ❌ No | ✅ Add | P2 |
| **Connection setup UI** | ✅ Databricks UI | ❌ Only via .env | ✅ Add wizard | **P0** |
| **Observability / tracing** | ✅ Databricks MLflow | ✅ OTel + Langfuse | ✅ Keep + enhance | — |
| **Self-hosted / on-premise** | ❌ Cloud only | ✅ Yes | ✅ Keep advantage | — |
| **Open-source / BSL** | ❌ Proprietary | ✅ Open | ✅ Keep advantage | — |
| **Custom LLM / local model** | ❌ OpenAI/Azure only | ✅ Ollama + any | ✅ Keep advantage | — |
| **Production frontend** | ✅ Full UI | ❌ Minimal HTML | ✅ Full UX redesign | **P0** |
| **Multi-viz per answer** | ✅ Dashboard mode | ❌ Single chart | ✅ Add | P2 |

---

## Where TachyonIQ Already Exceeds Genie

### 1. Database Agnosticism
Genie is hardcoded to Databricks. TachyonIQ (when GAP-01 and GAP-10 are closed) works on any SQLAlchemy-supported database including PostgreSQL, MySQL, Snowflake, BigQuery, Redshift, Oracle, and SQLite. This is the **primary differentiation**.

### 2. Hybrid Schema Retrieval
Genie uses Unity Catalog as its only retrieval backend. UADA's hybrid ChromaDB + BM25 + RRF with per-doctype budgets is architecturally superior for databases without a dedicated metadata catalog. The per-doctype budget prevents examples from starving schema context — a design issue found empirically during UADA development.

### 3. Self-Hosted / Air-Gapped Support
Genie requires Databricks Cloud. UADA/TachyonIQ can run fully on-premise with Ollama (local LLM), local ChromaDB, and local databases. This is critical for regulated industries (finance, healthcare, government).

### 4. Custom / Local LLMs
Genie uses only OpenAI or Azure OpenAI. UADA supports any PydanticAI-compatible model: Ollama (Qwen, Llama, Mistral), Anthropic, OpenAI, or custom HTTP endpoints.

### 5. SQL Security Model
UADA's 6-layer AST-based validator (via SQLGlot) provides transparency and auditability that Genie's ACL system does not expose to developers. The CRITICAL INVARIANT (SQLValidator inside retry loop) means even repaired SQL is validated — this is better than most industry implementations where repair bypasses validation.

### 6. Open-Source / Extensibility
Genie is proprietary. UADA is open and extensible. Customers can fork, customize, and self-host.

---

## Where Genie Leads (Gaps to Close)

### Critical Gaps (P0)

**Frontend UX (GAP-12)**
Genie has a polished AI/BI interface with a chat sidebar, inline charts, metric tiles, and a sharing surface. UADA's current UI is a developer test page. This is the #1 perception gap. A user evaluating TachyonIQ vs Genie will form their opinion from the UI first.

**Multi-Database Connection Runtime (GAP-01)**  
Genie surfaces all Unity Catalog databases in a picker. UADA requires .env editing. The UI connection wizard (GAP-02) is the interface-side equivalent.

### High-Value Gaps (P1)

**KPI Cards (GAP-07)**  
Genie renders single-value metrics with big numbers, trend arrows, and period comparisons. UADA returns `NO_CHART`. For executive-facing questions ("What was ARR last quarter?"), this is the most visible capability gap.

**Follow-Up Question Suggestions (GAP-04)**  
Genie surfaces 3–5 contextual follow-up chips after every answer. This is the primary mechanism for non-analyst users to continue exploring. Without it, conversation stalls.

**Data Profiling (GAP-03)**  
Genie leverages Unity Catalog statistics (built into Databricks). UADA's auto-generated semantic layer has no column statistics. This affects filter suggestion quality, join cardinality warnings, and chart axis selection.

**Cloud Warehouse Adapters (GAP-10)**  
Snowflake and BigQuery are the two most common enterprise analytical databases. Genie doesn't support them (Databricks-only). TachyonIQ can turn this into an advantage by supporting both — but the adapters need to be built first.

### Feature Depth Gaps (P2)

**Insight Narration Depth (GAP-14)**  
Genie uses GPT-4 to generate rich insight narratives with business context, driver attribution, and anomaly explanation. UADA uses deterministic templates ("Revenue grew 12%"). This gap matters most for executive presentations.

**Streaming UI (GAP-11)**  
Genie shows incremental progress (schema linking → SQL generation → execution → results). UADA is synchronous — the screen is blank for the full duration of a slow query. Streaming is a UX necessity for queries exceeding 3 seconds.

**Saved Analyses / Spaces (GAP-17)**  
Genie "Spaces" let users save, organize, and share analyses. TachyonIQ needs at minimum a query history and a save-to-bookmark feature.

**RBAC / Governance (GAP-16)**  
Genie inherits Unity Catalog governance (row-level security, column masking, user-level access). TachyonIQ needs a lightweight RBAC layer that maps users to permitted connection sets and respects database-level permissions.

---

## TachyonIQ Differentiation Strategy

### "Works Everywhere" Positioning
Position TachyonIQ against Genie's lock-in. Target customers on:
- Snowflake (no Genie)
- BigQuery (no Genie)
- Redshift (no Genie)
- PostgreSQL (no Genie)
- Multi-cloud or hybrid database estates
- Air-gapped / regulated environments

### "Open Architecture" Positioning
- Any LLM (local or cloud)
- Any database (via SQLAlchemy)
- Self-hosted
- Extensible semantic layer (YAML-based SCL vs proprietary catalog)

### Parity Threshold
For TachyonIQ to be credibly compared to Genie, it must close these gaps first:
1. Production frontend (3-panel layout)
2. Multi-DB connection wizard
3. KPI cards
4. Follow-up suggestions
5. 15+ chart types
6. Snowflake + BigQuery adapters

These 6 items constitute the **TachyonIQ MVP** from a competitive positioning standpoint.

---

## Implementation Sequence (Genie-Parity Roadmap)

| Milestone | Deliverable | Genie Feature Matched |
|---|---|---|
| M1 (P0 Sprint) | Production frontend (3-panel) | Genie UI |
| M1 (P0 Sprint) | Multi-DB connection wizard | Unity Catalog DB picker |
| M1 (P0 Sprint) | KPI card visualization | Metric tiles |
| M2 (P1 Sprint) | Follow-up suggestions | Contextual chips |
| M2 (P1 Sprint) | Data profiler | Unity Catalog statistics |
| M2 (P1 Sprint) | Snowflake adapter | Snowflake (TachyonIQ advantage) |
| M2 (P1 Sprint) | BigQuery adapter | BigQuery (TachyonIQ advantage) |
| M2 (P1 Sprint) | 8 additional chart types | Genie chart library |
| M3 (P2 Sprint) | Streaming SSE | Incremental UI |
| M3 (P2 Sprint) | Redis conversation store | Session persistence |
| M3 (P2 Sprint) | Insight narration depth | GPT-4 narratives |
| M3 (P2 Sprint) | Saved analyses | Genie Spaces |
| M4 (P2 Sprint) | RBAC layer | Unity Catalog governance |
| M4 (P2 Sprint) | Audit logging | Databricks audit |
| M4 (P2 Sprint) | Advanced analytics (forecast) | Databricks ML |

---

## Appendix: Genie Capabilities Not Targeted

The following Genie capabilities are deliberately out of scope for TachyonIQ because they require deep Databricks ecosystem integration and would not be achievable in a database-agnostic system:

- **Delta Lake time-travel** — Genie can query historical snapshots via Delta. TachyonIQ has no equivalent (database-dependent).
- **Databricks Workflows integration** — triggering jobs from chat. Out of scope.
- **Unity Catalog column-level lineage** — TachyonIQ targets column metadata, not lineage tracking.
- **Photon query acceleration** — Databricks runtime feature. TachyonIQ uses database-native execution.
- **Auto-ML via chat** — Genie can trigger AutoML experiments. TachyonIQ's advanced analytics are client-side Pandas/DuckDB only.

These are listed to explicitly **not** try to replicate them, avoiding scope creep. TachyonIQ's value proposition is breadth (any database) not depth in one ecosystem.
