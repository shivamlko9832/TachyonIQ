# TachyonIQ engineering audit and implementation baseline

This document records the implementation baseline for the ASTRA 6 enterprise
brief. It separates behavior verified in the repository from behavior that is
only planned, so a demo cannot be mistaken for a production control.

## Verified in the repository

| Area | Current behavior | Evidence |
| --- | --- | --- |
| Semantic grounding | A YAML Semantic Context Layer defines tables, columns, metrics, joins, glossary terms, examples, and exclusions. The schema linker retrieves a bounded context and the deterministic planner expands it into a `QueryPlan`. | `config/semantic_context.yaml`, `uada/scl/`, `uada/pipeline/schema_linker.py`, `uada/pipeline/query_planner.py` |
| SQL security boundary | SQLGlot validation rejects non-read statements, multi-statement input, system schemas, disallowed tables, dangerous functions, nested write operations, `SELECT INTO`, row locks, excessive limits, comments, and excessive subquery depth. A limit is injected for safe SELECTs. | `uada/pipeline/sql_validator.py`, `tests/unit/test_sql_validator.py`, `tests/evaluation/datasets/security_cases.jsonl` |
| Execution boundary | The orchestrator validates every generated statement before execution and can execute a selected connection adapter. Query results are bounded and represented as typed row/column data. | `uada/pipeline/orchestrator.py`, `uada/db/adapter.py`, `uada/models/result.py` |
| Connection lifecycle | Connection metadata is persisted without credentials; adapters are lazy and pooled. HTTP routes now preserve injected registries and scope connection access by authenticated owner for non-admin identities. | `uada/db/connection_manager.py`, `uada/api/routes/connections.py` |
| API response contract | Successful responses include structured `query_result` data for the UI. Viewer identities receive the rows but not executed SQL, including SQL nested inside `query_result`. | `uada/models/result.py`, `uada/api/routes/query.py` |
| Liveness behavior | `/health` and static UI paths are intentionally public; query, connection, session, and saved-analysis mutations remain subject to API-key/JWT policy. | `uada/api/middleware/auth.py`, `uada/api/routes/health.py` |
| Result quality loop | ResultCritic scores empty, cardinality, time axis, join, aggregation, sample, alignment, and anomaly dimensions. Empty results are a hard zero-score failure. Replanner retries once; a failed retry returns its terminal validation response instead of falling back to the original result. | `uada/pipeline/result_critic.py`, `uada/pipeline/replanner.py`, `uada/pipeline/orchestrator.py` |
| Observability | Request IDs, stage tracing, audit records, table usage, critic score, and explainability fields are available in the pipeline. | `uada/api/middleware/request_id.py`, `uada/observability/`, `uada/models/result.py` |
| Demo data | The generator is deterministic (`random.seed(42)`) and now creates a Europe recent-period decline plus one isolated APAC anomaly, in addition to the normal customer/product/order relationships. | `scripts/_gen_demo_db.py`, `docs/DEMO_SCENARIOS.md` |
| Governed analytical intent | Analysis profiles contain metric, dimension, time, ranking and method contracts without answer values. The normalizer converts natural language into typed operations and exact forecast horizons before planning. | `config/demo_semantic_context.yaml`, `uada/models/intent.py`, `uada/pipeline/intent_normalizer.py` |
| Statistical proof layer | Every successful governed response is analyzed from its executed rows by a deterministic engine. The report includes a SQL hash, result fingerprint, semantic aggregation, descriptive statistics, confidence intervals, trend tests, FDR-adjusted significance, robust anomaly detection, explanatory regression diagnostics, comparisons or backtested forecasts as requested. | `uada/analytics/statistical_engine.py`, `uada/analytics/forecast.py`, `tests/unit/test_governed_analytics.py` |
| Grounded narration | The deterministic narrative is the primary answer. Optional model-written findings are retained only when every numeric claim can be matched to the statistical report; otherwise a verified deterministic fallback is returned. | `uada/pipeline/orchestrator.py`, `uada/pipeline/insight_generator.py`, `tests/unit/test_insight_generator.py` |
| Evidence-aligned presentation | API compatibility fields for anomalies and correlations are projected from the governed statistical report, so the chat card and evidence drawer cannot disagree. Vega-Lite is enhanced in-browser when available, with deterministic local SVG support for named datasets, layered charts, distributions, time series, and ranking bars. | `uada/analytics/statistical_engine.py`, `uada/pipeline/orchestrator.py`, `uada/api/routes/ui.py`, `tests/unit/test_governed_analytics.py` |

