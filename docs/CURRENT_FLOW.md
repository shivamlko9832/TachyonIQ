# UADA — Current Data Flow

> Audit date: 2026-09-05

---

## End-to-End Request Flow

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│  USER INPUT                                                                      │
│  "Show me revenue by region for last quarter"                                    │
│  session_id: optional (UUID generated if absent)                                 │
└────────────────────────────────┬─────────────────────────────────────────────────┘
                                 │ POST /query
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│  AUTH MIDDLEWARE  (uada/api/middleware/auth.py)                                  │
│  • Bearer token check (disabled when api_key is None)                           │
│  • Pass-through for development                                                  │
└────────────────────────────────┬─────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│  QUERY ROUTE  (uada/api/routes/query.py)                                        │
│  • Parse QueryRequest(question, session_id)                                     │
│  • Generate UUID session_id if absent                                            │
│  • Delegate to PipelineOrchestrator.run()                                       │
│  • Always return HTTP 200 (errors encoded in UADAResponse)                       │
└────────────────────────────────┬─────────────────────────────────────────────────┘
                                 │
                                 ▼
┌══════════════════════════════════════════════════════════════════════════════════┐
║  PIPELINE ORCHESTRATOR  (uada/pipeline/orchestrator.py)                         ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║                                                                                  ║
║  ┌─────────────────────────────────────────────────────────────────────────┐    ║
║  │  STAGE 0: Session Setup                                                 │    ║
║  │  • ConversationStore.get_or_create(session_id)                         │    ║
║  │  • Load ConversationState (turns, active_context, compressed_context)   │    ║
║  │  • Build CoE-SQL edition context for LLM injection                     │    ║
║  └──────────────────────────────────┬──────────────────────────────────────┘    ║
║                                     │                                            ║
║  ┌──────────────────────────────────▼──────────────────────────────────────┐    ║
║  │  STAGE 1: Schema Linking  (uada/pipeline/schema_linker.py)             │    ║
║  │                                                                         │    ║
║  │  Per-doctype retrieval (prevents starving):                            │    ║
║  │  ┌──────────────────────────────────────────────────────────────────┐  │    ║
║  │  │ query="Show me revenue by region for last quarter"               │  │    ║
║  │  │                                                                  │  │    ║
║  │  │  TABLE   : ChromaDB (top_k=10) + BM25 → RRF → final_k=5        │  │    ║
║  │  │  METRIC  : ChromaDB (top_k=8)  + BM25 → RRF → final_k=4        │  │    ║
║  │  │  GLOSSARY: ChromaDB (top_k=8)  + BM25 → RRF → final_k=4        │  │    ║
║  │  │  EXAMPLE : ChromaDB (top_k=5)  + BM25 → RRF → final_k=3        │  │    ║
║  │  └──────────────────────────────────────────────────────────────────┘  │    ║
║  │                                                                         │    ║
║  │  Security check: re-verify all tables vs SCLManager.get_allowed_tables │    ║
║  │                                                                         │    ║
║  │  Output: SchemaContext {tables, metrics, glossary, examples, joins}    │    ║
║  └──────────────────────────────────┬──────────────────────────────────────┘    ║
║                                     │                                            ║
║  ┌──────────────────────────────────▼──────────────────────────────────────┐    ║
║  │  STAGE 2: Intent Extraction  (uada/pipeline/intent_extractor.py)       │    ║
║  │                                                                         │    ║
║  │  PydanticAI Agent → LLM call #1                                        │    ║
║  │  Input:  question + schema_context.to_prompt_context()                 │    ║
║  │          + conversation.get_edition_context() (CoE-SQL diffs)         │    ║
║  │  Output: AnalyticalIntent {                                            │    ║
║  │    question_type: AGGREGATION                                          │    ║
║  │    measures: ["revenue"]                                               │    ║
║  │    dimensions: ["region"]                                              │    ║
║  │    time_range: {type: RELATIVE, period: LAST_QUARTER, bucket: MONTH}  │    ║
║  │    references_prior_turn: false                                        │    ║
║  │  }                                                                     │    ║
║  │  Validators enforce cross-field constraints at parse time              │    ║
║  └──────────────────────────────────┬──────────────────────────────────────┘    ║
║                                     │                                            ║
║  ┌──────────────────────────────────▼──────────────────────────────────────┐    ║
║  │  STAGE 3: Query Planning  (uada/pipeline/query_planner.py)             │    ║
║  │                                                                         │    ║
║  │  Pure deterministic Python — NO LLM                                    │    ║
║  │  Input:  AnalyticalIntent + SchemaContext + ConversationState          │    ║
║  │  Output: QueryPlan {                                                   │    ║
║  │    dialect: POSTGRESQL                                                 │    ║
║  │    tables: [ResolvedTable(orders), ResolvedTable(customers)]           │    ║
║  │    joins: [ResolvedJoin(orders→customers LEFT on customer_id)]         │    ║
║  │    measures: [ResolvedMeasure(revenue, "SUM(orders.revenue)")]         │    ║
║  │    dimensions: [ResolvedDimension(region, "orders.region")]            │    ║
║  │    time_resolution: {                                                  │    ║
║  │      filter_sql: "order_date >= DATE_TRUNC('quarter', NOW()-'3 months'│    ║
║  │      group_by_sql: "DATE_TRUNC('month', order_date) AS period"         │    ║
║  │    }                                                                   │    ║
║  │  }                                                                     │    ║
║  └──────────────────────────────────┬──────────────────────────────────────┘    ║
║                                     │                                            ║
║  ┌──────────────────────────────────▼──────────────────────────────────────┐    ║
║  │  ┌─────────────────────────────────────────────────────────────────┐   │    ║
║  │  │  RETRY LOOP (max: settings.llm_max_retries)                    │   │    ║
║  │  │                                                                 │   │    ║
║  │  │  STAGE 4: SQL Generation  (uada/pipeline/sql_generator.py)     │   │    ║
║  │  │  PydanticAI Agent → LLM call #2                                │   │    ║
║  │  │  Input:  QueryPlan.to_generator_context() + dialect hint       │   │    ║
║  │  │  Output: "SELECT SUM(o.revenue), o.region,                     │   │    ║
║  │  │           DATE_TRUNC('month', o.order_date) AS period          │   │    ║
║  │  │           FROM orders o LEFT JOIN customers c ON ...           │   │    ║
║  │  │           WHERE order_date >= ... GROUP BY ... LIMIT 1000"     │   │    ║
║  │  │                                                                 │   │    ║
║  │  │  STAGE 5: SQL Validation  (uada/pipeline/sql_validator.py)     │   │    ║
║  │  │  ──────────────────────────────────────────────────────        │   │    ║
║  │  │  CRITICAL: Runs EVERY iteration, not just first pass           │   │    ║
║  │  │                                                                 │   │    ║
║  │  │  SQLGlot AST parse → 6 security rules:                         │   │    ║
║  │  │  1. SELECT-only? ──── NO → SECURITY_REJECTED, EXIT LOOP       │   │    ║
║  │  │  2. Single stmt? ──── NO → SECURITY_REJECTED, EXIT LOOP       │   │    ║
║  │  │  3. No sys schema?─── NO → SECURITY_REJECTED, EXIT LOOP       │   │    ║
║  │  │  4. Tables allowed? ─ NO → SECURITY_REJECTED, EXIT LOOP       │   │    ║
║  │  │  5. No danger fns? ── NO → SECURITY_REJECTED, EXIT LOOP       │   │    ║
║  │  │  6. Depth ≤ max? ──── NO → SECURITY_REJECTED, EXIT LOOP       │   │    ║
║  │  │  All pass → LIMIT injected if missing → ValidationResult.OK   │   │    ║
║  │  │                                                                 │   │    ║
║  │  │  STAGE 6: Execution  (uada/db/adapter.py)                      │   │    ║
║  │  │  SQLAlchemy connection (read-only, pooled)                     │   │    ║
║  │  │  fetchmany(max_rows+1) → truncation detection                  │   │    ║
║  │  │  Timeout: PostgreSQL=statement_timeout, SQLite=progress_handler│   │    ║
║  │  │                                                                 │   │    ║
║  │  │  ┌─── QueryTimeoutError → EXIT LOOP (no retry)                │   │    ║
║  │  │  │─── SecurityViolation → EXIT LOOP (no retry)                │   │    ║
║  │  │  │─── QueryExecutionError → repair_hint → Stage 4 (retry)     │   │    ║
║  │  │  └─── QueryExecutionResult → continue                          │   │    ║
║  │  └─────────────────────────────────────────────────────────────────┘   │    ║
║  └──────────────────────────────────┬──────────────────────────────────────┘    ║
║                                     │                                            ║
║  ┌──────────────────────────────────▼──────────────────────────────────────┐    ║
║  │  STAGE 7: Result Analysis  (uada/pipeline/result_analyser.py)          │    ║
║  │                                                                         │    ║
║  │  Input:  QueryExecutionResult + AnalyticalIntent                       │    ║
║  │  Pandas DataFrame from rows                                             │    ║
║  │                                                                         │    ║
║  │  Numeric column detection: from ColumnMeta.type (not pandas dtype)     │    ║
║  │  Time column: prefers "period" alias → intent.time_dimension → datetime│    ║
║  │                                                                         │    ║
║  │  Computation:                                                          │    ║
║  │  • numeric_summaries: {min, max, mean, median, std} per numeric col   │    ║
║  │  • trend: consecutive pct changes → INCREASING/DECREASING/STABLE/     │    ║
║  │           VOLATILE/INSUFFICIENT_DATA                                   │    ║
║  │  • outliers: z-score > 3.0 → row indices + scores                     │    ║
║  │  • narrative_insight: one-sentence English (deterministic templates)   │    ║
║  │  • key_finding: primary metric headline                                │    ║
║  │                                                                         │    ║
║  │  Output: AnalysedResult                                                │    ║
║  └──────────────────────────────────┬──────────────────────────────────────┘    ║
║                                     │                                            ║
║  ┌──────────────────────────────────▼──────────────────────────────────────┐    ║
║  │  STAGE 8: Visualization  (uada/pipeline/viz_generator.py)              │    ║
║  │                                                                         │    ║
║  │  Deterministic selection tree:                                          │    ║
║  │  row≤1 + numeric≤1 + no dims ──────────────────→ NO_CHART             │    ║
║  │  question_type==TIME_SERIES ───────────────────→ LINE chart            │    ║
║  │  question_type==RANKING AND rows≤20 ───────────→ BAR chart             │    ║
║  │  question_type==COMPARISON ────────────────────→ BAR (grouped)         │    ║
║  │  dimensions≥2 ─────────────────────────────────→ BAR                  │    ║
║  │  else ──────────────────────────────────────────→ LLM fallback (#3)   │    ║
║  │                                                                         │    ║
║  │  Altair → Vega-Lite JSON spec                                          │    ║
║  │  LLM fallback: validates mark name + column references                 │    ║
║  │                                                                         │    ║
║  │  Output: VegaLiteSpec | VisualisationFallback                          │    ║
║  └──────────────────────────────────┬──────────────────────────────────────┘    ║
║                                     │                                            ║
║  ┌──────────────────────────────────▼──────────────────────────────────────┐    ║
║  │  STAGE 9: Session Persistence  (uada/pipeline/conversation_store.py)   │    ║
║  │                                                                         │    ║
║  │  • Append ConversationTurn {                                           │    ║
║  │      turn_id, user_question, status: SUCCESS,                          │    ║
║  │      resolved_intent, generated_sql, result_summary,                   │    ║
║  │      tables_used, active_measures, dimensions, filters,                │    ║
║  │      chart_type, edition_diff                                          │    ║
║  │    }                                                                   │    ║
║  │  • Update ActiveContext: current_measures, dimensions, filters,        │    ║
║  │    time_range, tables, last_successful_sql                             │    ║
║  │  • Compress: turns > conversation_max_turns → compressed_context str  │    ║
║  │  • Save to in-memory dict (keyed by session_id)                        │    ║
║  └──────────────────────────────────┬──────────────────────────────────────┘    ║
╚════════════════════════════════════════════════════════════════════════════════╝  ║
                                      │                                             ║
                                      ▼                                            ║
┌──────────────────────────────────────────────────────────────────────────────────┐
│  RESPONSE ASSEMBLY                                                               │
│  UADAResponse {                                                                  │
│    session_id: "uuid-1234",                                                      │
│    question: "Show me revenue by region for last quarter",                       │
│    intent: AnalyticalIntent,                                                     │
│    query_plan: QueryPlan,                                                        │
│    executed_sql: "SELECT ...",                                                   │
│    result: AnalysedResult {                                                      │
│      query_result: QueryResult {columns, rows, row_count, is_truncated},        │
│      numeric_summaries: {...},                                                   │
│      trend_analysis: INCREASING,                                                 │
│      outliers: [],                                                               │
│      narrative_insight: "Revenue grew 12% month-over-month in Q4",              │
│      key_finding: "Total revenue: $2.4M"                                        │
│    },                                                                            │
│    visualisation: VegaLiteSpec {spec: {...}, chart_type: LINE},                 │
│    error: None    ← is_success = True                                           │
│  }                                                                               │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Follow-Up Question Flow

When `references_prior_turn=True` detected in intent:

```
ConversationState.get_edition_context()
    │
    │  Returns token-efficient diff:
    │  "PREV: measures=[revenue], dims=[region]
    │   THIS: measures=[revenue], dims=[region, segment]
    │   DIFF: +segment dimension"
    │
    ▼
IntentExtractor receives edition context
    │
    ▼
QueryPlanner._update_active_context() called:
    • Non-follow-up: RESET active_context
    • Follow-up: MERGE (empty fields = "keep current")
        - Empty measures → keep active_context.current_measures
        - Empty dimensions → keep active_context.current_dimensions
        - Filters: accumulated (not replaced)
```

---

## Error Path Flow

```
Any stage failure →
    │
    ├─ SecurityViolation → TurnStatus.SECURITY_REJECTED
    │                      UADAResponse.error.error_type = "security"
    │                      NO retry, no user-visible SQL
    │
    ├─ QueryTimeoutError → TurnStatus.ERROR
    │                      UADAResponse.error.is_retryable = False
    │                      NO retry
    │
    ├─ QueryExecutionError → repair_hint injected
    │                        sql_generator.repair() → retry from Stage 4
    │                        max llm_max_retries attempts
    │                        On exhaust → TurnStatus.ERROR
    │
    ├─ ValidationError (Pydantic) → LLM output did not match AnalyticalIntent
    │                               PydanticAI auto-retries internally
    │
    └─ OUT_OF_SCOPE / AMBIGUOUS intent → TurnStatus.CLARIFICATION_REQUESTED
                                          UADAResponse with clarification message
```

---

## Observability Flow

```
Every pipeline run:
    │
    ├─ OpenTelemetry spans (configure_tracing() at startup)
    │   • FastAPI request span (instrument_app)
    │   • PydanticAI agent spans (instrument_pydantic_ai)
    │   • DB execution span
    │
    ├─ Langfuse OTLP/HTTP
    │   • LLM traces (token usage, latency, model)
    │   • Basic Auth: base64(public_key:secret_key)
    │
    └─ Logfire bridge
        • logfire.configure(send_to_logfire=False) → sets "configured" flag
        • Allows instrument_pydantic_ai() to attach without real logfire
```

---

## Startup Flow

```
uvicorn main:app
    │
    ├─ load_dotenv() (config.py module-level)
    ├─ settings = Settings()
    │
    ├─ create_app()
    │   ├─ _bootstrap_orchestrator()
    │   │   ├─ SQLAlchemyAdapter(settings.db_url)
    │   │   ├─ SCLLoader.load(settings.scl_path) → SemanticContextLayer
    │   │   ├─ SCLManager(scl)
    │   │   ├─ Embedder(settings.embedding_model)
    │   │   ├─ ChromaBackend(settings.chroma_path)
    │   │   ├─ BM25Index(scl_manager.to_indexable_documents())
    │   │   ├─ HybridRetriever(chroma, bm25, embedder)
    │   │   ├─ SchemaLinker(retriever, scl_manager)
    │   │   ├─ IntentExtractor(settings)
    │   │   ├─ QueryPlanner(scl_manager, settings)
    │   │   ├─ SQLGenerator(settings)
    │   │   ├─ SQLValidator(scl_manager.get_allowed_tables())
    │   │   ├─ ResultAnalyser()
    │   │   ├─ VizGenerator(settings)
    │   │   ├─ ConversationStore(settings)
    │   │   └─ PipelineOrchestrator(all above)
    │   │
    │   └─ instrument_app() if not testing
    │
    └─ Routes: /query, /health, / (UI)
```
