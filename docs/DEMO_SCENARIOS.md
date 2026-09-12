# TachyonIQ evidence-backed enterprise demo

The synthetic demo warehouse contains 221,000+ rows across seven analytical tables,
three years of monthly history, 2,500 customer accounts, 54 products, approximately
59,000 orders, 100,000 support tickets, 58,000 account-health observations, marketing
performance, and a governed executive scorecard.

The fixed as-of date is **2026-09-01**. Use explicit dates during formal demonstrations
so results remain reproducible. Natural-language coverage is broad but finite: unsupported
definitions must produce clarification rather than a fabricated answer.

## Build and prove the data

From the project directory:

```powershell
.\.venv\Scripts\python.exe scripts\_gen_demo_db.py
.\.venv\Scripts\python.exe tests\evaluation\run_demo_eval.py
```

The first command writes `data/demo.sqlite` and `data/demo_ground_truth.json`. The
second re-executes every proof query through the production SQL validator and writes
`data/demo_validation_report.json`. A demo is accepted only when the report says
`"passed": true`.

## Executive opening

| Natural-language question | Expected experience and proof |
| --- | --- |
| “Give me an executive summary of 2026 performance by region.” | Revenue, profit, target attainment, customers, marketing and CSAT from `business_kpi_monthly`; Europe appears below target. |
| “Which regions are below revenue target this year?” | Three regions are below target in the deterministic fixture; show actual, target, variance and attainment. |
| “What changed most compared with the previous six months?” | A bounded comparison with explicit dates and evidence rows. |
| “Where should leadership focus next?” | Recommendations must be labelled as recommendations and tied to the measured gaps. |

## Revenue and profitability

1. “What is recognized revenue and profit by month for the last 12 months?”
2. “Which product categories have the highest revenue and gross margin?”
3. “Show revenue, profit and margin by region and customer tier.”
4. “Which sales representatives generated the most profit in 2026?”
5. “How does discount rate vary by region and tier?”
6. “Which ten customers have the highest recognized revenue and profit?”
7. “What is average order value by sales channel?”
8. “What is the refund rate by product category?”
9. “What is the on-time delivery rate by region?”
10. “What caused the APAC revenue spike in August 2026?”

The APAC spike is backed by one controlled USD 202,500 recognized order. Its z-score
within APAC August orders is greater than 17, making it an obvious statistical anomaly.

## Customer growth, usage and retention

1. “How many customer accounts exist and how many have purchased?”
2. “Show MRR and monthly active users by plan tier over the last year.”
3. “How many accounts are high risk in September 2026?”
4. “Which industries contain the most high-risk Enterprise accounts?”
5. “Does low feature adoption coincide with higher churn risk?”
6. “Compare NPS, adoption and support burden across customer segments.”
7. “Which accounts contracted MRR while usage also declined?”
8. “Show new and churned accounts by month and region.”
9. “Which acquisition channels produced the highest current contract value?”

The fixture contains 57,860 customer-month observations. Feature adoption and churn
risk have Pearson correlation approximately **-0.56**. Low-adoption accounts have an
average risk near **66.6**, versus **42.4** for high-adoption accounts, with Cohen's
effect size around **1.77**. These are associations created for demonstration and do
not establish real-world causality.

## Customer service

1. “Show ticket volume, CSAT and resolution time by priority.”
2. “Which support categories take longest to resolve?”
3. “Which regions have the highest escalation and reopening rates?”
4. “Are slower resolutions associated with lower CSAT?”
5. “Show critical-ticket volume and response time by month.”
6. “Which high-value accounts have poor CSAT and high churn risk?”

The validation set contains approximately 80,000 closed tickets with CSAT responses.
Resolution time and CSAT have Pearson correlation approximately **-0.66**.

## Marketing and funnel performance

1. “Which marketing channels have the strongest return on ad spend?”
2. “Show impressions, clicks, leads and conversions by region.”
3. “How did Secure Growth perform in APAC?”
4. “Which campaigns combine high spend with low conversion rate?”
5. “Show monthly marketing spend and attributed revenue for the last year.”
6. “Compare channel CTR, lead conversion and ROAS.”

The deterministic APAC Secure Growth campaign returns **5.92x ROAS**, providing a
credible positive-growth example without relying on implausible marketing economics.

## Controlled diagnostic story

Use this sequence in one fresh conversation:

1. “Compare Europe's recognized revenue for March through August 2026 with September
   2025 through February 2026.”
2. “Break the change down by product category.”
3. “Now break it down by customer tier.”
4. “Was the decline driven more by order volume or average order value?”
5. “Which customers contributed most to the decrease?”
6. “Summarize the evidence, assumptions and recommended follow-up.”

The verified totals show Europe falling from about USD 14.05 million to USD 8.48
million, a **39.68% decline**. Follow-up answers must retain both date windows and the
recognized-order population.

## Acceptance rules

- Show the generated SQL, returned rows, filters, date boundaries and calculation steps.
- Distinguish customer accounts from purchasing customers and active product users.
- Do not sum distinct customer counts, averages, percentages, ratios, MRR across months,
  or other non-additive measures.
- Treat correlations and drivers as associations or hypotheses unless a deterministic
  decomposition directly establishes the arithmetic contribution.
- A no-data result is valid evidence. Missing periods or unclear business terms require
  clarification.
- Use the same question set for every release and compare exact result sets, statistical
  tolerances, latency, cost and clarification quality.
