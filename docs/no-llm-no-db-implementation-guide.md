# What Can Be Built and Tested Without an LLM or Real Database

This document answers the specific question:
> **Which components can be implemented and tested without an LLM or real client database?**

This is important because it determines what can be built and validated in CI immediately,
without any external dependencies.

---

## Fully buildable and testable with zero external dependencies

These components require only `pip install uada[dev]` and a Python interpreter.

### 1. All Pydantic Models — `uada/models/`

Every model in:
- `uada/models/intent.py` (AnalyticalIntent, TimeRange, SemanticFilter, etc.)
- `uada/models/query_plan.py` (QueryPlan, ResolvedMeasure, etc.)
- `uada/models/schema_context.py` (SchemaContext, TableContext, etc.)
- `uada/models/result.py` (QueryResult, AnalysedResult, UADAResponse, VegaLiteSpec, etc.)
- `uada/models/conversation.py` (ConversationState, ConversationTurn, ActiveContext)

**Test without LLM:** All model validation, cross-field validators, computed properties.
`pytest tests/unit/test_models.py` — no external calls.

### 2. SQL Security Validator — `uada/pipeline/sql_validator.py`

The entire SQLValidator is deterministic. It uses only SQLGlot (a pure Python library).

**Test without LLM or database:** The full 50+ security test suite.
`pytest tests/unit/test_sql_validator.py` — no external calls.

This is the most important component to test early — it is the security boundary.

### 3. SCL Schema and Loader — `uada/scl/schema.py`, `uada/scl/loader.py`

The SCL Pydantic models can be fully tested. The loader parses YAML and validates it
against the schema.

**Test without LLM or database:** Load `config/semantic_context.yaml` and validate it.
All model validators (join reference validation, metric alias resolution, etc.).

### 4. Configuration — `uada/config.py`

The Pydantic Settings model can be tested with environment variable injection.
No external calls required.

### 5. BM25 Retrieval — `uada/retrieval/bm25.py`

`rank_bm25` is pure Python. The BM25 index can be built over synthetic schema strings
and queried without any vector store or LLM.

**Test without LLM or database:** Index a list of synthetic schema descriptions,
query them, verify rank order.

### 6. RRF Merge Logic — `uada/retrieval/hybrid.py`

The Reciprocal Rank Fusion algorithm is pure arithmetic. Takes two ranked lists,
produces a merged ranked list.

**Test without LLM or database:** Unit test with synthetic scores, verify merge behavior.

### 7. VegaLiteSpec Validation — `uada/models/result.py`

The `VegaLiteSpec.is_valid()` method checks for required JSON keys.
Pure Python, no external calls.

### 8. ConversationState Logic — `uada/models/conversation.py`

`get_edition_context()`, `get_recent_turns()`, `last_successful_turn` — all pure Python.

### 9. Query Plan `to_generator_context()` — `uada/models/query_plan.py`

Serializes a QueryPlan into the dict that gets injected into the SQL Generator prompt.
Pure Python, no external calls.

### 10. Result Analysis — `uada/pipeline/result_analyser.py` (most of it)

Statistical summaries (min, max, mean, std), trend detection, and outlier detection
using Pandas and DuckDB can be tested against synthetic DataFrames.

**Test without LLM or database:** Create a synthetic QueryResult with known values,
run the analyser, verify summaries.

---

## Requires SQLite (available in stdlib — no server needed)

SQLite is part of Python's standard library. These components need a database
connection but not a real client database.

### 11. DatabaseAdapter (SQLAlchemy + SQLite)

The SQLAlchemy adapter with SQLite works entirely in memory:
```python
from sqlalchemy import create_engine
engine = create_engine("sqlite:///:memory:")
```

**Test with SQLite:** Connection pooling, query execution, row limits, timeout behavior,
schema reflection.

### 12. Schema Reflector — `uada/scl/reflector.py`

SQLAlchemy Inspector on an in-memory SQLite database.
Create a few tables, run the reflector, verify the output matches.

### 13. End-to-End Pipeline (without LLM steps) — Integration test

With SQLite and PydanticAI `TestModel`:

```python
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

# TestModel returns a fixed output without calling any LLM API.
# Use it to test the pipeline plumbing without LLM costs or latency.
agent = Agent(TestModel(), output_type=AnalyticalIntent)
```

