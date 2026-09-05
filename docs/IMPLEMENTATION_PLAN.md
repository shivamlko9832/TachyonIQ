# TachyonIQ — Implementation Plan

> Based on: CURRENT_GAPS.md + TACHYONIQ_ARCHITECTURE.md  
> Principle: Every change is additive or a targeted refactor. No blind rewrites.  
> Security invariant: SQLValidator inside retry loop is NEVER moved.

---

## P0 Sprint — "Demo-able" (Do First)

These 5 items unlock the ability to show TachyonIQ to anyone.

### P0-1: DatabaseConnectionManager + REST API
**Files**: `uada/db/connection_manager.py` (new), `uada/api/routes/connections.py` (new), `uada/api/app.py` (refactor)  
**Why first**: Everything else depends on dynamic DB connections.  
**Test**: `POST /connections` with demo SQLite → `GET /connections` shows it → `POST /query` with `connection_id` works.

### P0-2: Connection Setup Wizard (Frontend)
**Files**: `uada/api/routes/ui.py` (full replace)  
**Why**: The connection wizard is the entry point of the product experience.  
**Deliverable**: 3-panel HTML page with left sidebar (DB nav), main chat, right SQL panel, connection modal.

### P0-3: KPI Card Visualization
**Files**: `uada/pipeline/viz_generator.py` (refactor), `uada/models/result.py` (add KPI type)  
**Why**: Most common executive question ("What was revenue last quarter?") currently returns NO_CHART.  
**Change**: Add `KPI` to ChartType enum; add `KpiCard` spec model; update decision tree.

### P0-4: Follow-Up Question Engine
**Files**: `uada/pipeline/followup_engine.py` (new), `uada/models/result.py` (add field), `uada/pipeline/orchestrator.py` (wire in)  
**Why**: Without suggested questions, conversation ends after each answer.  
**Deliverable**: Deterministic rules → 3–5 chips per answer.

### P0-5: UADAResponse.suggested_questions
**Files**: `uada/models/result.py` (add field), `uada/api/routes/query.py` (pass through)  
**Why**: Frontend needs the field to render chips.

---

## P1 Sprint — "Production Core"

### P1-1: DataProfiler
`uada/pipeline/data_profiler.py` (new) + `uada/scl/reflector.py` (integrate)  
null%, distinct count, min/max, sample values, temporal range.

### P1-2: Redis ConversationStore
`uada/pipeline/conversation_store.py` (refactor) + `uada/config.py` (add redis_url)  
Keep in-memory as fallback when redis_url is None.

### P1-3: Snowflake Adapter
`uada/db/adapters/snowflake.py` (new) + `uada/db/adapter_registry.py` (new)  
`snowflake-sqlalchemy` driver. Warehouse, role, schema params.

### P1-4: BigQuery Adapter
`uada/db/adapters/bigquery.py` (new)  
`sqlalchemy-bigquery`. Project, dataset, OAuth/service account.

### P1-5: Redshift Adapter
`uada/db/adapters/redshift.py` (new)  
`sqlalchemy-redshift` + psycopg2. statement_timeout.

### P1-6: 8 Additional Chart Types
`uada/pipeline/viz_generator.py` (extend)  
Grouped bar, stacked bar, histogram, waterfall, funnel, treemap, table fallback, enhanced pie.

### P1-7: QueryPlanner Dialect Extensions
`uada/pipeline/query_planner.py` (extend)  
DuckDB, Snowflake, BigQuery time functions + bucket expressions.

---

## P2 Sprint — "Feature Depth"

### P2-1: SSE Streaming
`uada/api/routes/query.py` (add GET /query/stream)  
Stage-by-stage EventSourceResponse.

### P2-2: Insight Narration Depth
`uada/pipeline/result_analyser.py` (extend)  
Key Findings, Drivers, Anomalies (LLM-powered with deterministic fallback).

### P2-3: Multi-Visualization Response
`uada/models/result.py` (add supplementary_visualisations)  
`uada/pipeline/viz_generator.py` (add MultiVizPlanner)

### P2-4: DuckDB in ResultAnalyser
`uada/pipeline/result_analyser.py` (extend)  
In-process OLAP on result sets. Correlation, percentile, window functions.

### P2-5: Schema Refresh API
`uada/api/routes/connections.py` (add POST /connections/{id}/refresh)  
Incremental ChromaDB upsert on fingerprint diff.

### P2-6: RBAC Layer
`uada/api/middleware/auth.py` (extend)  
JWT → user_id + role → ConnectionPolicy.

### P2-7: Audit Logging
`uada/observability/audit.py` (new)  
Structured JSON to file/DB: user_id, question, sql_hash, tables, timestamp.

---

## P3 Sprint — "Advanced Analytics"

### P3-1: Correlation Analysis Module
`uada/analytics/correlation.py` (new)

### P3-2: Anomaly Detection (Isolation Forest)
`uada/analytics/anomaly.py` (new) using scikit-learn

### P3-3: Forecasting Module
`uada/analytics/forecast.py` (new) using Prophet / statsmodels

### P3-4: Saved Analyses
`uada/api/routes/analyses.py` (new) + persistence layer

---

## Implementation Order Within P0

```
P0-5: UADAResponse.suggested_questions field (model change — no deps)
  ↓
P0-3: KPI card (viz change — depends only on models)
  ↓
P0-4: FollowUpEngine (depends on models + intent + result types)
  ↓
P0-1: DatabaseConnectionManager (independent new module)
  ↓
P0-1b: Connection REST routes + app.py wiring
  ↓
P0-2: Production frontend (depends on all above for correct API)
```

---

## Files Changed by Priority

| Priority | File | Change Type |
|---|---|---|
| P0 | `uada/models/result.py` | REFACTOR: add suggested_questions, KpiCard |
| P0 | `uada/pipeline/viz_generator.py` | REFACTOR: KPI card + decision tree |
| P0 | `uada/pipeline/followup_engine.py` | ADD |
| P0 | `uada/pipeline/orchestrator.py` | REFACTOR: wire followup_engine |
| P0 | `uada/db/connection_manager.py` | ADD |
| P0 | `uada/api/routes/connections.py` | ADD |
| P0 | `uada/api/app.py` | REFACTOR: mount connection routes |
| P0 | `uada/api/routes/ui.py` | REPLACE: full 3-panel frontend |
| P1 | `uada/pipeline/data_profiler.py` | ADD |
| P1 | `uada/scl/reflector.py` | REFACTOR: integrate profiler |
| P1 | `uada/pipeline/conversation_store.py` | REFACTOR: Redis backend |
| P1 | `uada/config.py` | REFACTOR: redis_url, multi-connection |
| P1 | `uada/db/adapters/snowflake.py` | ADD |
| P1 | `uada/db/adapters/bigquery.py` | ADD |
| P1 | `uada/db/adapters/redshift.py` | ADD |
| P1 | `uada/db/adapter_registry.py` | ADD |
| P1 | `uada/pipeline/viz_generator.py` | REFACTOR: 8 more chart types |
| P1 | `uada/pipeline/query_planner.py` | REFACTOR: DuckDB/Snowflake/BQ dialects |
