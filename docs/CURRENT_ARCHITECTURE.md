# UADA — Current Architecture

> Audit date: 2026-09-05  
> Auditor: TachyonIQ transformation analysis  
> Status: All 15 implementation phases confirmed complete

---

## Executive Summary

UADA (Unified Analytics Data Agent) is a production-quality conversational analytics system built on PydanticAI, SQLAlchemy, and a hybrid retrieval stack. The codebase is more complete than the project brief implies — all core pipeline stages from question intake through visualization are implemented and tested. The gap between UADA and the TachyonIQ vision is **capability expansion**, not rebuilding.

---

## Technology Stack

| Layer | Technology | Version / Notes |
|---|---|---|
| Agent orchestration | PydanticAI | 2.0+, `output_type=BaseModel`, auto-retry |
| SQL parsing / security | SQLGlot | AST-based, dialect-agnostic |
| Database connectivity | SQLAlchemy Core | 2.0, read-only, connection pooling |
| Vector store | ChromaDB | Local persistent, no built-in embedder |
| Lexical retrieval | rank-bm25 | BM25Okapi, regex tokenizer |
| Result merging | Reciprocal Rank Fusion | k=60, per-doctype budget |
| Embeddings | sentence-transformers | BAAI/bge-small-en-v1.5 |
| Visualization | Altair / Vega-Lite | Deterministic spec generation |
| Data analysis | Pandas + NumPy | Statistical, trend, outlier |
| In-process analytics | DuckDB | Dependency present, not yet wired |
| Observability | OpenTelemetry + Langfuse | Logfire bridge for PydanticAI |
| API | FastAPI + Uvicorn | Async, middleware auth |
| Configuration | Pydantic Settings | `UADA_` prefix, .env support |

---

## Component Map

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER                             │
│  uada/api/routes/ui.py          uada/api/routes/query.py        │
│  [REPLACE: minimal HTML]        [KEEP: POST /query endpoint]    │
└──────────────────────┬──────────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────────┐
│                     API LAYER                                   │
│  uada/api/app.py                uada/api/middleware/auth.py      │
│  [REFACTOR: add conn mgmt]      [KEEP: Bearer token auth]        │
└──────────────────────┬──────────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────────┐
│                  ORCHESTRATION LAYER                            │
│  uada/pipeline/orchestrator.py                                  │
│  [KEEP: security-correct pipeline wiring]                       │
│                                                                 │
│  Run loop:                                                      │
│  1. SchemaLinker.link()    → SchemaContext                      │
│  2. IntentExtractor.run()  → AnalyticalIntent                   │
│  3. QueryPlanner.plan()    → QueryPlan                          │
│  4. SQLGenerator.generate()→ SQL string                         │
│  5. SQLValidator.validate()→ ValidationResult (INSIDE loop)     │
│  6. DatabaseAdapter.execute_query() → QueryExecutionResult      │
│  7. ResultAnalyser.analyse()→ AnalysedResult                    │
│  8. VizGenerator.generate()→ VegaLiteSpec | VisualisationFallback│
│  9. ConversationStore.save()                                    │
└──────────────────────┬──────────────────────────────────────────┘
                       │
     ┌─────────────────┼─────────────────────────────┐
     │                 │                             │
