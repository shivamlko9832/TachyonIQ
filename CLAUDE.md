# UADA — Claude Code Implementation Context

> Read this file completely before writing a single line of code.
> Every architectural decision referenced here has already been made and validated.
> Your job is to implement, not to redesign.

---

## What this project is

UADA (Unified Analytics Data Agent) is a conversational analytics platform.
A user connects it to an existing database and asks analytical questions in plain English.
UADA translates those questions into SQL, executes them, analyses the results,
and returns answers with charts.

Example questions it must handle:
- "What was our revenue last quarter?"
- "Show monthly revenue for the last 12 months."
- "Compare this year's sales with last year."
- "Only show enterprise customers." (follow-up that refines the previous query)

---

## What already exists in this repository

The repository was scaffolded with fully-specified contracts. Read these files before implementing anything:

| File | What it contains |
|---|---|
| `uada/config.py` | All configuration via Pydantic Settings. Every setting already defined. |
| `uada/models/intent.py` | `AnalyticalIntent` — the core IR between NL and SQL. Fully specified with validators. |
| `uada/models/query_plan.py` | `QueryPlan` — the SQL blueprint passed to the generator. Fully specified. |
| `uada/models/schema_context.py` | `SchemaContext` — output of schema linking. Fully specified. |
| `uada/models/result.py` | `QueryResult`, `AnalysedResult`, `VegaLiteSpec`, `UADAResponse`. Fully specified. |
| `uada/models/conversation.py` | `ConversationState`, `ConversationTurn`, `ActiveContext`. Fully specified. |
| `uada/pipeline/sql_validator.py` | Deterministic SQLGlot AST security validator. **Complete — do not modify the security logic.** |
| `uada/db/interface.py` | `DatabaseAdapter` ABC + all exception types. Implement this, do not change the interface. |
| `uada/retrieval/interface.py` | `RetrievalBackend` ABC. Implement this, do not change the interface. |
| `uada/scl/schema.py` | `SemanticContextLayer` Pydantic schema. The SCL YAML validates against this. |
| `config/semantic_context.yaml` | Example SCL YAML showing the format. |
| `pyproject.toml` | All dependencies already listed. |
| `docker-compose.yml` | Local deployment stack already defined. |
| `tests/unit/test_sql_validator.py` | 50+ security tests already written. Run these first. |
| `tests/unit/test_models.py` | Model validation tests already written. |
| `tests/evaluation/datasets/security_cases.jsonl` | 50 named security attack cases. |
| `docs/no-llm-no-db-implementation-guide.md` | Exactly which components can be built/tested without LLM or real database. |

---

## Architecture — do not redesign this

The pipeline has 10 stages in this exact order:

```
1.  Conversation Manager     [DETERMINISTIC] Load session, compress context
2.  Schema Linker            [DETERMINISTIC] Hybrid retrieval: BM25 + vector → RRF
3.  Analytical Intent        [LLM]           NL → AnalyticalIntent (Pydantic model)
4.  Query Planner            [DETERMINISTIC] AnalyticalIntent → QueryPlan (resolves SCL)
5.  SQL Generator            [LLM]           QueryPlan → SQL string
6.  SQL Security Validator   [DETERMINISTIC] SQLGlot AST — NEVER LLM — security boundary
7.  Query Executor           [DETERMINISTIC] SQLAlchemy read-only
8.  Result Analyser          [DETERMINISTIC] Pandas + DuckDB
9.  Visualisation Generator  [LLM]           AnalysedResult → Vega-Lite JSON spec
10. Response Assembler       [DETERMINISTIC] Build UADAResponse
```

LLM steps (3, 5, 9) use PydanticAI 2.0 with `output_type=BaseModel` validation.
All other steps are deterministic Python.

---

## Technology stack — do not change these

| Purpose | Library | Version |
|---|---|---|
| Agent orchestration | `pydantic-ai` | >=2.0.0 |
| SQL parsing / security | `sqlglot` | >=26.0.0 |
| Database connectivity | `sqlalchemy` | >=2.0.41 |
| Vector store (POC) | `chromadb` | >=0.5.0 |
| BM25 retrieval | `rank-bm25` | >=0.2.2 |
| Embeddings | `sentence-transformers` | >=3.0.0 |
| In-process analytics | `duckdb` | >=1.1.0 |
| DataFrame processing | `pandas` | >=2.2.0 |
| Visualization spec | `altair` | >=5.3.0 |
| API framework | `fastapi` + `uvicorn` | >=0.115.0 |
| Observability | `langfuse` | self-hosted, MIT |
| Evaluation | `deepeval` | >=1.5.0 |
| Local LLM | `ollama` (external) | qwen2.5-coder:7b |

