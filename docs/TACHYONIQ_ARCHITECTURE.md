# TachyonIQ — Target Architecture

> Version: 1.0  
> Date: 2026-09-05  
> Status: Design complete — implementation begins at P0 sprint

---

## Vision Statement

TachyonIQ is a production-grade conversational analytics platform that connects to any database, automatically understands the data, and enables business users to ask questions in plain English and receive accurate, visualized, insightful answers.

**Core proposition**: Connect any database → TachyonIQ understands the data → Ask anything in English → Multi-step agentic analysis → Safe SQL execution → Deep statistical analysis → Auto-selected visualization → Insight narrative → Suggested next questions.

**Differentiation from Databricks Genie**: Works on any database (PostgreSQL, MySQL, Snowflake, BigQuery, Redshift, Oracle, SQLite, DuckDB). Fully self-hostable. Supports local LLMs (Ollama). Open architecture with extensible semantic layer.

---

## Architecture Diagrams

See `docs/architecture/`:
- `system-architecture.mmd` — Full system overview with all layers
- `agent-flow.mmd` — Sequence diagram: user question → response
- `query-lifecycle.mmd` — State machine: pipeline stages and error paths
- `database-connectivity.mmd` — Connection management and adapter tree
- `semantic-layer.mmd` — SCL, retrieval, and schema refresh flow
- `visualization-pipeline.mmd` — Chart type decision tree and spec generation

---

## 24 Named Modules

### Existing (KEEP / REFACTOR)

| # | Module | Class | File | Status |
|---|---|---|---|---|
| 1 | Database Interface | `DatabaseAdapter` | `uada/db/interface.py` | KEEP |
| 2 | SQLAlchemy Adapter | `SQLAlchemyAdapter` | `uada/db/adapter.py` | KEEP + EXTEND |
| 3 | Intent Model | `AnalyticalIntent` | `uada/models/intent.py` | KEEP |
| 4 | Query Plan Model | `QueryPlan` | `uada/models/query_plan.py` | KEEP |
| 5 | Result Models | `QueryResult`, `AnalysedResult`, `VegaLiteSpec`, `UADAResponse` | `uada/models/result.py` | KEEP |
| 6 | Conversation Models | `ConversationState`, `ConversationTurn` | `uada/models/conversation.py` | KEEP |
| 7 | Schema Context Model | `SchemaContext` | `uada/models/schema_context.py` | KEEP |
| 8 | Semantic Layer | `SemanticContextLayer` | `uada/scl/schema.py` | KEEP |
| 9 | SCL Loader | `SCLLoader` | `uada/scl/loader.py` | KEEP |
| 10 | SCL Manager | `SCLManager` | `uada/scl/manager.py` | KEEP |
| 11 | SCL Reflector | `SCLReflector` | `uada/scl/reflector.py` | REFACTOR: add profiling |
| 12 | Hybrid Retriever | `HybridRetriever` | `uada/retrieval/hybrid.py` | KEEP |
| 13 | Schema Linker | `SchemaLinker` | `uada/pipeline/schema_linker.py` | KEEP |
| 14 | Intent Extractor | `IntentExtractor` | `uada/pipeline/intent_extractor.py` | KEEP |
| 15 | Query Planner | `QueryPlanner` | `uada/pipeline/query_planner.py` | KEEP + EXTEND dialects |
| 16 | SQL Generator | `SQLGenerator` | `uada/pipeline/sql_generator.py` | KEEP |
| 17 | SQL Validator | `SQLValidator` | `uada/pipeline/sql_validator.py` | KEEP — DO NOT MODIFY |
| 18 | Result Analyser | `ResultAnalyser` | `uada/pipeline/result_analyser.py` | REFACTOR: DuckDB + insight depth |
| 19 | Viz Generator | `VizGenerator` | `uada/pipeline/viz_generator.py` | REFACTOR: 20+ chart types |
| 20 | Conversation Store | `ConversationStore` | `uada/pipeline/conversation_store.py` | REFACTOR: Redis backend |
| 21 | Orchestrator | `PipelineOrchestrator` | `uada/pipeline/orchestrator.py` | KEEP + streaming |

### New (ADD)

| # | Module | Class | File | Priority |
|---|---|---|---|---|
| 22 | Connection Manager | `DatabaseConnectionManager` | `uada/db/connection_manager.py` | P0 |
| 23 | Data Profiler | `DataProfiler` | `uada/pipeline/data_profiler.py` | P1 |
| 24 | Follow-Up Engine | `FollowUpEngine` | `uada/pipeline/followup_engine.py` | P1 |

