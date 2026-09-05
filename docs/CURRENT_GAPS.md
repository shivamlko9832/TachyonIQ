# UADA → TachyonIQ Gap Analysis

> Audit date: 2026-09-05  
> Methodology: Full codebase read + TachyonIQ requirements review  
> Framing: Every gap is an ADD or REFACTOR — no removal needed

---

## Gap Severity Legend

| Level | Meaning |
|---|---|
| 🔴 P0 | Blocks the primary product experience |
| 🟠 P1 | Required for production deployment |
| 🟡 P2 | Significantly improves capability and UX |
| 🟢 P3 | Enhances competitiveness / long-term vision |

---

## GAP-01 — Multi-Database Connection Management
**Severity**: 🔴 P0  
**Current state**: Single database URL in `.env` (`UADA_DB_URL`). Hardcoded at startup. Connection cannot be changed without restart.  
**Required**: Runtime multi-connection registry. Users connect to any database from the UI. Connections persist across sessions. Multiple databases queryable per deployment.  

**Missing components**:
- `DatabaseConnectionManager` (runtime registry, keyed by connection_id)
- REST endpoints: `POST /connections`, `GET /connections`, `DELETE /connections/{id}`, `POST /connections/{id}/test`
- Connection persistence (JSON file or PostgreSQL table)
- Session → connection_id binding (which DB is this session querying?)

**Impact if absent**: TachyonIQ cannot be demonstrated as database-agnostic. Users are locked to the single configured DB.

---

## GAP-02 — Database Connection UI (TEST/DISCOVER/PROFILE/READY Flow)
**Severity**: 🔴 P0  
**Current state**: No connection UI. The frontend is a minimal single-page HTML form (question box + answer area). Credentials must be set in `.env` before startup.  
**Required**: Connection setup wizard with 4 states:
1. TEST — verifies connectivity, returns latency + server version
2. DISCOVER — schema reflection, table/column counts, relationship detection
3. PROFILE — null %, distinct count, sample values, PK/FK detection
4. READY — connection indexed in vector store, semantic catalog populated

**Missing components**:
- Frontend connection form (host, port, database, user, password, SSL, dialect selector)
- Progress state machine in UI
- Backend `POST /connections/test`, `POST /connections/discover`, `POST /connections/profile`
- Credential never stored in browser state or logs
- Masked display (show only host + database after connection)

**Impact if absent**: No way to connect a new database through the UI.

---

## GAP-03 — Data Profiling Engine
**Severity**: 🟠 P1  
**Current state**: `SCLReflector.from_adapter()` generates candidate SCL from schema introspection but computes no statistics. `null_percentage`, `distinct_count`, `sample_values`, `has_enum_pattern` are all absent.  
**Required**: On-demand and scheduled profiling that captures:
- Null % per column
- Distinct count per column
- Min / max / mean / median for numeric columns
- Most-frequent values (top 10) for low-cardinality columns
- Temporal range (min date, max date) for temporal columns
- Row count per table

**Missing components**:
- `DataProfiler` class wrapping adapter + pandas
- Batch-safe profiling (samples large tables, full scan for small)
- Profile storage (JSON or DB table, invalidated by schema fingerprint change)
- Integration into `SCLReflector` output + semantic catalog

**Impact if absent**: Auto-generated semantic layer has no statistical grounding. Filter suggestions and chart recommendations are based on column names alone.

---

## GAP-04 — Follow-Up Question Engine
**Severity**: 🟠 P1  
**Current state**: No follow-up question suggestions. The API response has no `suggested_questions` field. The UI has no suggestion chips.  
**Required**: After every successful analysis, generate 3–5 contextual follow-up questions. Questions must be:
- Grounded in the actual result data (not generic)
- Diverse in question type (drill-down, compare, trend, outlier, related entity)
- Actionable (answerable by the current database)

**Missing components**:
- `FollowUpEngine` — PydanticAI agent or deterministic generator
- `UADAResponse.suggested_questions: list[str]` field
- UI chip row below the answer panel
- Deterministic fallback rules (e.g., RANKING → "break down {top_entity} by time", AGGREGATION → "how does this compare to last period?")

