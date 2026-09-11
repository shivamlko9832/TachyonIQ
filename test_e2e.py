"""
UADA End-to-End Integration Test
Tests full pipeline: NL question → SQL generation → execution → AI insights
"""
import sys, os, asyncio, types
import numpy as np

UADA_DIR = os.path.dirname(os.path.abspath(__file__))
# Change cwd so relative paths (./data/demo.sqlite, ./config/...) resolve correctly
os.chdir(UADA_DIR)
sys.path.insert(0, UADA_DIR)

# ── 1. Stub sentence_transformers BEFORE any uada import ──────────────────
class _FakeST:
    def __init__(self, *args, **kwargs): pass
    def encode(self, texts, batch_size=64, **kw):
        n = len(texts) if hasattr(texts, "__len__") else 1
        return np.zeros((n, 384), dtype=np.float32)

_st = types.ModuleType("sentence_transformers")
_st.SentenceTransformer = _FakeST
sys.modules["sentence_transformers"] = _st

# ── 2. Load env (sets OPENAI_API_KEY, UADA_LLM_MODEL, UADA_DB_URL) ───────
from dotenv import load_dotenv
load_dotenv(os.path.join(UADA_DIR, ".env"))

if not os.environ.get("OPENAI_API_KEY"):
    print("❌  OPENAI_API_KEY not set in .env — aborting")
    sys.exit(1)

# ── 3. Import and build orchestrator ──────────────────────────────────────
from uada.config import settings
from uada.api.app import _bootstrap_orchestrator

GREEN = "\033[32m"
RED   = "\033[31m"
CYAN  = "\033[36m"
BOLD  = "\033[1m"
RESET = "\033[0m"

TESTS = [
    ("Total revenue",    "What is the total revenue?"),
    ("Revenue by region","Show me revenue by region"),
    ("Top category",     "Which product category generates the most revenue?"),
    ("Customer count",   "How many customers do we have?"),
]

async def run_test(orch, name, question):
    print(f"\n{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}▶ {name}{RESET}")
    print(f"  Q: {question}")
    try:
        result = await orch.run(question=question, session_id="e2e-test")

        if result.error:
            print(f"  {RED}✗ Error: {result.error}{RESET}")
            return False

        print(f"  {GREEN}✓ SQL:{RESET} {result.sql_query}")
        if result.data:
            rows = result.data.rows or []
            cols = result.data.columns or []
            print(f"  {GREEN}✓ Rows:{RESET} {len(rows)}  Cols: {cols}")
            for row in rows[:3]:
                print(f"    {row}")
        if result.summary:
            print(f"  {GREEN}✓ Summary:{RESET} {result.summary[:250]}")
        if result.data_quality:
            dq = result.data_quality
            print(f"  {GREEN}✓ DQ completeness:{RESET} {getattr(dq,'completeness_score','-')}")
        if result.generated_insights:
            gi = result.generated_insights
            findings = getattr(gi, 'key_findings', [])
            print(f"  {GREEN}✓ Insights:{RESET} {len(findings)} finding(s)")
        return True

    except Exception as exc:
        import traceback
        print(f"  {RED}✗ Exception: {exc}{RESET}")
        traceback.print_exc()
        return False

async def main():
    print(f"\n{BOLD}{'='*60}")
    print("  UADA End-to-End Integration Test")
    print(f"{'='*60}{RESET}")
    print(f"  DB  : {settings.db_url.get_secret_value()}")
    print(f"  LLM : {settings.llm_model}")

    print("\nBuilding orchestrator …")
    try:
        orch = _bootstrap_orchestrator(settings)
    except Exception as exc:
        import traceback
        print(f"{RED}Failed to build orchestrator: {exc}{RESET}")
        traceback.print_exc()
        sys.exit(1)

    print("Orchestrator ready.")

    passed = 0
    for name, question in TESTS:
        ok = await run_test(orch, name, question)
        if ok:
            passed += 1

    print(f"\n{BOLD}{'='*60}")
    color = GREEN if passed == len(TESTS) else RED
    print(f"  Result: {color}{passed}/{len(TESTS)} tests passed{RESET}{BOLD}")
    print(f"{'='*60}{RESET}\n")

if __name__ == "__main__":
    asyncio.run(main())