Plus adapter modules: `uada/db/adapters/snowflake.py`, `bigquery.py`, `redshift.py`, `oracle.py`, `duckdb.py`

---

## Module 22: DatabaseConnectionManager

**Purpose**: Runtime registry of database connections, replacing the single static `UADA_DB_URL`.

```python
class DatabaseConnectionManager:
    """
    Thread-safe registry of DatabaseAdapter instances.
    Connections are identified by connection_id (UUID).
    Persists to JSON file or database table across restarts.
    """
    
    def register(self, config: ConnectionConfig) -> str:
        """
        Register a new connection. Returns connection_id.
        Stores credentials in secret manager / env, never in registry.
        """
    
    def get_adapter(self, connection_id: str) -> DatabaseAdapter:
        """Return cached adapter (creates pool on first call)."""
    
    def test_connection(self, connection_id: str) -> ConnectionTestResult:
        """SELECT 1 + latency + server version."""
    
    def discover_schema(self, connection_id: str) -> RawSchemaInfo:
        """Full schema reflection via adapter.get_raw_schema()."""
    
    def profile_connection(self, connection_id: str) -> ProfileResult:
        """Run DataProfiler on all included tables."""
    
    def list_connections(self) -> list[ConnectionSummary]:
        """All registered connections (no credentials)."""
    
    def delete_connection(self, connection_id: str) -> None:
        """Remove connection and clear its schema from ChromaDB."""
```

**ConnectionConfig** (never stored with credentials):
```python
class ConnectionConfig(BaseModel):
    name: str                          # display name
    dialect: str                       # postgresql | mysql | snowflake | ...
    host: str
    port: int
    database: str
    username: str
    password: SecretStr                # Pydantic SecretStr — never serialized
    ssl: bool = True
    extra: dict = {}                   # warehouse, role, project, dataset, etc.
```

**REST API**:
```
POST   /connections                    Create and test connection
GET    /connections                    List all (no credentials)
GET    /connections/{id}               Get one (no credentials)
DELETE /connections/{id}               Remove
POST   /connections/{id}/test          Re-test connectivity
POST   /connections/{id}/discover      Re-discover schema
POST   /connections/{id}/profile       Run data profiling
POST   /connections/{id}/refresh       Re-embed schema into ChromaDB
```

---

## Module 23: DataProfiler

**Purpose**: Column-level statistics for semantic grounding and chart recommendation.

```python
class DataProfiler:
    def profile_table(
        self, 
        adapter: DatabaseAdapter, 
        table: RawTableInfo,
        sample_size: int = 10_000
    ) -> TableProfile:
        """
        For each column, compute:
        - null_count, null_pct
        - distinct_count (estimated for large tables)
        - min_value, max_value (for ordered types)
        - mean, std (for numeric)
        - most_frequent_values: list[tuple[Any, int]] (top 10)
        - has_enum_pattern (bool: distinct_count ≤ 50)
        - temporal_min, temporal_max (for datetime columns)
        """
```

**Integration points**:
- `SCLReflector.from_adapter()` calls `DataProfiler.profile_table()` for each table
- Profile results stored in `ColumnDefinition.profile: ColumnProfile | None`
- Used in: filter suggestion, chart axis selection, KPI threshold detection
- Invalidated when schema fingerprint changes

**Sampling strategy**:
- Row count ≤ 100k: full scan
- Row count 100k–10M: `TABLESAMPLE SYSTEM(10)` (PostgreSQL/Snowflake/BigQuery) or `ORDER BY RANDOM() LIMIT 10000`
- Row count > 10M: `LIMIT 10000` with warning

---

## Module 24: FollowUpEngine

**Purpose**: Generate 3–5 contextual follow-up questions after each successful analysis.

```python
class FollowUpEngine:
    def suggest(
        self,
        intent: AnalyticalIntent,
        result: AnalysedResult,
        conversation_state: ConversationState,
        schema_context: SchemaContext,
    ) -> list[str]:
        """
        Deterministic-first: rule-based generation from intent + result.
        LLM enrichment (optional): enhance with data-specific context.
        Returns 3–5 English questions, deduplicated against conversation history.
        """
```

**Deterministic rules** (by question_type):