**Impact if absent**: Conversation ends after each answer. Discovery is user-driven with no guidance, reducing value for non-analyst users.

---

## GAP-05 — Visualization Completeness (12 Missing Chart Types)
**Severity**: 🟠 P1  
**Current state**: 8 chart types in `ChartType` enum: BAR, LINE, AREA, POINT, ARC, RECT, BOXPLOT, RULE. Deterministic selection covers 5 cases; LLM fallback handles the rest.  
**Required TachyonIQ chart types**:

| Chart Type | Use Case | Status |
|---|---|---|
| BAR | Rankings, comparisons | ✅ Exists |
| LINE | Time series | ✅ Exists |
| AREA | Cumulative trends | ✅ Exists |
| SCATTER / POINT | Correlation | ✅ Exists (POINT) |
| ARC (pie/donut) | Proportions | ✅ Exists |
| HEATMAP (RECT) | Cross-dimensional | ✅ Exists |
| BOXPLOT | Distribution | ✅ Exists |
| RULE | Reference lines | ✅ Exists |
| **KPI CARD** | **Single headline metric** | ❌ Missing |
| **HISTOGRAM** | **Distribution of values** | ❌ Missing |
| **WATERFALL** | **Running total / bridge** | ❌ Missing |
| **FUNNEL** | **Conversion steps** | ❌ Missing |
| **TREEMAP** | **Hierarchical proportions** | ❌ Missing |
| **GROUPED BAR** | **Multi-series comparison** | ❌ Missing (LLM only) |
| **STACKED BAR** | **Composition over category** | ❌ Missing |
| **COMBO LINE+BAR** | **Dual-axis** | ❌ Missing |
| **TABLE** | **Raw data fallback** | ❌ Missing |
| **GAUGE** | **Progress to target** | ❌ Missing |
| **SANKEY** | **Flow between entities** | ❌ Missing |
| **MAP / CHOROPLETH** | **Geographic data** | ❌ Missing |

**Minimum viable**: KPI card, histogram, grouped bar, stacked bar, table fallback (5 additions, covers 95% of real-world cases).

---

## GAP-06 — Multi-Visualization Response
**Severity**: 🟡 P2  
**Current state**: One `VegaLiteSpec` per `UADAResponse`. Comparison questions ("revenue vs last quarter by region") produce one chart.  
**Required**: Complex analytical questions should produce 2–4 complementary visualizations:
- Primary: answers the question directly
- Secondary: trend, distribution, or breakdown
- Tertiary: data quality warning or anomaly highlight

**Missing components**:
- `UADAResponse.visualisations: list[VizItem]` (primary + supplementary)
- `MultiVizPlanner` — decides which combo is appropriate
- Frontend panel: tabbed or stacked multi-chart layout

---

## GAP-07 — KPI Cards / Single-Value Display
**Severity**: 🟠 P1  
**Current state**: When `row≤1 AND numeric≤1 AND no dimensions`, viz generator returns `NO_CHART`. The result is shown as a raw table or text.  
**Required**: KPI card component with:
- Large metric display (formatted number, currency, percentage)
- Comparison to prior period (if available in conversation context)
- Trend arrow (up/down/flat)
- Sparkline (if time data in context)

**Impact if absent**: "What was total revenue last quarter?" produces an unstyled number. Poor product experience for the most common executive question type.

---

## GAP-08 — Advanced Analytics Modules
**Severity**: 🟡 P2  
**Current state**: `ResultAnalyser` provides descriptive stats (min/max/mean/median/std), trend direction, and z-score outliers. No inferential or predictive analytics.  
**Required modules**:

| Module | Capability | Priority |
|---|---|---|
| Correlation Analysis | Pearson/Spearman matrix, VIF | P2 |
| Regression | Linear regression, R², residuals | P2 |
| Anomaly Detection | IQR + isolation forest | P1 |
| Forecasting | ARIMA / Prophet / exponential smoothing | P2 |
| Cohort Analysis | Retention matrix, LTV curves | P3 |
| Funnel Analysis | Step-by-step conversion + drop-off | P3 |
| A/B Test Analysis | t-test, Mann-Whitney, effect size | P3 |
| Segmentation | k-means clustering on result set | P3 |

