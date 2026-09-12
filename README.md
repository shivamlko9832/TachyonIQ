# UADA — Unified Analytics Data Agent

Conversational analytics platform. Connect it to an existing database, ask
analytical questions in plain English, get back SQL-backed answers with charts.

See `CLAUDE.md` for the full architecture, phased implementation plan, and
security rules. Start with:

```bash
pip install -e ".[dev]"
pytest tests/unit/test_models.py tests/unit/test_sql_validator.py -v
```

Implementation proceeds phase by phase per `CLAUDE.md` — each phase must have
passing tests before the next one starts.

## Deterministic enterprise demo

The local demo warehouse is reproducible and contains 221,000+ rows across
revenue, profitability, customers, products, account health, customer service,
marketing and executive targets. Its engineered business signals are paired
with executable proof queries and statistical acceptance checks:

```bash
python scripts/_gen_demo_db.py
python tests/evaluation/run_demo_eval.py
```

For the demo server, set `UADA_DB_URL=sqlite:///./data/demo.sqlite`. The
application automatically pairs this database with
`config/demo_semantic_context.yaml`; set `UADA_SCL_PATH` explicitly only when
using a different semantic context. Restart the application after rebuilding
the database so the semantic and retrieval indexes are refreshed.

When using OpenAI, set `OPENAI_API_KEY` and `UADA_LLM_MODEL=openai:gpt-4o` in the
same PowerShell session that starts Uvicorn. The key is read server-side and is
never sent to the browser.

The client walkthrough and expected signals are documented in
[`docs/DEMO_SCENARIOS.md`](docs/DEMO_SCENARIOS.md). The implementation audit,
security boundaries, provider research, and remaining production work are in
[`docs/TACHYONIQ_ENGINEERING_AUDIT.md`](docs/TACHYONIQ_ENGINEERING_AUDIT.md).

## Verification

Run the full regression suite with:

```bash
pytest -q
```

The security evaluation can be run independently with
`python tests/evaluation/run_security_eval.py`.