| Current type | Suggested follow-up patterns |
|---|---|
| AGGREGATION | "Break [metric] down by [available_dimension]", "How does this compare to [prior_period]?", "Which [entity] has the highest [metric]?" |
| TIME_SERIES | "What's driving the [direction] in [month]?", "Forecast [metric] for next [bucket]", "Show [metric] by [dimension] over the same period" |
| RANKING | "Show the trend for [top_entity] over time", "How does [top_entity] compare to [2nd_entity]?", "What's [top_entity]'s breakdown by [dimension]?" |
| COMPARISON | "Which [dimension] is growing fastest?", "Show percentage change vs prior period" |
| DIAGNOSTIC | "Show the full distribution of [metric]", "Are there seasonal patterns?" |

**LLM enrichment** (when deterministic suggestions < 3):
- Inject key_finding + result summary into prompt
- Ask for 2–3 data-specific questions
- Validate: questions must reference tables/metrics in schema_context

---

## Security Architecture

```
TachyonIQ 5-Layer Defense Model
═══════════════════════════════════════════════════════════════════
Layer 1 │ Credentials    │ SecretStr; never in logs/prompts/SQL/state
Layer 2 │ Schema Allowlist│ SCL.security enforced in SchemaLinker + SQLValidator
Layer 3 │ AST Validation  │ SQLValidator: 6 rules, SQLGlot AST, INSIDE retry loop
Layer 4 │ Execution       │ Read-only pool; query_timeout; fetchmany+max_rows
Layer 5 │ Prompt Injection│ Comment stripping; SQL fence removal; intent validation
═══════════════════════════════════════════════════════════════════

CRITICAL INVARIANT: SQLValidator.validate() runs EVERY iteration of the
repair loop. Repaired SQL is new unvalidated LLM output and must be
re-validated before execution. This invariant must NEVER be broken.

Security violation → immediate halt, no retry, no user-visible SQL fragment.
```

**RBAC Layer (P2 ADD)**:
```
User identity → JWT claims → Role (viewer | analyst | admin)
Role → ConnectionPolicy (which connection_ids are accessible)
Connection → SchemaPolicy (which tables are accessible per user)
Request → AuditLog (user_id, connection_id, question, sql_hash, timestamp)
```

---

## Conversation Memory Architecture

**CoE-SQL (Chain of Editions)**:  
Rather than storing raw history (token-expensive), UADA tracks diffs between turns:
```
Turn 1: measures=[revenue], dims=[region]
Turn 2: +dim: segment  → "added dimension: segment"
Turn 3: +filter: status='complete' → "added filter: completed orders only"
Turn 4: reset → new question
```

`get_edition_context()` returns a compact summary:
```
ACTIVE: revenue by region, segment | filter: completed orders | Q4 2025
PREV: revenue by region only
DIFF: +segment dimension, +status filter
```

**ConversationState fields**:
- `turns: list[ConversationTurn]` — up to `conversation_max_turns` (default 6)
- `compressed_context: str` — summary of older turns (when > max_turns)
- `active_context: ActiveContext` — current analytical frame (measures, dimensions, filters, time_range, tables, last_sql)

**Production store (P1 ADD)**:
```python
class RedisConversationStore(ConversationStore):
    """
    Redis-backed with TTL.
    Serializes ConversationState via .model_dump_json().
    Key: f"tachyoniq:session:{session_id}"
    TTL: settings.session_ttl_seconds (default 86400 = 24h)
    """
```

---

## Visualization Architecture

**Deterministic-first, 20+ chart types**:

```
row=1 + numeric≤1 + no dims  → KPI Card ★
TIME_SERIES                  → Line Chart
RANKING + rows≤20            → Horizontal Bar
COMPARISON + dims=1          → Grouped Bar ★
COMPARISON + dims≥2          → Stacked Bar ★
DIAGNOSTIC + 2 numerics      → Scatter ★
Distribution question        → Histogram ★
Proportions ≤7 segments      → Pie / Donut
Cross-dimensional            → Heatmap
Running total                → Waterfall ★
Conversion steps             → Funnel ★
Hierarchical proportions     → Treemap ★
Large dimension set          → Table Fallback ★
Anomalies present            → Rule overlay on primary
else                         → LLM fallback (validated)

★ = new additions to existing 8 types
```

**Multi-visualization response (P2 ADD)**:
```python
class UADAResponse(BaseModel):
    visualisation: VegaLiteSpec | VisualisationFallback      # existing (primary)
    supplementary_visualisations: list[VizItem] = []         # ADD: secondary charts
    suggested_questions: list[str] = []                      # ADD: follow-up chips
```