**DuckDB gap**: DuckDB is in `pyproject.toml` but not wired into `result_analyser.py`. It enables in-process OLAP aggregations on large result sets without re-querying the source database.

---

## GAP-09 — Production Conversation Store
**Severity**: 🟠 P1  
**Current state**: `ConversationStore` uses an in-memory Python dict. Sessions are lost on restart. Does not support multiple API worker processes.  
**Required**: Redis-backed or PostgreSQL-backed conversation store with:
- TTL-based session expiry (configurable, e.g., 24 hours)
- Serializable `ConversationState` (already Pydantic, just needs `.model_dump_json()`)
- Connection pooling for store backend
- Optional: user_id → sessions index for "My conversations" feature

**Impact if absent**: Production deployment behind a load balancer or with any restart causes all active sessions to be lost. Unacceptable for a product claiming "conversational" analytics.

---

## GAP-10 — Snowflake / BigQuery / Redshift / Oracle Adapters
**Severity**: 🟠 P1  
**Current state**: 4 dialects supported: postgresql, mysql, sqlite, mssql. Cloud data warehouses are absent.  
**Required adapters**:

| Database | Priority | Notes |
|---|---|---|
| Snowflake | P1 | `snowflake-sqlalchemy`, warehouse/role params |
| BigQuery | P1 | `sqlalchemy-bigquery`, project/dataset params, OAuth |
| Redshift | P1 | `sqlalchemy-redshift`, `psycopg2` driver |
| Oracle | P2 | `cx-Oracle` or `oracledb`, service name |
| Databricks SQL | P2 | `databricks-sql-connector`, HTTP path |
| DuckDB | P1 | `duckdb-engine`, file or in-memory |
| ClickHouse | P3 | `clickhouse-sqlalchemy` |

**Also needed**: `QueryPlanner` dialect extensions for DuckDB, Snowflake, BigQuery time functions.

---

## GAP-11 — Streaming Response Support
**Severity**: 🟡 P2  
**Current state**: `POST /query` is synchronous. For slow queries (10–30 seconds), the user sees nothing until completion.  
**Required**: Server-Sent Events (SSE) or chunked streaming with status updates:
```
data: {"stage": "schema_linking", "status": "running"}
data: {"stage": "intent_extraction", "status": "running"}
data: {"stage": "sql_generation", "status": "running"}
data: {"stage": "execution", "status": "running", "sql": "SELECT..."}
data: {"stage": "analysis", "status": "running"}
data: {"stage": "complete", "result": {...}}
```

---

