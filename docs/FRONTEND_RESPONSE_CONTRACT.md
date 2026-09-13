# Frontend Response Contract for the Next.js Client

The supported UI is the Next.js application in `frontend/` on port 3001. The HTML UI
served directly by the backend on port 8000 is legacy and is not the integration target.

## Rules

1. Render the response for the latest submitted turn only in the right-side evidence panel.
   Clear stale SQL, statistics, and charts immediately when a new request begins or fails.
2. Keep the backend `session_id` for follow-ups. Starting a new connection must start a new
   session. A suggested-question click must submit into the same session.
3. Never calculate business values in the browser. KPI values, comparison percentages,
   forecast points, confidence bounds, anomalies, and evidence scores come from the API.
4. Never label an LLM-generated driver as proven. `generated_insights.drivers` are
   hypotheses. Deterministic findings live in `statistical_analysis` and `key_finding`.
5. A chart must render from the returned Vega-Lite specification or typed forecast points.
   Do not substitute a different aggregation. Show the chart rationale when available.
6. Display `error.user_message` and use `error.clarification_needed` when provided. Preserve
   the failed question in history and do not show evidence from the preceding success.

## Required response fields

The UI should type and render these backend fields in addition to the existing model:

- `statistical_analysis`: deterministic sample size, descriptive statistics, tests,
  method names, assumptions, limitations, and result SHA-256 provenance.
- `context_snapshot`: current measures, dimensions, filters, time range, and tables after a
  successful turn.
- `generated_insights.evidence_verified` and `verification_notes`: grounding status for
  LLM-written findings.
- `query_result.columns[].unit`: semantic display unit when available.
- `evidence_nodes`, `tables_used`, `critic_score`, `pipeline_duration_ms`, and
  `investigation_steps` for the evidence panel.

All response fields other than session identity are nullable or may be absent. The UI must
handle partial success without inventing placeholders such as zero, 100%, or a dash that
looks like a measured value.

## Recommended layout

- Answer header: concise finding, active period, and synthetic-data badge for the demo.
- KPI row: actual, budget/benchmark, variance, and valid unit.
- Primary chart: largest useful area, responsive, with tooltip, legend, zoom, and download.
- Statistical evidence: method, sample size, estimate/effect, interval, p-value and adjusted
  q-value when the backend returns them. Hide absent fields.
- Verification drawer: SQL, selected grain, joins, filters, row count, execution time,
  source tables, data-quality issues, result fingerprint, assumptions, and limitations.
- Follow-up chips: contextual questions submitted into the existing session.
- Data table: sortable, searchable, paginated, unit-aware, and exportable from returned rows.

## Required UI states

- Connecting or discovering schema.
- Interpreting question.
- Planning and validating query.
- Executing read-only SQL.
- Computing statistics and chart.
- Successful answer.
- Clarification required.
- Unsupported by available data.
- Security rejection.
- Execution failure with retry.
- No rows for the valid query.
- Truncated result with the exact truncation limit.

The frontend may improve presentation, but it must preserve API values and evidence
semantics exactly.