Do not add new dependencies without a strong reason. Every dependency above was chosen deliberately.

---

## Security rules — non-negotiable

1. **The SQL Security Validator (`uada/pipeline/sql_validator.py`) is the security boundary.**
   It is already complete. Do not bypass it, weaken it, or add exceptions to it.

2. **Never execute SQL that has not passed `SQLValidator.validate()` with `is_safe=True`.**

3. **Security violations (`is_safe=False`) stop execution immediately. No LLM retry.**
   The repair loop (max 2 retries) is only for execution errors, not security rejections.

4. **Database credentials must be read-only.** The adapter must validate this on connect.

5. **Never log SQL values, user data, or connection strings.** Log query metadata only.

6. **Never inject column values into LLM prompts.** Only metadata (column names, types, descriptions).

---

## Implementation sequence — follow this order exactly

Each phase must have passing tests before starting the next.

### Phase 0 — Project foundation (start here)
```
Goal: Project installs cleanly and all existing tests pass.

Tasks:
1. Install dependencies:
   pip install -e ".[dev]"
   (or: uv sync --dev)

2. Run existing tests — they should all pass immediately:
   pytest tests/unit/test_models.py -v
   pytest tests/unit/test_sql_validator.py -v

3. If any test fails, the model files have been accidentally modified.
   Restore them from the specification.

4. Confirm the test output shows:
   - All TestAnalyticalIntent tests pass
   - All TestConversationState tests pass
   - All TestVegaLiteSpec tests pass
   - All TestQueryResult tests pass
   - All TestSCL tests pass
   - All 50+ security validator tests pass

Acceptance: pytest tests/unit/ exits with 0 failures.
```

### Phase 1 — Database connectivity
```
File to create: uada/db/adapter.py

Implement SQLAlchemyAdapter(DatabaseAdapter):

  __init__(self, connection_string: str, settings: Settings):
    - Create SQLAlchemy engine with:
        pool_size=settings.db_pool_size
        pool_recycle=settings.db_pool_recycle_seconds
        connect_args={"connect_timeout": 10}
    - Store dialect string from engine.dialect.name
    - Map SQLAlchemy dialect names to SQLGlot dialect names:
        postgresql → postgres
        mysql → mysql
        sqlite → sqlite
        mssql → tsql

  test_connection(self) -> bool:
    - Execute: SELECT 1
    - If it fails, raise ConnectionError
    - Optionally: try to write (INSERT INTO nonexistent) and verify it fails
      (confirms read-only — log warning if write succeeds)

  execute_query(self, sql, timeout_seconds, max_rows) -> QueryExecutionResult:
    - Use engine.connect() with execution_options(timeout=timeout_seconds)
    - Execute the SQL
    - Fetch max_rows + 1 rows
    - If row count > max_rows: set is_truncated=True, return max_rows rows
    - Map SQLAlchemy Column types to Python type strings:
        Integer → "int", Float/Numeric → "float",
        String/Text → "str", DateTime/Date → "datetime", Boolean → "bool"
    - On timeout: raise QueryTimeoutError(timeout_seconds, sql)
    - On any other error: raise QueryExecutionError(str(error), sql, self.dialect)

  get_raw_schema(self) -> RawSchemaInfo:
    - Use sqlalchemy.inspect(engine)
    - For each table: get_columns(), get_pk_constraint(), get_foreign_keys(), get_indexes()
    - Build RawTableInfo for each table
    - Compute schema_fingerprint: SHA-256 of sorted "table.column" strings
    - Return RawSchemaInfo

  explain_query(self, sql) -> str:
    - PostgreSQL: EXPLAIN {sql}
    - MySQL: EXPLAIN {sql}
    - SQLite: EXPLAIN QUERY PLAN {sql}
    - SQL Server: SET SHOWPLAN_TEXT ON; {sql}
    - Return result as string

Test file: tests/unit/test_db_adapter.py
  - Use SQLite in-memory: "sqlite:///:memory:"
  - Create test tables with known schema
  - Test: connection, query execution, row limit, schema reflection, fingerprint
  - Test: QueryTimeoutError raised on slow query (SQLite timeout is tricky — test the path)
  - No PostgreSQL/MySQL required in CI
```

