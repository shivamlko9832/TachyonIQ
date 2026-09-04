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
