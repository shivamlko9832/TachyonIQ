# TachyonIQ analytics experience research and roadmap

## Purpose

The demo must feel like an analytical workspace rather than a chat box that
returns a paragraph. A strong answer needs four linked layers: intent, a
visual explanation of the result, statistical evidence, and a path to explore
the next question without losing the active metric, filters, or time window.

## What leading products establish

Databricks Genie treats the conversation as an exploration loop. It can ask a
clarifying question, use trusted queries or functions, show how a trusted asset
was used, and suggest follow-up questions after every answer. Users can save a
visualization to a dashboard and request review when an answer needs human
attention. See [Use a Genie Space to explore business data](https://docs.databricks.com/aws/en/genie/talk-to-genie).

Snowflake Cortex Analyst makes verified questions part of the semantic layer.
The verified query repository pairs natural-language questions with reviewed
SQL, and the service can report which verified query supported an answer. This
is a practical pattern for making high-value demo questions reliable and
auditable. See [Cortex Analyst Verified Query Repository](https://docs.snowflake.com/user-guide/snowflake-cortex/cortex-analyst/verified-query-repository).

Power BI Copilot combines narrative and visuals, cites the report visuals used,
supports follow-up questions, and lets users explore or customize the visual
that was generated. Microsoft also recommends keeping the semantic model and
visuals clear because sampling and slow visuals reduce answer quality. See
[Summarize a report with Copilot](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-pane-summarize-content),
[Explore data with Copilot](https://learn.microsoft.com/en-us/power-bi/create-reports/tutorial-copilot-power-bi-explore-data),
and [Copilot for Power BI reports and semantic models](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-reports-overview).

These patterns imply that TachyonIQ should make the following visible in the
same response: the answer, a primary visual, secondary views, a definition of
the metric, the exact population and period, statistical checks, the SQL, raw
rows, and suggested follow-ups. The model should propose language and
interpretation; deterministic code should calculate values, tests, and chart
data.

## Current demo gap

The original executive-summary shortcut returned one aggregate row. That was
useful for a headline but it bypassed the normal result analyser and
visualization generator. The UI therefore had text and evidence metadata but
no chart, no monthly observations, and no statistical test block to render.

This was a pipeline-shape problem rather than a browser rendering problem.
The frontend already supports Vega-Lite specifications, KPI cards, a data
table, evidence, explainability, anomaly output, forecasts, and follow-up
chips; the executive path simply did not populate those fields.

## Implemented demo improvement

The executive path now runs a governed monthly scorecard query grouped at the
month grain. It returns 37 monthly rows for the current demo data, rather than
the earlier single aggregate row or the four regional rows that would have
double-counted a time-series chart.

The response now contains:

- A revenue-versus-target monthly line chart with tooltips.
- Monthly profit and customer-health views.
- KPI cards for recognized revenue, target attainment, profit margin, churn,
  and CSAT.
- Mean, median, standard deviation, volatility, minimum, maximum, period
  change, CAGR, and target-hit rate.
- An OLS trend test with p-value, R², and a 95% confidence interval for the
  slope.
- A 95% Student-t confidence interval for mean monthly revenue.
- Anomaly flags, data-quality checking, calculation steps, assumptions, and
  recommendations.
- Raw monthly rows in the Data tab, validated SQL in the SQL tab, and
  source/population details in Evidence.

The new `statistical_analysis` response field is deterministic and safe to
display. It is calculated from query results and is not invented by the LLM.
The frontend renders these statistics in the Insights drawer while preserving
the existing conversational answer.

The chart renderer paints a browser-side SVG fallback immediately. If
Vega-Lite is blocked by a CDN, a browser extension, or a malformed third-party
spec, the user still sees the same inline data as a lightweight line or bar
chart instead of an empty chart frame.

Forecast suggestions now use the same governed monthly scorecard. A request
such as “forecast revenue for the next four months” returns a model method,
RMSE, point forecasts, 95% confidence bounds, a forecast chart, and the
historical rows used to fit it. This shortcut is applied before the generic
out-of-scope response so a concise forecast request remains usable with both
the test model and a conservative provider classifier.

Forecast-specific statistics are rendered separately from descriptive
scorecard statistics: historical sample size, horizon, latest actual, first
forecast, change versus the latest actual, RMSE, and confidence level are shown
instead of empty mean/median fields.

## Recommended product shape

For the demo, the interaction should follow this sequence:

1. Resolve the question into metric, dimensions, filters, and time window.
2. Show the active context in the top bar before the result appears.
3. Run a validated, read-only query against the semantic layer.
4. Calculate descriptive statistics and tests over the returned population.
5. Select a primary visual based on result shape and add two or three useful
   secondary views.
6. Present the headline, findings, chart, quality badge, evidence, SQL, and
   follow-up chips as one answer object.
7. Keep follow-ups stateful: “break that down by region” should inherit the
   metric and window, while “start over” should reset them.

High-value interactions to add next are visual cross-filtering, a “Why this
changed” driver view, chart download, CSV export, saved analysis snapshots,
and a review button that records a question, SQL, result, and user feedback.

## Delivery priorities

### P0: demo reliability

- Keep month grain explicit for every time-series response.
- Add verified query fixtures for the six to ten questions used in the client
  walkthrough.
- Return a clear clarification question when a period or metric is genuinely
  ambiguous.
- Show a visible “data is observational” note for correlation and trend
  results.

### P1: interaction quality

- Add a chart toolbar for download, copy data, and “ask about this chart”.
- Make every finding clickable so it activates the relevant filter or view.
- Add comparison controls for current period, previous period, and year over
  year.
- Add a compact analysis progress state showing intent, query, validation, and
  visualization stages.

### P2: analytical depth

- Add driver decomposition for revenue and margin changes by region, product,
  customer segment, and channel.
- Add cohort retention, funnel conversion, contribution share, Pareto, and
  anomaly drill-down views.
- Add forecast intervals and backtesting metrics instead of a point forecast
  alone.
- Add statistical guardrails for sample size, multiple comparisons, missing
  periods, and seasonality.

### P3: enterprise trust

- Store verified questions and approved SQL in the semantic layer.
- Add reviewer workflows for incorrect answers and a regression evaluation set.
- Enforce row and column policies in every generated query and display the
  effective scope in Evidence.
- Track latency, quality score, clarification rate, chart render failures, and
  user feedback.

## Acceptance criteria for the demo

The client walkthrough is ready when each recommended question produces:

- a natural-language answer that names the metric and period;
- at least one meaningful chart when there are multiple observations;
- at least two verifiable statistics or a clearly stated reason they do not
  apply;
- visible SQL, source tables, row count, quality result, assumptions, and raw
  data;
- three useful follow-up questions;
- no unsupported causal claim; and
- a stable result after a browser refresh or repeated request.
