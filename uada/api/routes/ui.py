"""
Minimal browser UI for manually exercising the pipeline (not one of the
15 specified phases -- CLAUDE.md's API phase only defines JSON
endpoints). VegaLiteSpec's own docstring says its `spec` is "sent
directly to the frontend for rendering -- no server-side rendering",
but nothing renders it yet. This is a single static page, served from
the same FastAPI app so browser fetch() calls to /query stay
same-origin (no CORS setup needed): a question box that calls /query
and renders the returned Vega-Lite spec with vega-embed, for manual
testing only -- not a production frontend.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>UADA</title>
<script src="https://cdn.jsdelivr.net/npm/vega@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6"></script>
<style>
  body { font-family: system-ui, sans-serif; max-width: 780px; margin: 2rem auto; color: #1a1a1a; }
  #question { width: 100%; padding: 0.6rem; font-size: 1rem; box-sizing: border-box; }
  button { padding: 0.6rem 1.2rem; font-size: 1rem; margin-top: 0.5rem; cursor: pointer; }
  #answer { font-size: 1.15rem; margin: 1rem 0; }
  #keyFinding { color: #555; }
  #chart { margin-top: 1rem; }
  #sql { background: #f4f4f4; padding: 0.75rem; border-radius: 4px; overflow-x: auto;
         font-family: monospace; font-size: 0.85rem; white-space: pre-wrap; }
  #error { color: #b00020; }
  .muted { color: #888; font-size: 0.9rem; }
</style>
</head>
<body>
  <h2>UADA</h2>
  <input id="question" placeholder="Ask a question, e.g. Show monthly revenue for the last 6 months"
         value="Show monthly revenue for the last 6 months">
  <div>
    <button id="ask">Ask</button>
    <span class="muted" id="status"></span>
  </div>
  <div id="answer"></div>
  <div id="keyFinding"></div>
  <div id="chart"></div>
  <div id="error"></div>
  <details id="sqlBox" style="margin-top:1rem;">
    <summary class="muted">SQL</summary>
    <div id="sql"></div>
  </details>

<script>
let sessionId = null;

async function ask() {
  const question = document.getElementById('question').value.trim();
  if (!question) return;
  const status = document.getElementById('status');
  const answerEl = document.getElementById('answer');
  const keyFindingEl = document.getElementById('keyFinding');
  const chartEl = document.getElementById('chart');
  const errorEl = document.getElementById('error');
  const sqlEl = document.getElementById('sql');

  status.textContent = 'Thinking...';
  answerEl.textContent = '';
  keyFindingEl.textContent = '';
  chartEl.innerHTML = '';
  errorEl.textContent = '';
  sqlEl.textContent = '';

  try {
    const resp = await fetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, session_id: sessionId }),
    });
    const data = await resp.json();
    status.textContent = '';
    sessionId = data.session_id;

    if (data.error) {
      errorEl.textContent = data.error.user_message || data.error.message || 'Something went wrong.';
      return;
    }

    answerEl.textContent = data.answer || '';
    keyFindingEl.textContent = data.key_finding ? ('Key finding: ' + data.key_finding) : '';
    if (data.sql) sqlEl.textContent = data.sql;

    const viz = data.visualisation;
    if (viz && viz.spec) {
      vegaEmbed('#chart', viz.spec, { actions: false });
    } else if (viz && viz.reason) {
      chartEl.textContent = 'No chart: ' + viz.reason;
    }
  } catch (e) {
    status.textContent = '';
    errorEl.textContent = 'Request failed: ' + e;
  }
}

document.getElementById('ask').addEventListener('click', ask);
document.getElementById('question').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') ask();
});
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui() -> str:
    """Serve the manual-testing UI. Not part of the JSON API contract."""
    return _PAGE