The entire pipeline can be exercised:
- Load SCL from YAML
- Build retrieval index in-memory ChromaDB
- Schema link against SQLite test schema
- Extract intent via TestModel (returns fixed AnalyticalIntent)
- Plan query (deterministic)
- Generate SQL via TestModel (returns fixed SQL string)
- Validate SQL (deterministic SQLGlot)
- Execute on SQLite
- Analyse results (Pandas)
- Generate viz spec via TestModel

**This tests all pipeline plumbing, state management, error handling, and retry logic
without a single real LLM call.**

---

## Requires a real LLM (skip in CI, run in dev)

These tests require a live LLM connection. Mark with `@pytest.mark.llm`.

| Component | Why LLM needed | When to test |
|---|---|---|
| Intent Extractor | Actual NL → AnalyticalIntent mapping | Dev + evaluation runs |
| SQL Generator | Actual QueryPlan → SQL | Dev + evaluation runs |
| Visualisation Generator | Chart type selection | Dev + evaluation runs |
| Multi-turn coherence | Full conversation flow | Dev + evaluation runs |
| Evaluation accuracy (DeepEval) | LLM-as-judge metrics | Scheduled CI, not per-PR |

---

## Requires a real client database

Only the final end-to-end accuracy evaluation.

| Component | Why real DB needed |
|---|---|
| SQL execution accuracy | Need real data to verify result correctness |
| Schema reflection accuracy | Need real schema to verify coverage |
| SCL YAML authoring | Need to see actual column names and sample values |

---

## CI Strategy Summary

```
Every push:
  pytest -m "not llm and not evaluation"
  ├── test_sql_validator.py      (50+ cases, zero external calls)
  ├── test_models.py             (all model validation)
  ├── test_scl_loader.py         (YAML loading)
  ├── test_bm25.py               (BM25 ranking)
  ├── test_rrf.py                (RRF merge)
  ├── test_result_analyser.py    (Pandas stats — synthetic data)
  └── test_pipeline_sqlite.py    (pipeline plumbing — SQLite + TestModel)

On PR merge:
  pytest -m "integration"        (real SQLite, TestModel for LLM steps)

Scheduled (nightly or weekly):
  pytest -m "llm"                (real LLM, dev database)
  pytest -m "evaluation"         (DeepEval, full accuracy measurement)
```

---

## Implementation Sequence (Dependency Order)

The sequence below allows you to have passing tests at every step.

```
Step 1 (Day 1)   uada/models/intent.py          → test_models.py::TestAnalyticalIntent
Step 2 (Day 1)   uada/models/query_plan.py       → test_models.py::TestQueryPlan
Step 3 (Day 1)   uada/models/conversation.py     → test_models.py::TestConversationState
Step 4 (Day 1)   uada/models/result.py           → test_models.py::TestQueryResult, TestVegaLiteSpec
Step 5 (Day 2)   uada/scl/schema.py              → test_models.py::TestSCL
Step 6 (Day 2)   uada/scl/loader.py              → test_scl_loader.py
Step 7 (Day 2)   uada/pipeline/sql_validator.py  → test_sql_validator.py (all 50 cases)
Step 8 (Day 3)   uada/config.py                  → test_config.py
Step 9 (Day 3)   uada/retrieval/bm25.py          → test_bm25.py
Step 10 (Day 3)  uada/retrieval/hybrid.py        → test_rrf.py
Step 11 (Day 4)  uada/db/adapter.py (SQLite)     → test_pipeline_sqlite.py (Phase 1)
Step 12 (Day 4)  uada/scl/reflector.py           → test_pipeline_sqlite.py (Phase 2)
Step 13 (Day 5)  uada/retrieval/chroma.py        → test_pipeline_sqlite.py (Phase 4)
Step 14 (Day 6)  uada/pipeline/schema_linker.py  → test_pipeline_sqlite.py (Phase 6)
Step 15 (Day 7)  uada/pipeline/result_analyser.py → test_result_analyser.py
Step 16 (Day 7)  uada/pipeline/viz_generator.py  → test_viz_generator.py (synthetic)
Step 17 (Day 8)  uada/pipeline/orchestrator.py   → test_pipeline_sqlite.py (full TestModel)
Step 18 (Day 9)  uada/api/app.py                 → test_api.py (FastAPI test client)
```

By Step 17, the full pipeline runs end-to-end with SQLite and TestModel — no LLM needed.
Real LLM integration (Step 18+) replaces TestModel without changing any pipeline logic.
```