### Phase 2 — Schema reflection and SCL generation
```
File to create: uada/scl/reflector.py
File to create: uada/scl/loader.py

Implement SCLReflector:

  from_adapter(self, adapter: DatabaseAdapter) -> SemanticContextLayer:
    - Call adapter.get_raw_schema()
    - For each table in RawSchemaInfo, create a TableDefinition:
        - name, columns (with types, PK/FK flags, references)
        - description: None (human fills this in)
        - grain: None (human fills this in)
        - excluded: False
    - For each FK, create a JoinDefinition with:
        - from_table, to_table, on condition
        - join_type: "LEFT"
    - Return SemanticContextLayer with:
        - version: "1.0"
        - database: DatabaseMeta(name=adapter.database_name, dialect=...)
        - tables: [all discovered tables]
        - joins: [all discovered FKs]
        - metrics: []  ← human fills these in
        - glossary: []  ← human fills these in
        - examples: []  ← human fills these in
        - security: SecurityPolicy()

Implement SCLLoader:

  load(path: Path) -> SemanticContextLayer:
    - Read YAML file
    - Parse with PyYAML
    - Validate with SemanticContextLayer.model_validate(data)
    - Return validated SCL
    - Raise clear errors with line numbers on validation failure

  save(scl: SemanticContextLayer, path: Path) -> None:
    - Serialize to YAML
    - Write to path

Test file: tests/unit/test_scl.py
  - Test loading config/semantic_context.yaml — it must load cleanly
  - Test reflector against in-memory SQLite with known schema
  - Test that join references are validated (referencing a non-existent table fails)
  - Test security exclusion logic (get_allowed_tables() excludes excluded_tables)
```

### Phase 3 — Semantic Context Layer manager
```
File to create: uada/scl/manager.py

Implement SCLManager:

  __init__(self, scl: SemanticContextLayer):
    - Store the loaded SCL
    - Build indexes for fast lookup:
        - table_index: dict[str, TableDefinition] (name → table)
        - metric_index: dict[str, MetricDefinition] (name + aliases → metric)
        - glossary_index: dict[str, GlossaryTerm] (term + aliases → term, lowercase)
        - allowed_tables: frozenset[str] (from scl.get_allowed_tables())

  get_allowed_tables(self) -> frozenset[str]:
    - Returns the set of table names permitted in SQL
    - Used by SQLValidator to build its allowlist

  resolve_metric(self, name: str) -> MetricDefinition | None:
    - Case-insensitive lookup by name and aliases

  resolve_glossary_term(self, term: str) -> GlossaryTerm | None:
    - Case-insensitive lookup by term and aliases

  get_join_path(self, from_table: str, to_table: str) -> JoinDefinition | None:
    - Look up the join between two tables (either direction)

  to_indexable_documents(self) -> list[dict]:
    - Returns a list of dicts ready for the retrieval indexer
    - One document per table, one per metric, one per glossary term, one per example
    - Each dict: {doc_id, content (text to embed), metadata}
    - doc_id format: "table:{name}", "metric:{name}", "glossary:{term}", "example:{idx}"
    - content for table: "{table_name}: {description}. Columns: {column names and descriptions}"
    - content for metric: "{metric_name}: {description}. Formula: {formula}"
    - content for glossary: "'{term}': {description}"
    - content for example: "Question: {question}"
    - metadata: {doc_type, table_name (if applicable), excluded: false}

Test file: tests/unit/test_scl_manager.py
  - Load config/semantic_context.yaml, build SCLManager
  - Test all lookup methods with known values
  - Test alias resolution (case-insensitive)
  - Test to_indexable_documents() returns expected count and format
```

### Phase 4 — Retrieval engine
```
Files to create:
  uada/retrieval/embedder.py
  uada/retrieval/bm25.py
  uada/retrieval/chroma_backend.py
  uada/retrieval/hybrid.py

Implement Embedder:
  __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
    - Load SentenceTransformer model
    - Cache the model (don't reload per call)

  embed(self, texts: list[str]) -> list[list[float]]:
    - Encode texts in batches (settings.embedding_batch_size)
    - Return list of embedding vectors

Implement BM25Index:
  __init__(self):
    - Empty index

  build(self, documents: list[dict]) -> None:
    - Tokenize each document's content (simple whitespace + lowercase)
    - Build BM25Okapi index
    - Store doc_ids in same order

  query(self, text: str, top_k: int = 10) -> list[RetrievalResult]:
    - Tokenize query
    - Score all documents
    - Return top_k as RetrievalResult(document, score, rank)

Implement ChromaBackend(RetrievalBackend):
  __init__(self, path: str, collection_name: str, embedder: Embedder):
    - Create persistent ChromaDB client at path
    - Get or create collection
    - Use embedder for all embedding

  upsert(self, documents: list[Document]) -> None:
    - Embed document contents
    - Upsert to ChromaDB with embeddings and metadata

  query(self, query_text, top_k, filters, collection) -> list[RetrievalResult]:
    - Embed query_text
    - Query ChromaDB with optional metadata filters
    - Return as RetrievalResult list

Implement HybridRetriever:
  __init__(self, vector_backend: RetrievalBackend, bm25_index: BM25Index):

  retrieve(self, query: str, top_k: int = 5, filters: dict | None = None) -> list[RetrievalResult]:
    - Get vector results (top_k * 2 candidates)
    - Get BM25 results (top_k * 2 candidates)
    - Merge with Reciprocal Rank Fusion:
        score = sum(1 / (rank + 60) for each list)
    - Return top_k results after merge, ordered by RRF score

  build_index(self, documents: list[dict]) -> None:
    - Convert dicts to Document objects
    - Upsert to vector backend
    - Build BM25 index

Test file: tests/unit/test_retrieval.py
  - Test BM25Index: build on 5 synthetic docs, query, verify expected doc in top-1
  - Test RRF: merge two ranked lists, verify merged order
  - Test ChromaBackend: in-memory (use tmp_path fixture), upsert, query
  - Test HybridRetriever: verify that a query matching by exact name (BM25 wins)
    and semantic similarity (vector wins) both surface the right document
  - No GPU/CUDA required — BGE-small runs on CPU
  NOTE: First run will download the embedding model (~90MB). This is expected.
```