┌────▼───────┐  ┌──────▼──────┐  ┌──────────────────▼──────────┐
│  RETRIEVAL │  │  SEMANTIC   │  │     DATABASE LAYER          │
│  LAYER     │  │  LAYER      │  │                             │
│            │  │             │  │  uada/db/interface.py       │
│ hybrid.py  │  │ scl/        │  │  [KEEP: clean ABC]          │
│ [KEEP: RRF]│  │ schema.py   │  │                             │
│            │  │ loader.py   │  │  uada/db/adapter.py         │
│ chroma_    │  │ manager.py  │  │  [KEEP+EXTEND: SQLAlchemy]  │
│ backend.py │  │ reflector.py│  │                             │
│ [KEEP]     │  │ [KEEP all]  │  │  Dialects: postgresql,      │
│            │  │             │  │  mysql, sqlite, mssql       │
│ bm25.py    │  │             │  │                             │
│ embedder.py│  │             │  │  [ADD: Snowflake, BigQuery,  │
│ [KEEP both]│  │             │  │   Redshift, Oracle adapters] │
└────────────┘  └─────────────┘  └─────────────────────────────┘
```

---

## Pipeline Stage Detail

### Stage 0 — Session Intake
**File**: `uada/pipeline/orchestrator.py`  
**Classification**: KEEP  

Generates UUID session IDs, loads `ConversationState` from `ConversationStore`, builds the UADA system prompt with injected SCL context, manages the LLM retry loop. The CRITICAL SECURITY INVARIANT: `SQLValidator.validate()` runs inside the retry while-loop, so every repaired SQL is re-validated before execution. Security violations cause immediate pipeline halt with no retry.

---

### Stage 1 — Schema Linking (Retrieval)
**File**: `uada/pipeline/schema_linker.py`  
**Classification**: KEEP  

Hybrid retrieval with separate `top_k` budget per doctype (TABLE, METRIC, GLOSSARY, EXAMPLE). This prevents examples from starving tables in a shared budget — a design decision confirmed by empirical testing (revenue question failed to surface "orders" table in top-10 shared results). Security layer: every retrieved table re-checked against `SCLManager.get_allowed_tables()`.

**Retrieval Architecture**:
```
User Question
     │
     ├─→ Vector Query (ChromaDB) per doctype ──┐
     │                                          ├─→ RRF Merge → SchemaContext
     └─→ BM25 Query per doctype ───────────────┘