---

## Performance Architecture

**Caching strategy**:
```
Schema fingerprint cache  → invalidate on schema change
ChromaDB embeddings       → persist to disk (chroma_path)
BM25 index               → rebuild on SCL reload
Query result cache        → Redis TTL (configurable, off by default for analytics)
Connection pool           → QueuePool: pool_size=5, max_overflow=10
```

**Async execution**:
- FastAPI async endpoints
- `asyncio.wait_for()` for query timeout
- Background profiling tasks (not blocking the request)

**Streaming (P2 ADD)**:
```
GET /query/stream?question=...&session_id=...

data: {"stage": "schema_linking", "pct": 10}
data: {"stage": "intent_extraction", "pct": 25}
data: {"stage": "sql_generation", "pct": 40}
data: {"stage": "execution", "pct": 60, "sql": "SELECT..."}
data: {"stage": "analysis", "pct": 80}
data: {"stage": "visualization", "pct": 90}
data: {"stage": "complete", "pct": 100, "result": {...}}
```

---

## Frontend Architecture (P0)

**3-Panel Layout**:
```
┌─────────────────────────────────────────────────────────────────────┐
│  Header: TachyonIQ logo | Active DB: [acme_analytics ▾] | User      │
├──────────────────┬──────────────────────────┬───────────────────────┤
│  LEFT SIDEBAR    │  MAIN CHAT AREA          │  RIGHT PANEL          │
│  (20%)           │  (55%)                   │  (25%)                │
│                  │                           │                       │
│  Databases       │  ┌──────────────────────┐│  SQL Panel            │
│  ──────────────  │  │ User: Show me reve.. ││  ─────────────────    │
│  ● acme_analytics│  └──────────────────────┘│  SELECT               │
│  ○ + Add DB      │                           │    SUM(o.revenue),   │
│                  │  ┌──────────────────────┐│    o.region,         │
│  Tables          │  │ [KPI] $2.4M Revenue  ││    period            │
│  ──────────────  │  │ ↑ +12% vs last qtr   ││  FROM orders o...    │
│  orders (12k)    │  │                       ││  [Copy SQL] [Explain]│
│  customers (4k)  │  │ [Line chart: Rev/Mo]  ││                       │
│  products (200)  │  │                       ││  Tables Used          │
│                  │  │ Revenue grew 12%      ││  ─────────────────    │
│  Conversations   │  │ Q3→Q4 driven by       ││  orders, customers   │
│  ──────────────  │  │ Enterprise segment    ││                       │
│  ● Current       │  └──────────────────────┘│  Schema Metadata      │
│  ○ Yesterday     │                           │  ─────────────────    │
│  ○ Last week     │  Follow-up:               │  orders.order_date   │
│                  │  [By segment ▸] [Forecast]│  Range: Jan–Dec 2025 │
│  Saved           │  [Compare to Q3 ▸]        │  Null: 0%            │
│  ──────────────  │                           │                       │
│  ○ Q4 Revenue    │  ┌──────────────────────┐│  Data Quality         │
│  ○ Churn Analysis│  │ Ask anything...      ││  ─────────────────    │
│                  │  │              [Send ▶] ││  ✓ No missing values  │
│                  │  └──────────────────────┘│  ⚠ 3.2% rows excluded │
└──────────────────┴──────────────────────────┴───────────────────────┘
```

**Tech choices for frontend**:
- Option A: Single-file enhanced HTML + vanilla JS + Vega-Embed (minimal dependency, current direction)
- Option B: React + Vite SPA (more maintainable for multi-panel state)
- Recommendation: Option A for P0 sprint (fastest path to demo), Option B for M3

---

## Observability Architecture

**Structured logging** (every pipeline run):
```json
{
  "timestamp": "2026-09-05T10:23:11Z",
  "session_id": "uuid",
  "turn_id": 1,
  "question": "...",
  "question_type": "AGGREGATION",
  "tables_used": ["orders", "customers"],
  "sql_attempts": 1,
  "execution_time_ms": 342,
  "result_rows": 12,
  "chart_type": "LINE",
  "llm_tokens": {"prompt": 1240, "completion": 187},
  "status": "SUCCESS"
}
```

**OpenTelemetry spans**:
- FastAPI request span (instrument_app)
- Per-stage spans: schema_linking, intent_extraction, query_planning, sql_generation, sql_validation, execution, result_analysis, visualization
- LLM spans via PydanticAI instrumentation → Langfuse