### Phase 5 — Schema linker
```
File to create: uada/pipeline/schema_linker.py

Implement SchemaLinker:
  __init__(self, retriever: HybridRetriever, scl_manager: SCLManager, settings: Settings):

  link(self, question: str, session_context: str | None = None) -> SchemaContext:
    - Build retrieval query: question + (session_context condensed if provided)
    - Retrieve from hybrid retriever (settings.retrieval_final_k results)
    - Filter: remove any doc with metadata["doc_type"] for excluded tables
    - Group results by doc_type:
        tables: build TableContext for each retrieved table
        metrics: build MetricContext for each retrieved metric
        glossary: build GlossaryContext for each retrieved glossary term
        examples: build ExampleContext for each retrieved example
    - For each retrieved table, also fetch its join definitions from SCL manager
    - Build and return SchemaContext

  _build_table_context(self, table_name: str, score: float) -> TableContext | None:
    - Look up table in SCL manager
    - If not in allowed_tables: return None (security exclusion)
    - Build TableContext with all non-excluded columns

Test file: tests/unit/test_schema_linker.py
  - Build a small in-memory index from config/semantic_context.yaml
  - Query "What was revenue last quarter?"
  - Assert: 'orders' table in returned SchemaContext.tables
  - Assert: 'revenue' column present
  - Assert: excluded tables never appear
  - Assert: security-excluded columns never appear
```

### Phase 6 — Analytical intent extractor
```
File to create: uada/pipeline/intent_extractor.py

Implement IntentExtractor:
  __init__(self, settings: Settings):
    - Create PydanticAI Agent:
        model = settings.llm_model  (e.g. "ollama:qwen2.5-coder:7b")
        output_type = AnalyticalIntent
        retries = settings.llm_max_retries

  SYSTEM_PROMPT (write this carefully — it matters most):
    """
    You are an analytical intent classifier for a business analytics system.

    Your job is to analyse a user's natural language question and extract
    a structured AnalyticalIntent object that describes what analytical
    operation the user wants to perform.

    Rules:
    - question_type must match the user's actual intent
    - measures must be names from the provided schema metrics
    - dimensions must be column or table names from the provided schema
    - time_range.bucket is REQUIRED for TIME_SERIES questions
    - Follow-up questions that say "only X" or "filter by X" are FOLLOW_UP_REFINE
    - Follow-up questions that say "compare that with" are FOLLOW_UP_EXTEND
    - Questions about "why" are DIAGNOSTIC
    - If you cannot determine intent with confidence >= 0.5, set question_type to AMBIGUOUS
      and set clarification_question
    - If the question cannot be answered from the available schema, set question_type to OUT_OF_SCOPE
    - raw_question MUST be set to the exact user input
    - All glossary terms from the schema context should be detected as SemanticFilters
      with glossary_term set to the matched term
    """

  extract(
    self,
    question: str,
    schema_context: SchemaContext,
    conversation_context: str
  ) -> AnalyticalIntent:
    - Build user message:
        "Schema context:\n{schema_context.to_prompt_context()}\n\n"
        "Conversation history:\n{conversation_context}\n\n"
        "User question: {question}"
    - Run agent
    - Return validated AnalyticalIntent
    - PydanticAI handles retries on ValidationError automatically

Test file: tests/unit/test_intent_extractor.py (uses TestModel — no LLM needed)
  from pydantic_ai.models.test import TestModel

  - Test with TestModel that returns a fixed AnalyticalIntent
  - Verify the prompt construction includes schema context
  - Verify the conversation context is included
  - Verify validation errors trigger retry (mock a bad response first, then good)
  - Do NOT test LLM quality here — that is the evaluation suite's job
```