```

**DocType budgets** (configurable via `retrieval_top_k`, `retrieval_final_k`):
- TABLE: top_k=10, final_k=5
- METRIC: top_k=8, final_k=4  
- GLOSSARY: top_k=8, final_k=4
- EXAMPLE: top_k=5, final_k=3 (capped in `to_prompt_context()`)

---

### Stage 2 — Intent Extraction
**File**: `uada/pipeline/intent_extractor.py`  
**Classification**: KEEP  

PydanticAI agent with `output_type=AnalyticalIntent`. Extracts structured analytical intent from natural language. `defer_model_check=True` enables test/CI use with `TestModel`. `retries=settings.llm_max_retries` for auto-repair.

**AnalyticalIntent fields**:
- `question_type`: AGGREGATION / TIME_SERIES / COMPARISON / RANKING / FILTER / DIAGNOSTIC / FOLLOW_UP_*  / OUT_OF_SCOPE / AMBIGUOUS
- `measures`: list of requested metrics
- `dimensions`: grouping dimensions
- `filters`: SemanticFilter list (entity, operator, value)
- `time_range`: TimeRange (relative/absolute, bucket: HOUR/DAY/WEEK/MONTH/QUARTER/YEAR)
- `order_by`, `limit`, `references_prior_turn`: bool

**Cross-field validators** (Pydantic root validators):
- RANKING requires `order_by`
- TIME_SERIES requires `time_range` with `bucket`
- FOLLOW_UP_* requires `references_prior_turn=True`
- OUT_OF_SCOPE/AMBIGUOUS must not have `measures`

---

### Stage 3 — Query Planning
**File**: `uada/pipeline/query_planner.py`  
**Classification**: KEEP  

Pure deterministic Python — no LLM calls. Translates `AnalyticalIntent` → `QueryPlan` with fully resolved, dialect-aware SQL fragments.

**Dialect coverage**: PostgreSQL, MySQL, SQLite, T-SQL (4 of 7 needed)  
**Missing**: DuckDB, Snowflake, BigQuery dialects

**RelativePeriod coverage** (16 values × 4 dialects):
- TODAY, YESTERDAY, THIS_WEEK, LAST_WEEK, THIS_MONTH, LAST_MONTH, THIS_QUARTER, LAST_QUARTER, THIS_YEAR, LAST_YEAR, LAST_7_DAYS, LAST_30_DAYS, LAST_90_DAYS, LAST_12_MONTHS, LAST_N_DAYS, LAST_N_MONTHS

**Time bucket functions** (all alias as `period`):
- HOUR: `DATE_TRUNC('hour', col)` / SQLite `strftime('%Y-%m-%d %H', col)`
- DAY/WEEK/MONTH/QUARTER/YEAR: dialect-specific

---

### Stage 4 — SQL Generation
**File**: `uada/pipeline/sql_generator.py`  
**Classification**: KEEP  

PydanticAI agent, `output_type=str`. Receives the `QueryPlan.to_generator_context()` condensed dict for injection. `_strip_code_fences()` removes markdown formatting from LLM output. `repair()` method injects `error.repair_hint` for targeted retry.

---

### Stage 5 — SQL Validation (Security Boundary)
**File**: `uada/pipeline/sql_validator.py`  
**Classification**: KEEP — DO NOT MODIFY  

The security boundary. All 6 rules enforced via SQLGlot AST:

| Rule | Implementation |
|---|---|
| SELECT-only | 16 forbidden statement types in FORBIDDEN_STATEMENT_TYPES |
| Single statement | Reject multi-statement input |
| No system schemas | SYSTEM_SCHEMA_PREFIXES: information_schema, pg_catalog, sys, master, tempdb, etc. |
| Table allowlist | SCL-sourced whitelist (empty = allow all, used in tests only) |
| No dangerous functions | 30+ functions: xp_cmdshell, load_file, pg_sleep, exec, version, etc. |
| Subquery depth | Configurable via `sql_max_subquery_depth` (default 3) |
| Comment injection | Rejects `--` or `/*` in SQL |
| Empty query | Rejects blank/whitespace-only input |
| CTE awareness | CTE names excluded from allowlist check |
| LIMIT injection | Automatically injects LIMIT if absent (`sql_inject_limit=True`) |

---

### Stage 6 — Query Execution
**File**: `uada/db/adapter.py`  
**Classification**: KEEP, EXTEND  

SQLAlchemy Core 2.0 adapter. `fetchmany(max_rows+1)` for truncation detection. Write-probe in `test_connection()` warns (not rejects) on non-read-only accounts. SHA-256 schema fingerprint for cache invalidation.

**Timeout implementation**:
- PostgreSQL/MySQL/MSSQL: statement-level timeout (dialect-specific)
- SQLite: `set_progress_handler` polling loop

---

### Stage 7 — Result Analysis
**File**: `uada/pipeline/result_analyser.py`  
**Classification**: KEEP  

Pandas-based. Numeric column detection from `ColumnMeta.type` (not pandas dtype, to correctly handle empty results). Outlier: z-score > 3.0. Trend: >5% = INCREASING, <-5% = DECREASING, >15% of mean magnitude on consecutive steps = VOLATILE, else STABLE.

**Output**: `AnalysedResult` with:
- `numeric_summaries`: per-column {min, max, mean, median, std}
- `trend_analysis`: TrendDirection + explanation
- `outliers`: row indices + z-score
- `has_time_dimension`: bool
- `narrative_insight`: one-sentence NL summary
- `key_finding`: primary data point (headline metric)

---

### Stage 8 — Visualization Generation
**File**: `uada/pipeline/viz_generator.py`  
**Classification**: REFACTOR  

Deterministic selection + LLM fallback. Current chart types: BAR, LINE, AREA, POINT, ARC, RECT, BOXPLOT, RULE.

**Decision tree**:
```
row≤1 AND numeric≤1 AND no dimensions → NO_CHART
question_type==TIME_SERIES → LINE
question_type==RANKING AND row≤20 → BAR  
question_type==COMPARISON → BAR (grouped)
dimensions≥2 → BAR
else → LLM fallback (validates mark + column refs)
```

**Missing**: KPI cards, pie/donut, scatter, heatmap, waterfall, funnel, histogram, treemap

---

### Stage 9 — Conversation Management
**File**: `uada/pipeline/conversation_store.py`  
**Classification**: REFACTOR  

In-memory dict. `save()` compresses turns beyond `conversation_max_turns` (default 6) into `compressed_context` string. `get_edition_context()` builds token-efficient CoE-SQL summary for LLM injection (diff-based, not full history).

**Production gap**: Must replace in-memory dict with Redis or PostgreSQL for persistence across restarts and horizontal scaling.

---

## Semantic Layer Architecture

```
config/semantic_context.yaml (SCL YAML)
         │
         ├─→ SCLLoader (custom SafeLoader, fixes YAML 1.1 on/off→bool)
         │           │
         │           └─→ SemanticContextLayer (Pydantic root model)
         │                       │
         │           ┌───────────┴────────────┐
         │           │                        │
         │    SCLManager                SCLReflector
         │    (indexed view)            (DB → candidate SCL)
         │    - metric_index            - schema introspection
         │    - glossary_index          - FK → join inference
         │    - get_allowed_tables()    - null/distinct profiling (MISSING)
         │    - get_join_path()
         │    - to_indexable_documents()
```

---

## Data Models

| Model | File | Purpose |
|---|---|---|
| `AnalyticalIntent` | models/intent.py | NL → structured intent IR |
| `QueryPlan` | models/query_plan.py | Intent → dialect SQL blueprint |
| `QueryResult` | models/result.py | Raw execution output |
| `AnalysedResult` | models/result.py | Statistical analysis layer |
| `VegaLiteSpec` | models/result.py | Visualization specification |
| `UADAResponse` | models/result.py | Final API response |
| `ConversationTurn` | models/conversation.py | Single exchange record |
| `ConversationState` | models/conversation.py | Full session with active context |
| `SchemaContext` | models/schema_context.py | Retrieved semantic context for LLM |

---

## Security Architecture

```
5-Layer Defense Model:
────────────────────────────────────────────────────────────────
Layer 1: Credentials        Never in logs, prompts, or SQL
Layer 2: Schema Allowlist   SCL.security.excluded_tables enforced in SchemaLinker
Layer 3: AST Validation     SQLValidator (6 rules, pre-execution, inside retry loop)
Layer 4: Execution          read-only connection, query_timeout, max_rows
Layer 5: Prompt Injection   Comment stripping, SQL fence removal, intent validation
────────────────────────────────────────────────────────────────
```

---

## Component Classification Summary

| Component | File | Classification | Reason |
|---|---|---|---|
| SQL Validator | pipeline/sql_validator.py | **KEEP** | Production security boundary |
| Database Interface | db/interface.py | **KEEP** | Clean ABC, don't break contract |
| SQLAlchemy Adapter | db/adapter.py | **KEEP + EXTEND** | Solid, needs more dialect adapters |
| All models/ | models/*.py | **KEEP** | Fully specified Pydantic contracts |
| SCL stack | scl/*.py | **KEEP** | Well-designed, YAML validated |
| Retrieval stack | retrieval/*.py | **KEEP** | Solid hybrid retrieval |
| Orchestrator | pipeline/orchestrator.py | **KEEP** | Security-correct pipeline |
| Query Planner | pipeline/query_planner.py | **KEEP** | Dialect-aware, all periods |
| Intent Extractor | pipeline/intent_extractor.py | **KEEP** | Clean PydanticAI agent |
| SQL Generator | pipeline/sql_generator.py | **KEEP** | Repair loop works |
| Result Analyser | pipeline/result_analyser.py | **KEEP** | Solid Pandas implementation |
| Observability | observability/tracer.py | **KEEP** | OTel + Langfuse wired |
| Viz Generator | pipeline/viz_generator.py | **REFACTOR** | Add 12+ chart types |
| Conversation Store | pipeline/conversation_store.py | **REFACTOR** | In-memory → Redis/PostgreSQL |
| Config | config.py | **REFACTOR** | Multi-connection support |
| App factory | api/app.py | **REFACTOR** | Add connection mgmt endpoints |
| SCL Reflector | scl/reflector.py | **REFACTOR** | Add data profiling |
| Frontend UI | api/routes/ui.py | **REPLACE** | Minimal HTML → full UX |
| Query Route | api/routes/query.py | **KEEP** | Clean, add streaming |
| Auth Middleware | api/middleware/auth.py | **KEEP** | Extend for RBAC |
| Multi-DB Manager | — | **ADD** | Runtime connection registry |
| Connection UI | — | **ADD** | TEST/DISCOVER/PROFILE/READY |
| Data Profiler | — | **ADD** | null%, cardinality, samples |
| Follow-up Engine | — | **ADD** | Suggested next questions |
| KPI Cards | — | **ADD** | Single-value visualization |
| Multi-viz | — | **ADD** | Multiple charts per answer |
| Advanced Analytics | — | **ADD** | Forecast, correlation, cohort |
| Snowflake Adapter | — | **ADD** | dialect + connector |
| BigQuery Adapter | — | **ADD** | dialect + connector |
| Redshift Adapter | — | **ADD** | dialect + connector |
| Oracle Adapter | — | **ADD** | dialect + connector |
| Production Conv Store | — | **ADD** | Redis-backed |
| Streaming Responses | — | **ADD** | SSE / chunked |
| RBAC Layer | — | **ADD** | Role-based access |
| Saved Analyses | — | **ADD** | Persisted query history |