## GAP-12 — Production Frontend (UX Redesign)
**Severity**: 🔴 P0  
**Current state**: `uada/api/routes/ui.py` — single-file HTML with plain textarea for input, div for output, vega-embed for chart. No layout, no sidebar, no SQL panel, no history.  
**Required layout**:
```
┌──────────────────────────────────────────────────────────────────┐
│  Left Sidebar (20%)    │  Main Chat (55%)    │  Right Panel (25%) │
│  ─────────────────     │  ─────────────────  │  ───────────────── │
│  • Database selector   │  • Chat messages    │  • SQL panel (copy)│
│  • Connection status   │  • KPI cards        │  • Execution plan  │
│  • Table browser       │  • Charts           │  • Schema metadata │
│  • Conversation history│  • Insights text    │  • Data quality    │
│  • Saved analyses      │  • Follow-up chips  │  • Row counts      │
│                        │  • Input box        │                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## GAP-13 — Semantic Search / Schema Embedding Refresh
**Severity**: 🟡 P2  
**Current state**: ChromaDB is populated at startup from `scl_manager.to_indexable_documents()`. Schema changes require restart. No incremental update path.  
**Required**: 
- `POST /connections/{id}/refresh` → re-reflects schema + re-embeds
- Schema fingerprint comparison to detect drift
- Incremental upsert (only changed documents re-embedded)
- Background refresh task (scheduled or webhook-triggered)

---

## GAP-14 — Insight Generation Depth
**Severity**: 🟡 P2  
**Current state**: `ResultAnalyser` produces:
- `narrative_insight`: one deterministic sentence (e.g., "Revenue grew 12% month-over-month")
- `key_finding`: primary metric value
- `trend_analysis`: TrendDirection enum

**Required TachyonIQ insight dimensions**:
- **Key Findings** — top 3 data points with business context
- **Drivers** — "Growth was driven by Enterprise segment (+34%) while SMB declined (-8%)"
- **Anomalies** — outlier explanation with context ("Oct spike 2.4σ above mean, coincides with promo period")
- **Trends** — directional + rate-of-change
- **Recommendations** — "Consider investigating the Free tier churn rate"
- **Data Quality** — "3.2% of records missing region; excluded from analysis"

---

## GAP-15 — Data Quality Intelligence
**Severity**: 🟡 P2  
**Current state**: `is_truncated` flag in `QueryResult`. No null handling, no duplicate detection, no consistency warnings.  
**Required**:
- Null % warning when result set has significant missing values
- Duplicate row detection for JOIN results (cartesian join warning)
- Truncation warning with estimated true row count
- Data freshness indicator (when was the source table last updated?)
- Type mismatch warnings (e.g., date stored as VARCHAR)

---

## GAP-16 — RBAC / Multi-Tenant Architecture
**Severity**: 🟡 P2  
**Current state**: Single `api_key` in settings. All authenticated users have identical access.  
**Required**:
- User identity (JWT from auth provider or basic user_id)
- Role definitions: viewer, analyst, admin
- Connection-level access (user A can query sales DB, not finance DB)
- Row-level security passthrough (database row filters per user role)
- Audit log (who queried what, when, what SQL was generated)

---

## GAP-17 — Saved Analyses and Query History
**Severity**: 🟡 P2  
**Current state**: No persistence layer for analyses. Conversation history is session-scoped and in-memory.  
**Required**:
- Save analysis: title, question, SQL, result summary, chart spec
- Browse history: paginated list of past analyses
- Share analysis: permalink to a specific result
- Schedule analysis: recurring execution (daily revenue summary, etc.)

---

## GAP-18 — Explainability Layer
**Severity**: 🟡 P2  
**Current state**: Generated SQL is included in `UADAResponse` but not explained. The `executed_sql` field is in the API response but not prominently displayed in the UI.  
**Required**:
- "How was this calculated?" explanation in plain English
- SQL panel with syntax highlighting and copy button
- "Why this chart?" explanation
- "Which tables were used?" metadata
- "What assumptions were made?" (e.g., NULL handling, date range boundaries)

---

## Gap Priority Summary

| Priority | Gaps | Key Items |
|---|---|---|
| 🔴 P0 | 3 | Multi-DB connections, Connection UI, Production frontend |
| 🟠 P1 | 6 | Data profiling, Follow-up engine, KPI cards, Cloud adapters, Conv store, Chart types |
| 🟡 P2 | 9 | Multi-viz, Advanced analytics, Streaming, Schema refresh, Insights depth, Data quality, RBAC, Saved analyses, Explainability |
| 🟢 P3 | — | Cohort, A/B, segmentation, Oracle, ClickHouse |

---

## What Does NOT Need to Change

The following components are production-quality and should **not** be modified:
- `uada/pipeline/sql_validator.py` — the security boundary is correct and complete
- `uada/pipeline/orchestrator.py` — security invariants are correctly implemented
- `uada/models/` — the Pydantic contracts are well-designed and stable
- `uada/scl/` — the semantic layer is sound
- `uada/retrieval/` — hybrid retrieval with per-doctype budgets is the right design
- `uada/pipeline/query_planner.py` — dialect-aware, all relative periods covered
- `uada/db/interface.py` — clean abstraction, don't break the contract