### Phase 7 — Query planner
```
File to create: uada/pipeline/query_planner.py

Implement QueryPlanner:
  __init__(self, scl_manager: SCLManager):

  plan(self, intent: AnalyticalIntent, schema_context: SchemaContext) -> QueryPlan:
    This is pure deterministic logic — no LLM.

    1. Determine dialect from schema_context.dialect
    2. Resolve primary table:
       - If intent.measures: find the table that owns the first measure metric
       - If intent.dimensions only: use the first retrieved table
    3. Resolve measures:
       - For each measure name in intent.measures:
         - Look up MetricDefinition in SCL manager
         - If found: use metric.formula as sql_expression
         - If not found: use "SUM({table}.{measure})" as fallback
         - Build ResolvedMeasure
    4. Resolve dimensions:
       - For each dimension name in intent.dimensions:
         - Find the column in schema_context.tables
         - Build ResolvedDimension with sql_expression = "table.column"
    5. Resolve joins:
       - For each table referenced by measures/dimensions beyond the primary:
         - Look up join path in SCL manager
         - Build ResolvedJoin
    6. Resolve time:
       - If intent.time_range is set:
         - Call _resolve_time(intent.time_range, intent.time_dimension, dialect)
         - Returns TimeResolution with dialect-specific SQL
    7. Resolve filters:
       - For each SemanticFilter in intent.filters:
         - If filter.glossary_term: look up in SCL, use sql_filter
         - Else: build SQL fragment from entity/operator/value
    8. Build and return QueryPlan

  _resolve_time(self, time_range: TimeRange, column: str, dialect: str) -> TimeResolution:
    - Map RelativePeriod → dialect-specific SQL
    - PostgreSQL examples:
        LAST_QUARTER → "order_date >= DATE_TRUNC('quarter', NOW() - INTERVAL '3 months')
                         AND order_date < DATE_TRUNC('quarter', NOW())"
        LAST_12_MONTHS → "order_date >= NOW() - INTERVAL '12 months'"
    - MySQL examples:
        LAST_QUARTER → "order_date >= DATE_SUB(CURDATE(), INTERVAL 3 MONTH)"
    - SQLite examples:
        LAST_QUARTER → "order_date >= date('now', '-3 months')"
    - SQL Server examples:
        LAST_QUARTER → "order_date >= DATEADD(quarter, -1, GETDATE())"
    - For TIME_SERIES with bucket:
        month / PostgreSQL → "DATE_TRUNC('month', order_date) AS period"
        month / MySQL      → "DATE_FORMAT(order_date, '%Y-%m') AS period"
        month / SQLite     → "strftime('%Y-%m', order_date) AS period"

Test file: tests/unit/test_query_planner.py (no LLM, no database)
  - Build a SCLManager from config/semantic_context.yaml
  - Build AnalyticalIntents manually (not via LLM)
  - Test plan() for:
      aggregation: measures=["revenue"], time_range=LAST_QUARTER
      time_series: measures=["revenue"], time_range=LAST_12_MONTHS, bucket=MONTH
      ranking: measures=["revenue"], dimensions=["region"], limit=10, order_by DESC
      filter: filters=[enterprise customer glossary term]
  - Assert correct tables, measures, time_resolution SQL for each dialect
```

### Phase 8 — SQL generator
```
File to create: uada/pipeline/sql_generator.py

Implement SQLGenerator:
  __init__(self, settings: Settings):
    - Create PydanticAI Agent:
        model = settings.llm_model
        output_type = str  (plain SQL string)
        retries = settings.llm_max_retries

  SYSTEM_PROMPT:
    """
    You are an expert SQL generator for analytical queries.

    You receive a structured query plan and must produce a single, valid SQL
    SELECT statement that implements it exactly.

    Rules:
    - Output ONLY the SQL query. No explanation, no markdown, no code fences.
    - Use the exact dialect specified in the plan.
    - Use fully qualified column references (table.column) to avoid ambiguity.
    - Apply the metric formula from the plan exactly — do not improvise.
    - Apply the time filter from the plan exactly — use the provided SQL fragment.
    - Include all joins specified in the plan.
    - Include GROUP BY for all non-aggregated columns in SELECT.
    - Apply LIMIT if specified. If no LIMIT in the plan, do not add one.
    - Use aliases exactly as specified (measure.output_alias, dimension.output_alias).
    - For comparison queries, use UNION ALL with period labels.
    - Never use subqueries beyond what the plan specifies.
    - Never reference tables not in the plan.
    """

  generate(self, plan: QueryPlan) -> str:
    - Build user message from plan.to_generator_context()
      Format: JSON representation of the plan context dict
    - Run agent → returns SQL string
    - Strip any markdown fences if present (```sql ... ``` → plain SQL)
    - Return cleaned SQL string

  repair(self, sql: str, error: QueryExecutionError, plan: QueryPlan) -> str:
    - Build repair message:
        "The following SQL failed with error: {error.repair_hint}\n\n"
        "Failed SQL:\n{sql}\n\n"
        "Original plan:\n{plan.to_generator_context()}\n\n"
        "Fix the SQL to resolve the error. Output only the corrected SQL."
    - Run agent → returns repaired SQL string
    - Strip markdown fences
    - Return cleaned SQL

Test file: tests/unit/test_sql_generator.py (uses TestModel)
  - Test generate() with TestModel returning a known SQL string
  - Test repair() with TestModel
  - Test that markdown fences are stripped from output
  - Test that empty output raises an error
```