**Metrics** (Prometheus):
- `tachyoniq_query_duration_seconds{stage, status}` histogram
- `tachyoniq_sql_attempts_total{question_type}` counter
- `tachyoniq_security_rejections_total{rule}` counter
- `tachyoniq_llm_tokens_total{model, type}` counter

---

## API Contract

```
POST /query
  Request:  {question: str, session_id: str | null, connection_id: str | null}
  Response: UADAResponse

GET  /query/stream
  SSE stream of pipeline stage events → final UADAResponse

POST /connections
  Request:  ConnectionConfig (host, port, db, user, password, dialect)
  Response: {connection_id, name, status: "connected"}

GET  /connections
  Response: list[ConnectionSummary] (no credentials)

DELETE /connections/{id}
  Response: 204

POST /connections/{id}/test
  Response: {latency_ms, server_version, tables_found}

POST /connections/{id}/profile
  Response: {status: "profiling", task_id}

GET  /health
  Response: {status: "ok", db: "connected", vector_store: "ok"}

GET  /metrics
  Prometheus text format
```

---

## Implementation Roadmap (P0 → P4)

### P0 Sprint (Prerequisite for any demo)
1. Production frontend — 3-panel UX, vega-embed, session state
2. `DatabaseConnectionManager` + REST endpoints
3. Connection setup wizard (TEST/DISCOVER/PROFILE/READY)
4. KPI card chart type
5. `FollowUpEngine` (deterministic rules, no LLM)

### P1 Sprint (Production-ready core)
6. `DataProfiler` + integration into SCLReflector
7. Redis-backed `ConversationStore`
8. Snowflake + BigQuery + Redshift adapters
9. 8 additional chart types (grouped bar, stacked bar, histogram, waterfall, funnel, table fallback, pie enhance, scatter)
10. `UADAResponse.suggested_questions` field + UI chips

### P2 Sprint (Feature depth)
11. SSE streaming for long queries
12. Insight narration depth (LLM-powered, Key Findings + Drivers + Anomalies)
13. Multi-visualization response (primary + secondary)
14. DuckDB integration in `ResultAnalyser`
15. Schema refresh API + incremental ChromaDB upsert
16. RBAC layer (user → role → connection policy)
17. Audit logging

### P3 Sprint (Advanced analytics)
18. Correlation + regression analysis module
19. Anomaly detection (isolation forest)
20. Time series forecasting (Prophet / exponential smoothing)
21. Saved analyses + query history persistence

### P4 Sprint (Enterprise)
22. Oracle + Databricks SQL adapters
23. Cohort analysis module
24. Multi-DB per session (cross-database joins via DuckDB)
25. Scheduled analyses (recurring execution)
26. RBAC-aware row filter passthrough

---

## Technology Stack (Complete)

| Component | Technology | Notes |
|---|---|---|
| Agent orchestration | PydanticAI 2.0 | output_type=BaseModel, auto-retry |
| SQL parsing / security | SQLGlot | AST-based, dialect-agnostic |
| Database connectivity | SQLAlchemy Core 2.0 | Read-only, connection pooling |
| PostgreSQL driver | psycopg2 / asyncpg | |
| MySQL driver | pymysql | |
| Snowflake driver | snowflake-sqlalchemy | ADD |
| BigQuery driver | sqlalchemy-bigquery | ADD |
| Redshift driver | sqlalchemy-redshift + psycopg2 | ADD |
| DuckDB driver | duckdb-engine | ADD |
| Vector store | ChromaDB | Persistent, no built-in embedder |
| Lexical retrieval | rank-bm25 | BM25Okapi |
| Rank fusion | RRF (custom) | k=60, per-doctype |
| Embeddings | sentence-transformers | BAAI/bge-small-en-v1.5 |
| Visualization | Altair / Vega-Lite | Deterministic spec generation |
| Data analysis | Pandas + NumPy | |
| OLAP analytics | DuckDB (client) | In-process aggregation on results |
| Forecasting | Prophet / statsmodels | P3 |
| Anomaly detection | scikit-learn (IsolationForest) | P3 |
| Session store | Redis (production) | P1, fallback to in-memory |
| Observability | OpenTelemetry + Langfuse | Logfire bridge |
| API | FastAPI + Uvicorn | Async, SSE streaming |
| Configuration | Pydantic Settings | UADA_ prefix, .env |
| Testing | pytest + pydantic-ai TestModel | Unit + integration + eval |