## Partial or missing controls

These are deliberately visible gaps, not claims of completion:

- JWT role checks are present, but deployment still needs issuer/audience
  validation, key rotation, and a tenant identity model. Expiration is enforced
  when an `exp` claim is present; token subjects are required.
- Connection ownership is enforced for HTTP-selected connections, while the
  persisted registry has no tenant-wide policy engine or external secret
  manager. Credentials are intentionally not recoverable after a process
  restart; production must re-attach them through a secret provider.
- SCL metric filters are now carried into the plan. Unknown measures and
  dimensions still have compatibility fallbacks for the existing test contract;
  production mode should make semantic misses terminal errors after callers
  migrate away from that behavior.
- Fiscal calendars, row-level filters, column masking, and policy-aware prompt
  retrieval are represented by SCL fields but are not a complete database-native
  authorization boundary. Enforce them in the database (for example PostgreSQL
  RLS) before production use.
- Investigation and complex-question paths remain experimental. The safe
  linear path is the reference implementation for the Monday demo.
- Cloud adapters are registry stubs until their driver, authentication,
  timeout, cancellation, and read-only contracts are tested against each
  provider.
- Saved analyses and conversations now carry owner checks, but a full tenant
  hierarchy and external identity/secret provider are still required before
  multi-tenant deployment.
- Two legacy integration assertions still expect API-key authentication to block
  `/health`; the current liveness contract intentionally keeps that endpoint
  public.

## Statistical and grounding acceptance — 13 September 2026

The prior forecast and executive-summary response shortcuts have been removed.
Semantic profiles contain reusable analysis instructions only; they do not contain
answers, chart points, KPI values, or client-facing prose. Runtime values now follow
this path:

`question -> typed intent -> semantic plan -> validated read-only SQL -> returned rows -> deterministic statistics -> claim verification -> response`

Method selection is driven by typed operations:

| Question contract | Statistical implementation |
| --- | --- |
| Summary/distribution | Count, nulls, sum, mean, median, sample variance/standard deviation, quartiles, IQR, coefficient of variation, standard error, t confidence interval and optional D'Agostino-Pearson normality test |
| Trend | OLS slope, confidence interval, p-value and R² plus Mann-Kendall tau; Benjamini-Hochberg adjusted q-values control multiple testing |
| Anomaly | Modified z-score using median absolute deviation, with IQR fallback for zero-MAD samples |
| Relationship/driver | Pairwise Pearson tests with FDR-adjusted q-values and multivariable OLS using HC3 robust standard errors, residual diagnostics and variance-inflation factors; results are explicitly association-only |
| Comparison | Exact primary and baseline windows with absolute and relative deltas |
| Forecast | Chronological holdout comparison of supported models, selection by validation RMSE, exact requested horizon, prediction intervals and model/error metadata |

Live acceptance against `data/demo.sqlite` produced the following reproducible results:

- The executive-summary request now runs through the governed scorecard profile
  and returns 37 monthly rows. An independent replay compared all 407 returned
  cells with a fresh execution of the displayed SQL and found an exact match.
  The reported result fingerprint also matched a separately recomputed SHA-256;
  changing one value produced a different fingerprint.
- The one-month revenue forecast returned exactly one point. ARIMA was selected by
  chronological holdout, with first forecast 9,698,813.69, 95% interval
  7,407,851.10–11,989,776.28 and holdout RMSE 655,395.85. Independent execution of
  the same rows and forecaster returned the same values.
- The three-month regional forecast used the governed `orders -> customers` join,
  modeled four regions independently and aggregated three projected months.
- Europe's latest six complete months returned 8,478,163.70 versus 14,054,444.94
  in the preceding six months, a -39.68% change. Model-proposed time dimensions and
  duplicate time filters are discarded in favor of the governed comparison windows.
- Customer churn analysis evaluated all 2,500 accounts, displayed the top 10, and
  identified Clear Labs 0848 from the executed rows. Its correlations contain
  FDR-adjusted q-values and its explanatory regression uses all 2,500 observations;
  the result was not truncated.
- A fresh browser acceptance run rendered the executive monthly trend, revenue
  distribution and anomaly view. The chat card and statistics drawer both reported
  one robust anomaly; only the flagged row was displayed with its measure, period,
  observed value, score and selected MAD/IQR method. No legacy Isolation Forest
  output or unrequested correlation panel was mixed into that response.

