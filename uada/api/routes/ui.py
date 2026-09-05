"""
TachyonIQ Production Frontend
================================
3-panel conversational analytics UI:
  Left  — connection list + conversation history
  Center — chat (KPI cards, charts, insight text, follow-up chips)
  Right  — SQL panel, schema browser, raw data table
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TachyonIQ</title>
<script src="https://cdn.jsdelivr.net/npm/vega@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
  --accent: #6c63ff; --accent2: #00d4aa;
  --text: #e2e4ed; --muted: #7b7f96; --danger: #ff4d6a;
  --up: #00d4aa; --down: #ff4d6a;
  --sidebar-w: 240px; --right-w: 340px;
  --radius: 8px; --font: system-ui, -apple-system, sans-serif;
}
body { font-family: var(--font); background: var(--bg); color: var(--text);
       display: flex; height: 100vh; overflow: hidden; font-size: 14px; }

/* ── Sidebar ── */
#sidebar { width: var(--sidebar-w); background: var(--surface); border-right: 1px solid var(--border);
           display: flex; flex-direction: column; flex-shrink: 0; }
#sidebar-logo { padding: 16px; font-size: 18px; font-weight: 700;
                color: var(--accent); letter-spacing: -0.5px; border-bottom: 1px solid var(--border); }
#sidebar-logo span { color: var(--accent2); }
.sidebar-section { padding: 12px 16px 6px; font-size: 11px; font-weight: 600;
                   color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; }
.conn-item { display: flex; align-items: center; gap: 8px; padding: 8px 16px;
             cursor: pointer; border-radius: 0; transition: background .15s; }
.conn-item:hover { background: rgba(108,99,255,.12); }
.conn-item.active { background: rgba(108,99,255,.2); }
.conn-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
.conn-dot.ok { background: var(--up); }
.conn-dot.err { background: var(--danger); }
.conn-name { font-size: 13px; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conn-badge { font-size: 10px; color: var(--muted); background: var(--border); padding: 1px 5px; border-radius: 10px; }
.sidebar-btn { margin: 8px 12px; padding: 7px; background: rgba(108,99,255,.15); border: 1px solid rgba(108,99,255,.3);
               color: var(--accent); border-radius: var(--radius); cursor: pointer; font-size: 12px;
               text-align: center; transition: background .15s; }
.sidebar-btn:hover { background: rgba(108,99,255,.28); }
.history-item { padding: 7px 16px; cursor: pointer; font-size: 12px; color: var(--muted);
                white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-radius: 0;
                transition: background .15s; }
.history-item:hover { background: rgba(255,255,255,.05); color: var(--text); }
.history-item.active { color: var(--text); background: rgba(255,255,255,.07); }
#sidebar-footer { margin-top: auto; padding: 12px 16px; border-top: 1px solid var(--border);
                  font-size: 11px; color: var(--muted); }

/* ── Main ── */
#main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
#topbar { display: flex; align-items: center; padding: 0 20px; height: 52px; border-bottom: 1px solid var(--border);
          background: var(--surface); gap: 12px; flex-shrink: 0; }
#topbar-conn { font-size: 13px; color: var(--muted); }
#topbar-conn b { color: var(--text); }
#topbar-status { font-size: 12px; color: var(--muted); margin-left: auto; }
#spinner { display: none; width: 14px; height: 14px; border: 2px solid var(--border);
           border-top-color: var(--accent); border-radius: 50%; animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

#chat-area { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
#chat-area::-webkit-scrollbar { width: 6px; }
#chat-area::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

.msg { display: flex; flex-direction: column; gap: 6px; max-width: 760px; }
.msg.user { align-self: flex-end; }
.msg-bubble { padding: 10px 14px; border-radius: var(--radius); font-size: 14px; line-height: 1.5; }
.msg.user .msg-bubble { background: rgba(108,99,255,.22); color: var(--text); }
.msg.ai .msg-bubble { background: var(--surface); border: 1px solid var(--border); }
.msg-answer { font-size: 15px; line-height: 1.6; }
.msg-finding { font-size: 13px; color: var(--muted); margin-top: 4px; }
.msg-error { color: var(--danger); font-size: 13px; }
.msg-time { font-size: 11px; color: var(--muted); align-self: flex-end; }

/* KPI Card */
.kpi-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
            padding: 20px 24px; display: inline-flex; flex-direction: column; gap: 4px;
            min-width: 180px; margin-top: 8px; }
.kpi-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
.kpi-value { font-size: 36px; font-weight: 700; letter-spacing: -1px; line-height: 1; }
.kpi-trend { font-size: 13px; margin-top: 4px; }
.kpi-trend.up { color: var(--up); }
.kpi-trend.down { color: var(--down); }
.kpi-trend.flat { color: var(--muted); }
.kpi-comp { font-size: 12px; color: var(--muted); }

/* Chart */
.chart-wrap { margin-top: 8px; border-radius: var(--radius); overflow: hidden; background: var(--surface);
              border: 1px solid var(--border); padding: 12px; }

/* Chips */
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip { padding: 5px 11px; border: 1px solid var(--border); border-radius: 20px; font-size: 12px;
        color: var(--muted); cursor: pointer; transition: all .15s; background: transparent; }
.chip:hover { border-color: var(--accent); color: var(--text); background: rgba(108,99,255,.1); }

/* Input */
#input-area { padding: 14px 20px; border-top: 1px solid var(--border); background: var(--surface); flex-shrink: 0; }
#input-row { display: flex; gap: 10px; align-items: flex-end; }
#question { flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
            color: var(--text); padding: 10px 14px; font-size: 14px; resize: none; min-height: 44px;
            max-height: 120px; font-family: var(--font); line-height: 1.4; }
#question:focus { outline: none; border-color: var(--accent); }
#send-btn { padding: 10px 18px; background: var(--accent); border: none; border-radius: var(--radius);
            color: #fff; cursor: pointer; font-size: 14px; font-weight: 600; transition: opacity .15s;
            flex-shrink: 0; height: 44px; }
#send-btn:hover { opacity: .88; }
#send-btn:disabled { opacity: .4; cursor: not-allowed; }

/* ── Right panel ── */
#right { width: var(--right-w); background: var(--surface); border-left: 1px solid var(--border);
         display: flex; flex-direction: column; flex-shrink: 0; }
.panel-tabs { display: flex; border-bottom: 1px solid var(--border); }
.panel-tab { flex: 1; padding: 12px 0; text-align: center; font-size: 12px; font-weight: 600;
             color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent;
             transition: color .15s; }
.panel-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.panel-body { flex: 1; overflow-y: auto; padding: 14px; }
.panel-body::-webkit-scrollbar { width: 5px; }
.panel-body::-webkit-scrollbar-thumb { background: var(--border); }
.panel-pane { display: none; }
.panel-pane.active { display: block; }

/* SQL pane */
#sql-actions { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.icon-btn { padding: 4px 10px; background: transparent; border: 1px solid var(--border); border-radius: 4px;
            color: var(--muted); cursor: pointer; font-size: 11px; transition: all .15s; }
.icon-btn:hover { border-color: var(--accent); color: var(--accent); }
#sql-block { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
             padding: 12px; font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px;
             line-height: 1.6; color: #a9b1d6; white-space: pre-wrap; word-break: break-all; }
.sql-kw { color: #bb9af7; font-weight: 600; }
.sql-fn { color: #7dcfff; }
.sql-str { color: #9ece6a; }
.sql-num { color: #ff9e64; }

/* Data table */
#data-table-wrap { overflow-x: auto; }
table.data-tbl { width: 100%; border-collapse: collapse; font-size: 11px; }
table.data-tbl th { background: var(--bg); padding: 6px 8px; text-align: left; color: var(--muted);
                    border-bottom: 1px solid var(--border); white-space: nowrap; font-weight: 600; }
table.data-tbl td { padding: 5px 8px; border-bottom: 1px solid rgba(42,45,58,.5);
                    white-space: nowrap; max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
table.data-tbl tr:hover td { background: rgba(255,255,255,.03); }

/* Schema pane */
.schema-table { margin-bottom: 12px; }
.schema-table-name { font-size: 12px; font-weight: 600; color: var(--accent); margin-bottom: 4px;
                     display: flex; align-items: center; gap: 6px; cursor: pointer; }
.schema-cols { display: none; padding-left: 12px; }
.schema-cols.open { display: block; }
.schema-col { font-size: 11px; color: var(--muted); padding: 2px 0; display: flex; justify-content: space-between; }
.schema-col-type { color: var(--border); font-size: 10px; }
.empty-state { color: var(--muted); font-size: 13px; text-align: center; padding: 30px 0; }

/* ── Connection modal ── */
#modal-backdrop { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.65); z-index: 100;
                  align-items: center; justify-content: center; }
#modal-backdrop.open { display: flex; }
#modal { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
         width: 440px; padding: 24px; }
#modal h3 { font-size: 16px; font-weight: 700; margin-bottom: 18px; }
.form-row { margin-bottom: 12px; }
.form-row label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.form-row input, .form-row select { width: 100%; background: var(--bg); border: 1px solid var(--border);
  border-radius: var(--radius); color: var(--text); padding: 8px 10px; font-size: 13px; }
.form-row input:focus, .form-row select:focus { outline: none; border-color: var(--accent); }
.form-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.modal-actions { display: flex; gap: 10px; margin-top: 18px; justify-content: flex-end; }
.btn-cancel { padding: 8px 16px; background: transparent; border: 1px solid var(--border);
              color: var(--muted); border-radius: var(--radius); cursor: pointer; font-size: 13px; }
.btn-cancel:hover { border-color: var(--text); color: var(--text); }
.btn-primary { padding: 8px 16px; background: var(--accent); border: none; color: #fff;
               border-radius: var(--radius); cursor: pointer; font-size: 13px; font-weight: 600; }
.btn-primary:hover { opacity: .88; }
#modal-status { font-size: 12px; margin-top: 10px; min-height: 18px; }
#modal-status.ok { color: var(--up); }
#modal-status.err { color: var(--danger); }
/* ── Data Quality ── */
.dq-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
            border-radius: 20px; font-size: 12px; font-weight: 600; margin-top: 8px; cursor: default; }
.dq-badge.dq-high { background: rgba(0,212,170,.15); color: var(--up); border: 1px solid rgba(0,212,170,.3); }
.dq-badge.dq-medium { background: rgba(255,158,100,.12); color: #ff9e64; border: 1px solid rgba(255,158,100,.3); }
.dq-badge.dq-low { background: rgba(255,77,106,.12); color: var(--danger); border: 1px solid rgba(255,77,106,.3); }
.dq-issues { margin-top: 5px; }
.dq-issue { display: flex; align-items: center; gap: 6px; padding: 2px 0; font-size: 11px; color: var(--muted); }
.dq-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.dq-dot.high { background: var(--danger); }
.dq-dot.medium { background: #ff9e64; }
.dq-dot.low { background: var(--muted); }

/* ── Explainability Accordion ── */
.ex-accordion { margin-top: 10px; border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.ex-header { padding: 8px 12px; background: rgba(255,255,255,.03); font-size: 12px; font-weight: 600;
             color: var(--muted); cursor: pointer; display: flex; align-items: center;
             justify-content: space-between; user-select: none; transition: background .15s; }
.ex-header:hover { background: rgba(108,99,255,.1); color: var(--text); }
.ex-body { display: none; padding: 10px 12px; font-size: 12px; line-height: 1.7; color: var(--muted);
           border-top: 1px solid var(--border); }
.ex-body.open { display: block; }
.ex-sec { margin-bottom: 8px; }
.ex-sec:last-child { margin-bottom: 0; }
.ex-sec-lbl { font-size: 10px; font-weight: 700; color: var(--text); text-transform: uppercase;
              letter-spacing: 0.5px; margin-bottom: 3px; }
.ex-step::before { content: "→ "; color: var(--accent); }

/* ── Generated Insights ── */
.insights-panel { margin-top: 10px; background: rgba(108,99,255,.06); border: 1px solid rgba(108,99,255,.22);
                  border-radius: var(--radius); padding: 12px 14px; }
.insights-hdr { font-size: 10px; font-weight: 700; color: var(--accent); text-transform: uppercase;
                letter-spacing: 0.6px; margin-bottom: 8px; }
.insights-sec { margin-bottom: 8px; }
.insights-sec:last-child { margin-bottom: 0; }
.insights-sec-lbl { font-size: 10px; font-weight: 600; color: var(--muted); margin-bottom: 3px;
                    text-transform: uppercase; letter-spacing: 0.4px; }
.insights-item { font-size: 12px; line-height: 1.5; color: var(--text); padding: 2px 0; }
.insights-item::before { content: "• "; color: var(--accent2); }
.insights-conf { font-size: 10px; color: var(--muted); margin-top: 6px; font-style: italic; }

/* ── Anomalies ── */
.anomaly-badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; margin-top: 8px;
                 border-radius: 12px; font-size: 11px; font-weight: 600;
                 background: rgba(255,77,106,.12); color: var(--danger); border: 1px solid rgba(255,77,106,.3); }
.anomaly-row { margin-top: 4px; font-size: 11px; color: var(--muted); padding: 4px 8px;
               background: rgba(255,77,106,.05); border-radius: 4px; border-left: 2px solid var(--danger); }

/* ── Correlation ── */
.corr-wrap { margin-top: 8px; }
.corr-lbl-row { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
.corr-pair { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 11px; }
.corr-names { min-width: 130px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text); }
.corr-bar-bg { flex: 1; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
.corr-bar { height: 100%; border-radius: 2px; }
.corr-bar.pos { background: var(--up); }
.corr-bar.neg { background: var(--danger); }
.corr-val { min-width: 34px; text-align: right; color: var(--muted); }

</style>
</head>
<body>

<!-- ── Sidebar ── -->
<nav id="sidebar">
  <div id="sidebar-logo">Tachyon<span>IQ</span></div>
  <div class="sidebar-section">Connections</div>
  <div id="conn-list"></div>
  <div class="sidebar-btn" id="add-conn-btn">+ Add Connection</div>
  <div class="sidebar-section" style="margin-top:8px;">History</div>
  <div id="history-list" style="flex:1; overflow-y:auto;"></div>
  <div id="sidebar-footer">v0.1 · TachyonIQ</div>
</nav>

<!-- ── Main ── -->
<main id="main">
  <div id="topbar">
    <div id="topbar-conn"><b id="active-conn-name">No connection</b></div>
    <div id="topbar-status"><span id="spinner"></span><span id="status-text"></span></div>
  </div>
  <div id="chat-area">
    <div style="text-align:center; padding: 60px 20px;">
      <div style="font-size:32px; font-weight:700; color:var(--muted); letter-spacing:-1px;">Ask anything about your data</div>
      <div style="color:var(--muted); margin-top:10px; font-size:14px;">Connect a database and start a conversation</div>
    </div>
  </div>
  <div id="input-area">
    <div id="input-row">
      <textarea id="question" rows="1" placeholder="Ask a question about your data…"></textarea>
      <button id="send-btn">Send</button>
    </div>
  </div>
</main>

<!-- ── Right Panel ── -->
<aside id="right">
  <div class="panel-tabs">
    <div class="panel-tab active" data-pane="sql">SQL</div>
    <div class="panel-tab" data-pane="schema">Schema</div>
    <div class="panel-tab" data-pane="data">Data</div>
  </div>
  <div class="panel-body">
    <div class="panel-pane active" id="pane-sql">
      <div id="sql-actions"><button class="icon-btn" id="copy-sql-btn">Copy</button></div>
      <div id="sql-block"><span class="empty-state" style="padding:0;">Run a query to see SQL</span></div>
    </div>
    <div class="panel-pane" id="pane-schema">
      <div id="schema-content"><div class="empty-state">Select a connection to browse schema</div></div>
    </div>
    <div class="panel-pane" id="pane-data">
      <div id="data-table-wrap"><div class="empty-state">Run a query to see raw data</div></div>
    </div>
  </div>
</aside>

<!-- ── Connection Modal ── -->
<div id="modal-backdrop">
  <div id="modal">
    <h3>Add Database Connection</h3>
    <div class="form-row">
      <label>Connection Name</label>
      <input id="f-name" placeholder="My Analytics DB" value="">
    </div>
    <div class="form-row">
      <label>Dialect</label>
      <select id="f-dialect">
        <option value="postgresql">PostgreSQL</option>
        <option value="mysql">MySQL</option>
        <option value="sqlite">SQLite</option>
        <option value="mssql">SQL Server</option>
        <option value="duckdb">DuckDB</option>
        <option value="snowflake">Snowflake</option>
        <option value="bigquery">BigQuery</option>
        <option value="redshift">Redshift</option>
      </select>
    </div>
    <div class="form-2col">
      <div class="form-row"><label>Host</label><input id="f-host" placeholder="localhost"></div>
      <div class="form-row"><label>Port</label><input id="f-port" placeholder="5432" type="number"></div>
    </div>
    <div class="form-row"><label>Database / File Path</label><input id="f-database" placeholder="mydb"></div>
    <div class="form-2col">
      <div class="form-row"><label>Username</label><input id="f-username" placeholder="user"></div>
      <div class="form-row"><label>Password</label><input id="f-password" type="password" placeholder="••••••••"></div>
    </div>
    <div id="modal-status"></div>
    <div class="modal-actions">
      <button class="btn-cancel" id="modal-cancel">Cancel</button>
      <button class="btn-primary" id="modal-test">Test Connection</button>
      <button class="btn-primary" id="modal-save">Connect</button>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
const state = {
  sessionId: null,
  activeConnId: null,
  connections: [],
  sessions: [],
  lastSql: '',
  lastData: null,
};

// ── Utils ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const now = () => new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

function highlightSql(sql) {
  const kws = /\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP BY|ORDER BY|HAVING|LIMIT|OFFSET|WITH|AS|AND|OR|NOT|IN|IS|NULL|BETWEEN|LIKE|CASE|WHEN|THEN|ELSE|END|DISTINCT|COUNT|SUM|AVG|MIN|MAX|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TABLE|INDEX|VIEW)\b/gi;
  const fns = /\b(COALESCE|NULLIF|CAST|CONVERT|DATE|YEAR|MONTH|DAY|NOW|CURRENT_DATE|CURRENT_TIMESTAMP|ROUND|FLOOR|CEIL|ABS|LENGTH|UPPER|LOWER|TRIM|CONCAT|SUBSTRING|EXTRACT|DATEDIFF|DATEADD|IFNULL|IIF|ROW_NUMBER|RANK|DENSE_RANK|LAG|LEAD|OVER|PARTITION BY)\b/gi;
  const escaped = sql.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return escaped
    .replace(kws, m => `<span class="sql-kw">${m.toUpperCase()}</span>`)
    .replace(fns, m => `<span class="sql-fn">${m}</span>`)
    .replace(/'([^']*)'/g, `<span class="sql-str">'$1'</span>`)
    .replace(/\b(\d+(\.\d+)?)\b/g, '<span class="sql-num">$1</span>');
}

function setStatus(msg, spin=false) {
  $('status-text').textContent = msg;
  $('spinner').style.display = spin ? 'inline-block' : 'none';
}

// ── Connections ────────────────────────────────────────────────────────────
async function loadConnections() {
  try {
    const r = await fetch('/connections');
    if (!r.ok) return;
    const d = await r.json();
    state.connections = d.connections || [];
    renderConnList();
  } catch(e) { /* server may not be ready */ }
}

function renderConnList() {
  const el = $('conn-list');
  el.innerHTML = '';
  if (!state.connections.length) {
    el.innerHTML = '<div style="padding:8px 16px;font-size:12px;color:var(--muted)">No connections yet</div>';
    return;
  }
  state.connections.forEach(c => {
    const div = document.createElement('div');
    div.className = 'conn-item' + (c.connection_id === state.activeConnId ? ' active' : '');
    div.innerHTML = `<div class="conn-dot ${c.last_test_ok === true ? 'ok' : c.last_test_ok === false ? 'err' : ''}"></div>
      <div class="conn-name" title="${c.name}">${c.name}</div>
      <div class="conn-badge">${c.dialect}</div>`;
    div.addEventListener('click', () => setActiveConn(c));
    el.appendChild(div);
  });
}

function setActiveConn(c) {
  state.activeConnId = c.connection_id;
  $('active-conn-name').textContent = c.name;
  renderConnList();
  loadSchema(c.connection_id);
  clearChat();
}

async function loadSchema(connId) {
  try {
    const r = await fetch(`/connections/${connId}/discover`);
    if (!r.ok) { $('schema-content').innerHTML = '<div class="empty-state">Could not load schema</div>'; return; }
    const d = await r.json();
    $('schema-content').innerHTML = `<div style="font-size:12px;color:var(--muted);margin-bottom:10px">
      ${d.table_count} tables · ${d.column_count} columns · ${d.relationship_count} relationships
    </div><div class="empty-state">Schema explorer coming in P1</div>`;
  } catch(e) {}
}

// ── Modal ──────────────────────────────────────────────────────────────────
$('add-conn-btn').addEventListener('click', () => {
  $('modal-backdrop').classList.add('open');
  $('modal-status').textContent = '';
});
$('modal-cancel').addEventListener('click', () => $('modal-backdrop').classList.remove('open'));
$('modal-backdrop').addEventListener('click', e => { if(e.target === $('modal-backdrop')) $('modal-backdrop').classList.remove('open'); });

function modalFormData() {
  return {
    name: $('f-name').value.trim(),
    dialect: $('f-dialect').value,
    host: $('f-host').value.trim() || '',
    port: parseInt($('f-port').value) || 5432,
    database: $('f-database').value.trim(),
    username: $('f-username').value.trim(),
    password: $('f-password').value,
    test_on_create: false,
  };
}

$('modal-test').addEventListener('click', async () => {
  const ms = $('modal-status');
  ms.className = ''; ms.textContent = 'Testing…';
  const body = {...modalFormData(), test_on_create: true};
  try {
    const r = await fetch('/connections', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json();
    if (d.test_result && d.test_result.ok) {
      ms.className = 'ok'; ms.textContent = `✓ Connected (${d.test_result.latency_ms?.toFixed(0)}ms) — ${d.test_result.server_version || ''}`;
      // delete the test connection
      await fetch(`/connections/${d.connection_id}`, {method:'DELETE'});
    } else {
      ms.className = 'err'; ms.textContent = '✗ ' + (d.test_result?.error || 'Connection failed');
    }
  } catch(e) { ms.className = 'err'; ms.textContent = '✗ ' + e.message; }
});

$('modal-save').addEventListener('click', async () => {
  const ms = $('modal-status');
  ms.className = ''; ms.textContent = 'Connecting…';
  try {
    const r = await fetch('/connections', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(modalFormData())});
    const d = await r.json();
    if (r.ok) {
      ms.className = 'ok'; ms.textContent = '✓ Connected!';
      await loadConnections();
      const conn = state.connections.find(c => c.connection_id === d.connection_id);
      if (conn) setActiveConn(conn);
      setTimeout(() => $('modal-backdrop').classList.remove('open'), 800);
    } else {
      ms.className = 'err'; ms.textContent = '✗ ' + (d.detail || 'Failed');
    }
  } catch(e) { ms.className = 'err'; ms.textContent = '✗ ' + e.message; }
});

// auto-fill port on dialect change
$('f-dialect').addEventListener('change', () => {
  const defaults = {postgresql:5432, mysql:3306, mssql:1433, redshift:5439, snowflake:443, bigquery:0, sqlite:0, duckdb:0};
  const p = defaults[$('f-dialect').value];
  if (p !== undefined) $('f-port').value = p || '';
});

// ── Chat ───────────────────────────────────────────────────────────────────
function clearChat() {
  $('chat-area').innerHTML = '';
  state.sessionId = null;
  state.lastSql = '';
  state.lastData = null;
  $('sql-block').innerHTML = '<span class="empty-state" style="padding:0;">Run a query to see SQL</span>';
  $('data-table-wrap').innerHTML = '<div class="empty-state">Run a query to see raw data</div>';
}

function appendMsg(role, content) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  wrap.innerHTML = content;
  $('chat-area').appendChild(wrap);
  $('chat-area').scrollTop = $('chat-area').scrollHeight;
  return wrap;
}

function renderKpi(viz) {
  const arrow = viz.trend_direction === 'up' ? '↑' : viz.trend_direction === 'down' ? '↓' : '→';
  const cls = viz.trend_direction || 'flat';
  const trendHtml = viz.trend_pct != null
    ? `<div class="kpi-trend ${cls}">${arrow} ${Math.abs(viz.trend_pct).toFixed(1)}% ${viz.comparison_label || ''}</div>`
    : '';
  return `<div class="kpi-card">
    <div class="kpi-label">${viz.label || ''}</div>
    <div class="kpi-value">${viz.formatted_value || viz.value}</div>
    ${trendHtml}
  </div>`;
}

let chartIdCounter = 0;
function renderChart(viz) {
  const id = 'chart-' + (++chartIdCounter);
  setTimeout(() => {
    const el = document.getElementById(id);
    if (el && viz.spec) vegaEmbed('#' + id, viz.spec, {actions: false, theme: 'dark', renderer: 'svg'}).catch(()=>{});
  }, 50);
  return `<div class="chart-wrap"><div id="${id}"></div></div>`;
}

function renderChips(questions) {
  if (!questions || !questions.length) return '';
  const chips = questions.map(q =>
    `<button class="chip" onclick="sendQuestion(${JSON.stringify(q)})">${q}</button>`
  ).join('');
  return `<div class="chips">${chips}</div>`;
}

// ── Data Quality ────────────────────────────────────────────────────────────
function renderDataQuality(dq) {
  if (!dq || dq.skipped) return '';
  const score = dq.overall_quality_score != null ? dq.overall_quality_score : 100;
  const cls = score >= 80 ? 'dq-high' : score >= 50 ? 'dq-medium' : 'dq-low';
  const icon = score >= 80 ? '✓' : score >= 50 ? '⚠' : '✗';
  let h = `<div class="dq-badge ${cls}">${icon} Data Quality: ${score.toFixed(0)}%</div>`;
  if (dq.summary) h += `<div style="font-size:11px;color:var(--muted);margin-top:3px">${dq.summary}</div>`;
  if (dq.issues && dq.issues.length) {
    h += '<div class="dq-issues">' + dq.issues.slice(0,4).map(i =>
      `<div class="dq-issue"><div class="dq-dot ${i.severity}"></div><span><b>${i.column}</b>: ${i.detail || i.issue_type}</span></div>`
    ).join('') + '</div>';
  }
  return h;
}

// ── Explainability ──────────────────────────────────────────────────────────
let _exId = 0;
function renderExplainability(ex) {
  if (!ex) return '';
  const id = 'ex-' + (++_exId);
  let body = '';
  if (ex.calculation_steps && ex.calculation_steps.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Calculation Steps</div>${ex.calculation_steps.map(s=>`<div class="ex-step">${s}</div>`).join('')}</div>`;
  if (ex.filters_applied && ex.filters_applied.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Filters</div><div>${ex.filters_applied.join(', ')}</div></div>`;
  if (ex.aggregations && ex.aggregations.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Aggregations</div><div>${ex.aggregations.join(', ')}</div></div>`;
  if (ex.tables_referenced && ex.tables_referenced.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Tables</div><div>${ex.tables_referenced.join(', ')}</div></div>`;
  if (ex.assumptions && ex.assumptions.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Assumptions</div><div>${ex.assumptions.join('; ')}</div></div>`;
  if (ex.chart_rationale)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Chart Choice</div><div>${ex.chart_rationale}</div></div>`;
  if (!body) return '';
  return `<div class="ex-accordion">
    <div class="ex-header" onclick="var b=document.getElementById('${id}');b.classList.toggle('open');this.querySelector('.ex-arr').textContent=b.classList.contains('open')?'▲':'▼'">
      <span>🔍 How this was calculated</span><span class="ex-arr">▼</span>
    </div>
    <div class="ex-body" id="${id}">${body}</div>
  </div>`;
}

// ── Generated Insights ──────────────────────────────────────────────────────
function renderInsights(ins) {
  if (!ins) return '';
  const hasSomething = (ins.key_findings && ins.key_findings.length)
    || (ins.drivers && ins.drivers.length)
    || (ins.recommendations && ins.recommendations.length);
  if (!hasSomething) return '';
  let h = '<div class="insights-panel"><div class="insights-hdr">✨ AI Insights</div>';
  if (ins.key_findings && ins.key_findings.length)
    h += `<div class="insights-sec"><div class="insights-sec-lbl">Key Findings</div>${ins.key_findings.map(f=>`<div class="insights-item">${f}</div>`).join('')}</div>`;
  if (ins.drivers && ins.drivers.length)
    h += `<div class="insights-sec"><div class="insights-sec-lbl">Drivers</div>${ins.drivers.map(d=>`<div class="insights-item">${d}</div>`).join('')}</div>`;
  if (ins.recommendations && ins.recommendations.length)
    h += `<div class="insights-sec"><div class="insights-sec-lbl">Recommendations</div>${ins.recommendations.map(r=>`<div class="insights-item">${r}</div>`).join('')}</div>`;
  if (ins.confidence)
    h += `<div class="insights-conf">Confidence: ${ins.confidence}</div>`;
  return h + '</div>';
}

// ── Forecast Sparkline ──────────────────────────────────────────────────────
function renderForecast(fc) {
  if (!fc || fc.skipped || !fc.forecast_values || !fc.forecast_values.length) return '';
  const id = 'fc-' + (++chartIdCounter);
  const vals = fc.forecast_values.slice(0, 12);
  const spec = {
    $schema: 'https://vega.github.io/schema/vega-lite/v5.json',
    width: 'container', height: 80, background: 'transparent',
    data: { values: vals.map((v, i) => ({i, v})) },
    mark: { type: 'area', line: { color: '#6c63ff' }, color: { expr: "{'gradient':'linear','stops':[{'offset':0,'color':'rgba(108,99,255,0.3)'},{'offset':1,'color':'rgba(108,99,255,0)'}],'x1':0,'y1':0,'x2':0,'y2':1}" } },
    encoding: {
      x: { field: 'i', type: 'quantitative', axis: null },
      y: { field: 'v', type: 'quantitative', axis: { labelColor: '#7b7f96', tickCount: 3, gridColor: '#2a2d3a' } }
    },
    config: { view: { stroke: null } }
  };
  setTimeout(() => {
    const el = document.getElementById(id);
    if (el) vegaEmbed('#'+id, spec, {actions:false, theme:'dark', renderer:'svg'}).catch(()=>{});
  }, 60);
  const label = fc.method ? `Forecast · ${fc.method}` : 'Forecast';
  return `<div class="chart-wrap" style="margin-top:8px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">${label} · next ${vals.length} periods</div>
    <div id="${id}"></div>
  </div>`;
}

// ── Anomaly Highlights ──────────────────────────────────────────────────────
function renderAnomalies(an) {
  if (!an || an.skipped || !an.anomaly_count) return '';
  let h = `<div class="anomaly-badge">⚠ ${an.anomaly_count} anomal${an.anomaly_count===1?'y':'ies'} detected</div>`;
  if (an.top_anomalies && an.top_anomalies.length)
    h += an.top_anomalies.slice(0,3).map(row => {
      const txt = Object.entries(row).slice(0,4).map(([k,v])=>`${k}: <b>${v}</b>`).join(' · ');
      return `<div class="anomaly-row">${txt}</div>`;
    }).join('');
  return h;
}

// ── Correlation Pairs ───────────────────────────────────────────────────────
function renderCorrelation(corr) {
  if (!corr || corr.skipped || !corr.top_pairs || !corr.top_pairs.length) return '';
  const pairs = corr.top_pairs.slice(0, 5);
  let h = '<div class="corr-wrap"><div class="corr-lbl-row">Top Correlations</div>';
  pairs.forEach(p => {
    const val = typeof p.correlation === 'number' ? p.correlation : 0;
    const pct = Math.abs(val * 100).toFixed(0);
    const cls = val >= 0 ? 'pos' : 'neg';
    h += `<div class="corr-pair">
      <div class="corr-names">${p.col_a} ↔ ${p.col_b}</div>
      <div class="corr-bar-bg"><div class="corr-bar ${cls}" style="width:${pct}%"></div></div>
      <div class="corr-val">${val>=0?'+':''}${val.toFixed(2)}</div>
    </div>`;
  });
  return h + '</div>';
}

function renderDataTable(data) {
  if (!data || !data.columns || !data.rows || !data.rows.length) return;
  const hdrs = data.columns.map(c => `<th>${c}</th>`).join('');
  const rows = data.rows.slice(0, 50).map(r =>
    '<tr>' + data.columns.map(c => `<td title="${r[c] ?? ''}">${r[c] ?? ''}</td>`).join('') + '</tr>'
  ).join('');
  $('data-table-wrap').innerHTML = `<table class="data-tbl"><thead><tr>${hdrs}</tr></thead><tbody>${rows}</tbody></table>
    ${data.rows.length > 50 ? `<div style="font-size:11px;color:var(--muted);padding:6px 0">Showing 50 of ${data.rows.length} rows</div>` : ''}`;
}

async function sendQuestion(question) {
  question = question || $('question').value.trim();
  if (!question) return;
  $('question').value = '';
  autoResize();

  // Show user bubble
  appendMsg('user', `<div class="msg-bubble">${question}</div><div class="msg-time">${now()}</div>`);
  setStatus('Thinking…', true);
  $('send-btn').disabled = true;

  const body = {question, session_id: state.sessionId};
  if (state.activeConnId) body.connection_id = state.activeConnId;

  try {
    const r = await fetch('/query', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json();
    setStatus('', false);
    $('send-btn').disabled = false;

    if (d.session_id) state.sessionId = d.session_id;

    // Update history
    addHistory(question);

    if (d.error) {
      appendMsg('ai', `<div class="msg-bubble msg-error">⚠ ${d.error.user_message || d.error.message || 'Something went wrong.'}</div>`);
      return;
    }

    // SQL panel
    if (d.sql) {
      state.lastSql = d.sql;
      $('sql-block').innerHTML = highlightSql(d.sql);
    }

    // Data panel
    if (d.query_result) {
      state.lastData = d.query_result;
      renderDataTable(d.query_result);
    }

    // Build AI message
    let html = '<div class="msg-bubble">';
    if (d.answer) html += `<div class="msg-answer">${d.answer}</div>`;
    if (d.key_finding) html += `<div class="msg-finding">💡 ${d.key_finding}</div>`;

    const viz = d.visualisation;
    if (viz) {
      if (viz.value !== undefined && viz.label !== undefined) {
        // KPI card
        html += renderKpi(viz);
      } else if (viz.spec) {
        html += renderChart(viz);
      } else if (viz.reason) {
        html += `<div style="color:var(--muted);font-size:12px;margin-top:8px">No chart: ${viz.reason}</div>`;
      }
    }

    // P4-A rich response fields
    html += renderDataQuality(d.data_quality);
    html += renderExplainability(d.explainability);
    html += renderInsights(d.generated_insights);
    html += renderForecast(d.forecast_result);
    html += renderAnomalies(d.anomaly_result);
    html += renderCorrelation(d.correlation_result);
    html += '</div>';

    // Follow-up chips
    if (d.suggested_questions && d.suggested_questions.length) {
      html += renderChips(d.suggested_questions);
    }

    html += `<div class="msg-time">${now()}</div>`;
    appendMsg('ai', html);

  } catch(e) {
    setStatus('', false);
    $('send-btn').disabled = false;
    appendMsg('ai', `<div class="msg-bubble msg-error">Request failed: ${e.message}</div>`);
  }
}

function addHistory(question) {
  const el = document.createElement('div');
  el.className = 'history-item active';
  el.textContent = question;
  el.title = question;
  // deactivate previous
  $('history-list').querySelectorAll('.history-item').forEach(e => e.classList.remove('active'));
  $('history-list').insertBefore(el, $('history-list').firstChild);
  state.sessions.push(question);
}

// ── Input auto-resize ──────────────────────────────────────────────────────
const qEl = $('question');
function autoResize() {
  qEl.style.height = 'auto';
  qEl.style.height = Math.min(qEl.scrollHeight, 120) + 'px';
}
qEl.addEventListener('input', autoResize);
qEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendQuestion(); }
});
$('send-btn').addEventListener('click', () => sendQuestion());

// ── Panel tabs ─────────────────────────────────────────────────────────────
document.querySelectorAll('.panel-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.panel-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel-pane').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    $('pane-' + tab.dataset.pane).classList.add('active');
  });
});

// ── Copy SQL ───────────────────────────────────────────────────────────────
$('copy-sql-btn').addEventListener('click', () => {
  if (!state.lastSql) return;
  navigator.clipboard.writeText(state.lastSql).then(() => {
    $('copy-sql-btn').textContent = 'Copied!';
    setTimeout(() => $('copy-sql-btn').textContent = 'Copy', 1500);
  });
});

// ── Init ───────────────────────────────────────────────────────────────────
loadConnections();
</script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui() -> str:
    """Serve the TachyonIQ production frontend."""
    return _PAGE
