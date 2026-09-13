# Waste-Finance Demo Runbook

## Scope and disclosure

The Monday demonstration uses **synthetic, deterministic data** generated with seed `42`.
It contains no customer records and must never be described as client data. The governed
waste domain covers 420 synthetic customers, 700 contracts, and 22,614 contract-month
finance rows from September 2023 through August 2026. The complete database has 245,562
rows across ten analytical tables.

The source of truth is `data/demo.sqlite`. `scripts/_gen_demo_db.py` recreates it, and
`data/demo_ground_truth.json` records its hash, row counts, executable proof queries, and
expected scalars. `tests/evaluation/run_demo_eval.py` independently executes those proofs
through the production SQL validator and writes `data/demo_validation_report.json`.

## Governed grain and relationships

`wm_finance_monthly` has exactly one row per waste-service contract and complete month.
It joins many-to-one to `wm_contracts`, which joins many-to-one to `wm_customers`.
The planner may traverse this declared two-hop path; it may not infer any other join.

The table includes service events, collection/disposal/recycling tons, revenue, revenue
budget, five operating-cost components, operating profit, billed invoices, cash
collections, ending AR, overdue AR, and DSO. Region and customer segment come from the
customer master. Service line and pricing model come from the contract master.

## Metric definitions

| Business term | Governed calculation | Unit | Time behavior |
|---|---|---:|---|
| Waste revenue | `SUM(revenue)` | USD | Additive |
| Revenue variance | `SUM(revenue) - SUM(revenue_budget)` | USD | Additive |
| Budget attainment | `100 * SUM(revenue) / SUM(revenue_budget)` | % | Recompute at every grouping |
| Operating cost | `SUM(operating_cost)` | USD | Additive |
| Operating profit | `SUM(operating_profit)` | USD | Additive |
| Operating margin | `100 * SUM(operating_profit) / SUM(revenue)` | % | Recompute; never average row margins |
| Revenue per ton | `SUM(revenue) / SUM(collection_volume_tons)` | USD/ton | Recompute |
| Ending AR | `SUM(ar_balance)` | USD | Add across contracts for one month; never sum across months |
| Overdue AR | `SUM(overdue_ar_balance)` | USD | Add across contracts for one month; never sum across months |
| DSO | `AVG(days_sales_outstanding)` | days | Non-additive |

## Reconciliation gates

The validation run must stop the demo release if any of these checks is non-zero:

1. Duplicate `(contract_id, month)` rows.
2. Missing month, revenue, operating cost, operating profit, or AR.
3. `operating_cost != fuel + labor + disposal + fleet + other operating cost`.
4. `operating_profit != revenue - operating_cost`.
5. `current AR != prior AR + invoices billed - cash collected` for consecutive months.

The current canonical build passes all five gates with zero violations.

## Verified demonstration facts

These values are database results, not text fixtures:

- January-August 2026 waste revenue: **$268,403,459.60**.
- January-August 2026 operating margin: **36.4705%**.
- January-August 2026 revenue variance to budget: **+$4,109,309.72**.
- West operating margin for January-August: **40.9171% in 2025** and **33.8264% in 2026**.
- West fuel cost per collected ton: **$8.81 in 2025** and **$11.66 in 2026**.
- West disposal cost per collected ton: **$25.40 in 2025** and **$31.01 in 2026**.
- Across 36 monthly observations, fuel share and operating margin have Pearson
  **r = -0.9709**. This is an association in synthetic data, not proof of causality.

## Recommended customer demonstration

Use one session and ask these in order so context handling is visible:

1. “Give me a waste-finance summary.”
2. “What was our revenue and operating margin in 2026?”
3. “Break that down by region.”
4. “Compare the West with the same months in 2025.”
5. “What is driving our margin decline?”
6. “Show the cost contribution by fuel, labor, disposal, fleet, and other cost.”
7. “Break the West result down by service line.”
8. “Which are our least profitable waste customers?”
9. “Review our waste accounts receivable.”
10. “Which customers had the largest overdue receivables in August 2026?”
11. “Forecast waste revenue for the next four months.”
12. “Show the SQL, assumptions, model diagnostics, and evidence for that forecast.”

For every answer, inspect the SQL, data, evidence, semantic metric, date window, and chart.
Call a result an observed fact only when it is present in the executed result. Describe
driver outputs as contribution or association unless a causal design is explicitly present.

## Questions that should clarify or decline

- “Why did a real customer leave?” The synthetic data has no verified causal event history.
- “What will diesel prices be next year?” No external commodity-price source is connected.
- “Which driver caused the margin decline?” The data supports contribution and association,
  not a causal conclusion.
- “Show safety performance.” The current waste schema does not contain a governed safety
  metric.
- “Give September 2026 waste actuals.” Waste actuals end in August 2026; the system should
  state that coverage boundary.

## Fair Claude plus database comparison

Use the same read-only `data/demo.sqlite`, the same twelve questions, the same fixed date
coverage, and no hidden prompt containing expected answers. Give each system schema access
and a read-only SQL tool. Record first-run output without manual repair.

Score each response on: semantic metric correctness, grain, join path, aggregation,
date boundary, SQL safety, numeric match to ground truth, statistical-method validity,
uncertainty disclosure, evidence/provenance, follow-up context, chart correctness, graceful
unsupported behavior, and latency. TachyonIQ-specific UI affordances do not count toward
numeric correctness; Claude prose quality does not excuse an unverifiable number.

## Start and acceptance commands

Backend terminal:

```powershell
cd C:\Users\Lp\Desktop\Tachyon_AI_Engineering\TachyonIQ
$env:OPENBLAS_NUM_THREADS="1"
$env:OMP_NUM_THREADS="1"
.\.venv\Scripts\python.exe -m uvicorn uada.api.app:app --host 127.0.0.1 --port 8000
```

Frontend terminal:

```powershell
cd C:\Users\Lp\Desktop\Tachyon_AI_Engineering\TachyonIQ\frontend
npm run dev -- -p 3001
```

Pre-demo verification:

```powershell
cd C:\Users\Lp\Desktop\Tachyon_AI_Engineering\TachyonIQ
.\.venv\Scripts\python.exe scripts\_gen_demo_db.py
.\.venv\Scripts\python.exe tests\evaluation\run_demo_eval.py
.\.venv\Scripts\python.exe -m pytest tests\unit tests\integration -q
```