### Phase 9 — Pipeline orchestrator
```
File to create: uada/pipeline/orchestrator.py

Implement PipelineOrchestrator:
  __init__(
    self,
    db_adapter: DatabaseAdapter,
    scl_manager: SCLManager,
    schema_linker: SchemaLinker,
    intent_extractor: IntentExtractor,
    query_planner: QueryPlanner,
    sql_generator: SQLGenerator,
    result_analyser: ResultAnalyser,
    viz_generator: VisualisationGenerator,
    conversation_store: ConversationStore,
    validator: SQLValidator,
    settings: Settings,
  ):

  async run(self, question: str, session_id: str) -> UADAResponse:
    """Full pipeline execution with error handling and retry budget."""

    start_time = time.perf_counter()

    # Load session
    state = await conversation_store.load(session_id)
    turn_id = state.turn_count

    try:
      # Step 1: Schema linking
      context_str = state.get_edition_context()
      schema_ctx = schema_linker.link(question, context_str)

      # Step 2: Intent extraction
      intent = await intent_extractor.extract(question, schema_ctx, context_str)

      # Handle out-of-scope / ambiguous
      if intent.question_type in (QuestionType.OUT_OF_SCOPE, QuestionType.AMBIGUOUS):
        return _build_error_response(...)

      # Step 3: Query planning
      plan = query_planner.plan(intent, schema_ctx)

      # Step 4: SQL generation + validation + execution loop
      sql = await sql_generator.generate(plan)
      retries = 0

      while retries <= settings.llm_max_retries:
        # ALWAYS validate before execution
        validation = validator.validate(sql, dialect=schema_ctx.dialect)

        if not validation.is_safe:
          # Security violation: STOP. No retry.
          _log_security_incident(session_id, sql, validation)
          return _build_security_error_response(validation)

        normalised_sql = validation.normalised_sql or sql

        # Execute
        try:
          raw_result = db_adapter.execute_query(
            normalised_sql,
            timeout_seconds=settings.db_query_timeout_seconds,
            max_rows=settings.db_max_rows,
          )
          break  # Success

        except QueryExecutionError as e:
          retries += 1
          if retries > settings.llm_max_retries:
            return _build_execution_error_response(e)
          sql = await sql_generator.repair(normalised_sql, e, plan)

        except QueryTimeoutError as e:
          return _build_timeout_error_response(e)

      # Step 5: Result analysis
      query_result = _convert_to_query_result(raw_result, normalised_sql, schema_ctx)
      analysed = result_analyser.analyse(query_result, intent)

      # Step 6: Visualisation
      viz = await viz_generator.generate(analysed, intent)

      # Step 7: Build response
      answer = _build_narrative(analysed, intent)
      response = UADAResponse(
        session_id=session_id,
        turn_id=turn_id,
        timestamp=datetime.now(tz=timezone.utc),
        answer=answer,
        sql=normalised_sql,
        row_count=query_result.row_count,
        is_truncated=query_result.is_truncated,
        execution_time_ms=raw_result.execution_time_ms,
        visualisation=viz,
        key_finding=analysed.key_finding,
        question_type=intent.question_type.value,
        tables_used=list(schema_ctx.tables[i].table_name for i in range(len(schema_ctx.tables))),
        pipeline_duration_ms=(time.perf_counter() - start_time) * 1000,
      )

      # Save conversation turn
      turn = ConversationTurn(...)
      state.turns.append(turn)
      state.active_context = _update_active_context(state.active_context, intent, query_result)
      await conversation_store.save(state)

      return response

    except Exception as e:
      # Log unexpected errors, return safe error response
      ...

IMPORTANT: The security check (validator.validate) MUST happen inside the
while loop, before every execution attempt. Never move it outside.

Test file: tests/integration/test_pipeline.py
  - Use SQLite in-memory with a known schema
  - Use TestModel for all LLM steps
  - Run the full pipeline end-to-end
  - Assert: response is UADAResponse with is_success=True
  - Assert: security violations return is_success=False with no SQL executed
  - Assert: execution errors trigger repair, not security stop
  - Assert: retry budget is respected (max 2 retries then error)
```