Each result carries a SHA-256 fingerprint over the exact columns and rows, plus a
hash of the executed SQL. Changing a returned value changes the fingerprint. The
demo ground-truth runner independently checks table counts, scalar proofs, effect
sizes, correlations, the Europe decline and the APAC anomaly. The current unit suite
passes 496 tests, the integration suite passes 64 tests, and the deterministic demo
acceptance report passes.

The executive-summary failure shown during acceptance was traced to the planner's
table-reference lexer. It interpreted the decimal literal `100.0` in a governed
percentage formula as the qualified identifier `100.0`, then tried to find a join
to a table named `100`. The identifier matcher now requires SQL identifiers to begin
with a letter or underscore, and the governed-profile regression test plans the full
scorecard formula set to prevent recurrence. Profile normalization also clears
model-proposed comparison, ordering, limit and prior-turn fields that are absent from
the matched governed contract.

These checks establish that the demonstrated answers are computed rather than
prewritten. They do not make every statistical method appropriate for every future
dataset. The engine withholds models when sample or variance prerequisites fail,
reports truncation and missing-data limits, and labels regression evidence as
non-causal. Production readiness still requires the tenant, identity, connector and
evaluation gates below.

## Architecture decision

TachyonIQ should remain a modular monolith around a typed semantic plan and a
deterministic execution boundary. The system can expose MCP tools or delegate
complex investigations later, but those interfaces must call the same policy
and SQL validation layer.

| Reference | Useful pattern | Boundary to preserve |
| --- | --- | --- |
| Databricks Genie | Semantic space, trusted tables, evaluation and monitoring | Attached tables are context; Unity Catalog policies remain the authorization boundary. [Genie setup](https://docs.databricks.com/aws/en/genie-agents/set-up) |
| Snowflake Cortex Analyst/Agents | Semantic views, verified queries, agent orchestration | Keep metric definitions and access policy in governed semantic/database objects. [Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst), [Cortex Agents](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents) |
| Managed MCP | Standard tool transport for governed data actions | MCP is a transport and capability boundary, not permission by itself. [Managed MCP](https://docs.databricks.com/aws/en/agents/mcp-tools/managed-mcp), [Snowflake MCP](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-mcp) |
| Foundry MCP | Tool integration with explicit server configuration | Remote tool calls still need identity, audit, timeouts, and allowlists. [Foundry MCP](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools/model-context-protocol?view=foundry) |
| PostgreSQL RLS / DuckDB security | Database-native row and file-access controls | Use database policies for tenant isolation and disable unsafe extensions/file access. [PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html), [DuckDB security](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview) |

## Prioritized delivery plan

1. **Monday demo:** generate the deterministic dataset, load the demo SCL,
   connect SQLite, and demonstrate KPI, monthly trend, Europe decline, APAC
   anomaly, a follow-up filter, and rejected SQL attacks. Capture the request
   ID, generated SQL for analysts, tables used, row count, and critic score.
2. **P0 production safety:** move tenant and row policy enforcement into the
   selected adapter/database, require semantic resolution in production mode,
   add cancellation/timeout tests for every adapter, and make saved sessions
   owner-scoped.
3. **P1 evaluation:** run fresh-conversation intent/SQL/security cases, add
   golden numeric results for the demo scenarios, and record latency/error
   budgets by dialect. Genie evaluations also start fresh conversations, so
   follow-up quality must be measured separately. [Genie monitoring](https://docs.databricks.com/aws/en/genie-agents/monitor)
4. **P2 integrations:** expose governed semantic operations through MCP,
   implement cloud adapters one at a time, and add provider-specific contract
   tests before enabling a connector.
5. **P3 scale:** split services only when measured load or isolation requires
   it; preserve the typed `AnalyticalIntent` → `QueryPlan` → validated SQL
   chain across the split.

## Assumptions and operating rules

- The connected database is the source of truth for authorization. The LLM may
  propose intent and SQL, but it never grants access.
- Metric formulas, glossary SQL, and join definitions are trusted configuration
  and must be code-reviewed like application code.
- Raw rows may contain sensitive values. They are returned only to an
  authorized caller and are never sent to the insight prompt or written to
  logs by the profiling path.
- Any security validation failure is terminal for that attempt; a repair loop
  may only run after an ordinary execution error and must validate its new SQL
  again.