### Phase 10 — Result analyser and visualisation generator
```
File to create: uada/pipeline/result_analyser.py

Implement ResultAnalyser:
  analyse(self, result: QueryResult, intent: AnalyticalIntent) -> AnalysedResult:
    - Convert QueryResult to Pandas DataFrame
    - For each numeric column: compute NumericSummary (min, max, mean, median, std, sum)
    - If has_time_dimension: compute TrendAnalysis
        - Sort by time column
        - Calculate change from first to last value
        - Classify: INCREASING (>5%), DECREASING (<-5%), STABLE, VOLATILE
    - Detect outliers (z-score > 3) for numeric columns
    - Generate narrative_insight:
        - Aggregation: "Total {measure} was {value} {unit}."
        - Time series: "Revenue {direction} {pct}% over the period."
        - Ranking: "Top {dimension}: {top_value} with {measure}={value}."
    - Generate key_finding from the most significant number in the result

File to create: uada/pipeline/viz_generator.py

Implement VisualisationGenerator:
  __init__(self, settings: Settings):
    - Create PydanticAI Agent with output_type=dict (Vega-Lite JSON)

  CHART_SELECTION_RULES (deterministic, applied before calling LLM):
    - TIME_SERIES → line chart
    - RANKING with <= 20 rows → bar chart
    - COMPARISON of 2 periods → grouped bar chart
    - Single numeric value → no chart (return None)
    - Multiple dimensions → bar chart

  generate(self, result: AnalysedResult, intent: AnalyticalIntent) -> VegaLiteSpec | VisualisationFallback:
    - Apply deterministic chart type selection
    - If single value: return None
    - Use Altair to build the spec from the DataFrame
    - Validate with VegaLiteSpec.is_valid()
    - On failure: return VisualisationFallback(reason=..., data_available=True)
```

### Phase 11 — Conversation store
```
File to create: uada/pipeline/conversation_store.py

Implement ConversationStore (in-memory for POC):
  __init__(self):
    - self._sessions: dict[str, ConversationState] = {}

  async load(self, session_id: str) -> ConversationState:
    - If session_id not in _sessions:
        Create new ConversationState(session_id=session_id, ...)
    - Return session

  async save(self, state: ConversationState) -> None:
    - Compress context if state.turn_count > settings.conversation_max_turns:
        Summarise oldest turn into compressed_context string
        Remove oldest turn from turns list
    - Update last_updated
    - Store in _sessions

  async delete(self, session_id: str) -> None:
    - Remove from _sessions

NOTE for production: Replace dict with Redis or PostgreSQL.
The interface (load/save/delete) stays the same — only the storage backend changes.
```

### Phase 12 — API layer
```
File to create: uada/api/app.py
File to create: uada/api/routes/query.py
File to create: uada/api/routes/health.py
File to create: uada/api/routes/session.py
File to create: uada/api/middleware/auth.py

FastAPI application:

  POST /query
    Request body: {question: str, session_id: str | None}
    Response: UADAResponse
    - If session_id is None: generate new UUID
    - Call orchestrator.run(question, session_id)
    - Return UADAResponse

  GET /health
    Response: {status: "ok", version: "0.1.0", database: "connected" | "error"}

  DELETE /session/{session_id}
    Response: {deleted: true}

  Auth middleware:
    - If settings.api_key is set: require "Authorization: Bearer {api_key}" header
    - If settings.api_key is None: allow all (local dev)

Test file: tests/integration/test_api.py
  - Use FastAPI TestClient
  - POST /query with TestModel in place of real LLM
  - Assert response is UADAResponse
  - Assert /health returns 200
  - Assert auth middleware blocks requests with wrong key
```

### Phase 13 — Scripts and CLI
```
File to create: scripts/onboard_db.py

CLI script that:
  1. Takes --db-url as argument
  2. Connects via SQLAlchemy
  3. Reflects schema
  4. Generates candidate semantic_context.yaml
  5. Writes to --output path (default: config/semantic_context.yaml)
  6. Prints summary: "Discovered N tables, M foreign keys. SCL written to {path}."
  7. Prints next steps:
      "Next: Edit {path} to add:
        - table/column descriptions
        - metric definitions
        - glossary terms
        - example Q&A pairs
       Then run: python scripts/build_index.py"

File to create: scripts/build_index.py

CLI script that:
  1. Loads settings.scl_path
  2. Validates SCL
  3. Builds SCLManager
  4. Builds retrieval index (ChromaDB + BM25)
  5. Prints: "Indexed N documents. Ready."
```

### Phase 14 — Observability
```
File to create: uada/observability/tracer.py

Setup OpenTelemetry → Langfuse:
  - If settings.otel_enabled and settings.langfuse_secret_key:
      Configure OTLP exporter to settings.langfuse_host
      Instrument FastAPI
      Call logfire.instrument_pydantic_ai() to auto-trace LLM calls
  - Add span for each pipeline stage in orchestrator:
      with tracer.start_as_current_span("schema_linking") as span:
          span.set_attribute("retrieval.query", question)
          span.set_attribute("retrieval.result_count", len(results))
  - If Langfuse not configured: use console exporter (dev mode)

Spans to add (in orchestrator.run):
  - "schema_linking" — attributes: query, tables_retrieved
  - "intent_extraction" — attributes: question_type, confidence, measures
  - "query_planning" — attributes: dialect, table_count, join_count
  - "sql_generation" — attributes: attempt_number
  - "sql_validation" — attributes: is_safe, violation_type (if rejected)
  - "query_execution" — attributes: row_count, execution_time_ms, is_truncated
  - "result_analysis" — attributes: has_time_dimension, outlier_count
  - "viz_generation" — attributes: chart_type
```

### Phase 15 — Evaluation runner
```
File to create: tests/evaluation/run_security_eval.py

Deterministic security evaluation (no LLM):
  - Load tests/evaluation/datasets/security_cases.jsonl
  - For each case: run SQLValidator.validate(sql)
  - Assert: not result.is_safe
  - Assert: expected_violation in result.violations
  - Print: "Security evaluation: N/50 passed"
  - Exit 1 if any case passes (security gap)

Run this as part of CI — it requires no LLM, no database.

File to create: tests/evaluation/run_accuracy_eval.py

LLM accuracy evaluation (requires LLM + database):
  - Mark with @pytest.mark.evaluation
  - Load tests/evaluation/datasets/eval_dataset_format.jsonl
  - For each case: run full pipeline
  - Measure: intent accuracy, SQL execution accuracy, latency
  - Use DeepEval for LLM-as-judge metrics
```

---

## Critical rules for Claude Code

### Do implement

- All files listed in each phase
- Full error handling in every function (no bare `except: pass`)
- Type annotations on every function signature
- Docstrings on every class and public method
- Logging at INFO level for important events, DEBUG for verbose
- Unit tests for every implemented module

### Do not

- Do not change any model field names or types in `uada/models/`
- Do not change the `DatabaseAdapter` or `RetrievalBackend` interfaces
- Do not bypass the SQL validator
- Do not add LLM calls to deterministic pipeline stages
- Do not add new top-level dependencies without asking
- Do not remove the security deny-lists from `sql_validator.py`
- Do not use `print()` — use `logging.getLogger(__name__)`
- Do not hardcode the LLM model name — always read from `settings.llm_model`
- Do not hardcode database connection strings anywhere
- Do not commit `.env` files

### Code style

- Python 3.11+
- Type annotations everywhere (mypy strict)
- Ruff for linting (`ruff check .`)
- Black for formatting (`black .`)
- All imports at top of file
- No wildcard imports (`from x import *`)
- Prefer composition over inheritance (except for the ABC interfaces)

---

## How to verify your work at each phase

```bash
# After Phase 0 — must pass before starting Phase 1
pytest tests/unit/ -v

# After Phase 1
pytest tests/unit/test_db_adapter.py -v

# After Phase 2
pytest tests/unit/test_scl.py -v

# After Phase 3
pytest tests/unit/test_scl_manager.py -v

# After Phase 4
pytest tests/unit/test_retrieval.py -v

# After Phase 5
pytest tests/unit/test_schema_linker.py -v

# After Phase 6, 7, 8 (uses TestModel — no LLM)
pytest tests/unit/test_intent_extractor.py tests/unit/test_query_planner.py tests/unit/test_sql_generator.py -v

# After Phase 9 (full pipeline — SQLite + TestModel)
pytest tests/integration/test_pipeline.py -v

# After Phase 12 (API)
pytest tests/integration/test_api.py -v

# Security evaluation (no LLM, no database — run anytime)
python tests/evaluation/run_security_eval.py

# Full CI suite (what runs on every push)
pytest -m "not llm and not evaluation" -v
```

---

## Starting point

Run this right now:

```bash
pip install -e ".[dev]"
pytest tests/unit/test_models.py tests/unit/test_sql_validator.py -v
```

Both should pass with zero failures. If they do, the contracts are intact and you can start Phase 1.

If anything fails, read the error message — it will tell you which model field
or validator is broken. Restore the affected file from this specification document.
