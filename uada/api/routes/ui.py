"""
TachyonIQ Production Frontend
================================
3-panel conversational analytics UI:
  Left  — connection list + conversation history
  Center — chat (KPI cards, charts, insight text, follow-up chips)
  Right  — SQL panel, schema browser, raw data table
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
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
.fallback-svg { display: block; width: 100%; min-height: 180px; }
.chart-fallback { color: var(--muted); font-size: 12px; padding: 28px 12px; text-align: center; }

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

/* ── Executive workspace layer ─────────────────────────────────────────── */
:root {
  --sidebar-w: 272px;
  --right-w: 410px;
  --surface-2: #202431;
  --surface-3: #252a3a;
  --accent-soft: rgba(108,99,255,.14);
}
#sidebar { background: linear-gradient(180deg, #191c28 0%, #151821 100%); }
#sidebar-logo { padding: 20px 18px 8px; border-bottom: 0; }
.workspace-kicker { padding: 0 18px 16px; color: var(--muted); font-size: 10px; letter-spacing: 1px; font-weight: 700; }
.workspace-nav { padding: 0 10px 12px; border-bottom: 1px solid var(--border); }
.workspace-nav button { width: 100%; border: 0; background: transparent; color: var(--muted); text-align: left;
  padding: 9px 10px; border-radius: 7px; cursor: pointer; font: inherit; font-size: 12px; }
.workspace-nav button:hover, .workspace-nav button.active { background: var(--accent-soft); color: var(--text); }
.workspace-nav button span { display: inline-block; width: 22px; color: var(--accent2); }
#main { background: radial-gradient(circle at 50% -20%, rgba(108,99,255,.12), transparent 38%), var(--bg); }
#topbar { height: 68px; padding: 0 24px; background: rgba(26,29,39,.92); gap: 18px; }
#topbar-identity { display: flex; flex-direction: column; gap: 2px; min-width: 180px; }
#topbar-title { font-weight: 700; font-size: 14px; }
#topbar-subtitle { color: var(--muted); font-size: 11px; }
#context-strip { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.context-pill { color: var(--muted); background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px;
  padding: 5px 9px; font-size: 10px; white-space: nowrap; }
.context-pill strong { color: var(--text); font-weight: 600; }
#chat-area { padding: 26px 32px; gap: 18px; }
#workspace-home { max-width: 1040px; width: 100%; margin: 0 auto; }
.home-eyebrow { color: var(--accent2); text-transform: uppercase; letter-spacing: 1.2px; font-size: 10px; font-weight: 700; }
.home-title { font-size: 30px; line-height: 1.15; letter-spacing: -.8px; font-weight: 750; margin-top: 8px; }
.home-subtitle { color: var(--muted); line-height: 1.6; max-width: 760px; margin-top: 9px; }
.home-grid { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 10px; margin-top: 24px; }
.home-card { background: rgba(26,29,39,.8); border: 1px solid var(--border); border-radius: 10px; padding: 14px; min-height: 92px; }
.home-card-label { text-transform: uppercase; letter-spacing: .7px; color: var(--muted); font-size: 10px; font-weight: 700; }
.home-card-value { font-size: 23px; font-weight: 750; margin-top: 9px; }
.home-card-meta { color: var(--muted); font-size: 11px; margin-top: 4px; }
.home-section { margin-top: 24px; }
.home-section-title { color: var(--text); font-size: 12px; font-weight: 700; margin-bottom: 9px; }
.action-grid { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 8px; }
.action-card { text-align: left; cursor: pointer; color: var(--text); background: var(--surface);
  border: 1px solid var(--border); border-radius: 9px; padding: 12px; font: inherit; }
.action-card:hover { border-color: var(--accent); background: var(--accent-soft); }
.action-card strong { display: block; font-size: 12px; }
.action-card span { color: var(--muted); font-size: 11px; display: block; margin-top: 5px; line-height: 1.4; }
.home-empty { border: 1px dashed var(--border); color: var(--muted); padding: 22px; border-radius: 10px; text-align: center; }
.answer-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom: 8px; }
.answer-type { color: var(--accent2); text-transform: uppercase; letter-spacing: .7px; font-size: 10px; font-weight: 700; }
.save-analysis-btn { border: 1px solid var(--border); background: transparent; color: var(--muted); border-radius: 6px; padding: 5px 8px; font-size: 10px; cursor: pointer; }
.save-analysis-btn:hover { color: var(--text); border-color: var(--accent); }
.investigation-track { margin-top: 12px; padding: 11px 12px; border: 1px solid var(--border); border-radius: 8px; background: rgba(255,255,255,.02); }
.investigation-track-title { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .6px; font-weight: 700; margin-bottom: 8px; }
.investigation-steps { display: flex; flex-wrap: wrap; gap: 6px; }
.investigation-step { color: var(--muted); border: 1px solid var(--border); padding: 5px 8px; border-radius: 999px; font-size: 10px; }
.investigation-step.done { color: var(--up); border-color: rgba(0,212,170,.35); background: rgba(0,212,170,.08); }
.drawer-section { margin-bottom: 18px; }
.drawer-title { text-transform: uppercase; letter-spacing: .7px; font-size: 10px; color: var(--muted); font-weight: 700; margin-bottom: 8px; }
.drawer-copy { color: var(--text); font-size: 12px; line-height: 1.55; }
.metric-line { display:flex; justify-content:space-between; gap:12px; padding:7px 0; border-bottom:1px solid rgba(42,45,58,.7); font-size:11px; }
.metric-line span:first-child { color:var(--muted); }
.metric-line span:last-child { color:var(--text); text-align:right; }
.confidence-bar { height: 6px; border-radius: 4px; background: var(--border); overflow:hidden; margin-top:7px; }
.confidence-bar i { display:block; height:100%; background:var(--accent2); }
.semantic-search { width:100%; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:8px; font-size:11px; margin-bottom:10px; }
.semantic-group { border:1px solid var(--border); border-radius:8px; margin-bottom:8px; overflow:hidden; }
.semantic-group summary { cursor:pointer; list-style:none; padding:9px 10px; background:rgba(255,255,255,.025); font-size:11px; color:var(--text); }
.semantic-group summary::-webkit-details-marker { display:none; }
.semantic-content { padding:9px 10px; color:var(--muted); font-size:11px; line-height:1.55; }
.semantic-content b { color:var(--text); }
.semantic-tag { display:inline-block; margin:3px 4px 0 0; padding:3px 6px; border-radius:999px; background:var(--surface-3); color:var(--muted); font-size:10px; }
@media (max-width: 1180px) { :root { --right-w: 350px; } .home-grid { grid-template-columns: repeat(2,1fr); } }
@media (max-width: 860px) { #sidebar { width: 220px; } #right { display:none; } #context-strip { display:none; } .action-grid { grid-template-columns: 1fr; } }

</style>
</head>
<body>

<!-- ── Sidebar ── -->
<nav id="sidebar">
  <div id="sidebar-logo">Tachyon<span>IQ</span></div>
  <div class="workspace-kicker">EXECUTIVE ANALYTICS WORKSPACE</div>
  <div class="workspace-nav">
    <button class="active" id="nav-overview"><span>◈</span>Overview</button>
    <button id="nav-semantic"><span>◇</span>Semantic explorer</button>
    <button id="nav-saved"><span>▣</span>Saved analyses</button>
  </div>
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
    <div id="topbar-identity">
      <div id="topbar-title">Executive analytics</div>
      <div id="topbar-subtitle"><span id="active-conn-name">No connection</span> · evidence-backed workspace</div>
    </div>
    <div id="context-strip">
      <span class="context-pill"><strong>Metric</strong> — not selected</span>
      <span class="context-pill"><strong>Window</strong> — awaiting analysis</span>
      <span class="context-pill"><strong>Filters</strong> — none</span>
    </div>
    <div id="topbar-status"><span id="spinner"></span><span id="status-text"></span></div>
  </div>
  <div id="chat-area">
    <div id="workspace-home"></div>
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
    <div class="panel-tab active" data-pane="insights">Insights</div>
    <div class="panel-tab" data-pane="evidence">Evidence</div>
    <div class="panel-tab" data-pane="sql">SQL</div>
    <div class="panel-tab" data-pane="semantic">Semantic</div>
    <div class="panel-tab" data-pane="data">Data</div>
  </div>
  <div class="panel-body">
    <div class="panel-pane active" id="pane-insights">
      <div id="insights-content"><div class="empty-state">Run an analysis to see executive insights</div></div>
    </div>
    <div class="panel-pane" id="pane-evidence">
      <div id="evidence-content"><div class="empty-state">Evidence will appear after a validated query</div></div>
    </div>
    <div class="panel-pane" id="pane-sql">
      <div id="sql-actions"><button class="icon-btn" id="copy-sql-btn">Copy</button></div>
      <div id="sql-block"><span class="empty-state" style="padding:0;">Run a query to see SQL</span></div>
    </div>
    <div class="panel-pane" id="pane-semantic">
      <div id="semantic-content"><div class="empty-state">Select a connection to load the semantic contract</div></div>
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
  lastResponse: null,
  lastQuestion: '',
  schema: null,
  semantic: null,
  activeContext: {metric: null, dimensions: [], filters: [], window: null},
};

// ── Utils ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const now = () => new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
const escapeHtml = value => String(value ?? '').replace(/&/g,'&amp;')
  .replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')
  .replace(/'/g,'&#39;');

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

function setContextPills(ctx = state.activeContext) {
  const metric = ctx.metric || 'not selected';
  const dimensions = (ctx.dimensions || []).length ? ctx.dimensions.join(', ') : 'none';
  const filters = (ctx.filters || []).length ? ctx.filters.join(', ') : 'none';
  const window = ctx.window || 'awaiting analysis';
  $('context-strip').innerHTML = [
    `<span class="context-pill"><strong>Metric</strong> — ${escapeHtml(metric)}</span>`,
    `<span class="context-pill"><strong>Group by</strong> — ${escapeHtml(dimensions)}</span>`,
    `<span class="context-pill"><strong>Window</strong> — ${escapeHtml(window)}</span>`,
    `<span class="context-pill"><strong>Filters</strong> — ${escapeHtml(filters)}</span>`,
  ].join('');
}

function renderWorkspaceHome() {
  const conn = state.connections.find(c => c.connection_id === state.activeConnId);
  const semantic = state.semantic;
  if (!conn) {
    $('workspace-home').innerHTML = `<div class="home-empty">
      <div class="home-eyebrow">TachyonIQ</div>
      <div class="home-title">Connect your business data</div>
      <div class="home-subtitle">Select a connection to open an evidence-backed executive workspace. Every answer is produced by the governed semantic layer and validated before it is shown.</div>
    </div>`;
    return;
  }
  const tableCount = Number(state.schema?.table_count || conn.table_count || 0).toLocaleString();
  const columnCount = Number(state.schema?.column_count || 0).toLocaleString();
  const relationCount = Number(state.schema?.relationship_count || 0).toLocaleString();
  const metricCount = Number(semantic?.metrics?.length || 0).toLocaleString();
  $('workspace-home').innerHTML = `<div>
    <div class="home-eyebrow">${escapeHtml(conn.name)} · READY FOR ANALYSIS</div>
    <div class="home-title">Executive summary workspace</div>
    <div class="home-subtitle">Start with a business question. TachyonIQ will resolve the intent, choose the analysis path, generate read-only SQL, validate the result, and show the calculation and evidence behind it.</div>
    <div class="home-grid">
      <div class="home-card"><div class="home-card-label">Semantic metrics</div><div class="home-card-value">${metricCount}</div><div class="home-card-meta">Governed measures available</div></div>
      <div class="home-card"><div class="home-card-label">Tables</div><div class="home-card-value">${tableCount}</div><div class="home-card-meta">Reflected source tables</div></div>
      <div class="home-card"><div class="home-card-label">Columns</div><div class="home-card-value">${columnCount}</div><div class="home-card-meta">Discoverable fields</div></div>
      <div class="home-card"><div class="home-card-label">Relationships</div><div class="home-card-value">${relationCount}</div><div class="home-card-meta">Join paths available</div></div>
    </div>
    <div class="home-section"><div class="home-section-title">Recommended executive questions</div>
      <div class="action-grid">
        <button class="action-card" data-question="Give me an executive summary of revenue, profitability, customer health, support and marketing."><strong>Executive summary</strong><span>Revenue, profit, customers, service and growth in one report.</span></button>
        <button class="action-card" data-question="Compare Europe's recognized revenue for the latest six complete months with the preceding six months."><strong>Investigate Europe revenue</strong><span>Trace the change through products, customers, volume and order value.</span></button>
        <button class="action-card" data-question="Which customer accounts have the highest churn risk and what evidence explains that risk?"><strong>Customer health</strong><span>Find at-risk accounts and the usage or service context behind them.</span></button>
        <button class="action-card" data-question="How does support resolution time relate to customer satisfaction?"><strong>Support quality</strong><span>Measure service performance and the relationship with CSAT.</span></button>
        <button class="action-card" data-question="Which marketing channels and campaigns have the strongest ROAS and conversion efficiency?"><strong>Marketing efficiency</strong><span>Compare spend, funnel conversion and attributed revenue.</span></button>
        <button class="action-card" data-question="Find unusual orders or revenue movements in the latest complete month."><strong>Find anomalies</strong><span>Surface unusual observations with statistical evidence.</span></button>
      </div>
    </div>
    <div class="home-section"><div class="home-section-title">How the workspace answers</div>
      <div class="home-subtitle">Ask a follow-up such as “break that down by product,” “show the evidence,” or “compare it with APAC.” The active metric, dimensions, filters and time window remain visible above the conversation.</div>
    </div>
  </div>`;
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
      <div class="conn-name" title="${escapeHtml(c.name)}">${escapeHtml(c.name)}</div>
      <div class="conn-badge">${escapeHtml(c.dialect)}</div>`;
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
  loadSavedAnalyses();
}

async function loadSchema(connId) {
  try {
    const [schemaResponse, semanticResponse] = await Promise.all([
      fetch(`/connections/${connId}/schema`),
      fetch('/semantic-context'),
    ]);
    if (schemaResponse.ok) state.schema = await schemaResponse.json();
    if (semanticResponse.ok) state.semantic = await semanticResponse.json();
    renderWorkspaceHome();
    renderSemanticExplorer();
  } catch(e) {}
}

async function loadSavedAnalyses() {
  try {
    const suffix = state.activeConnId ? `?connection_id=${encodeURIComponent(state.activeConnId)}&limit=12` : '?limit=12';
    const r = await fetch('/analyses' + suffix);
    if (!r.ok) return;
    const d = await r.json();
    const items = (d.analyses || []).map(a => `<div class="history-item" data-analysis-id="${escapeHtml(a.id)}" title="${escapeHtml(a.question)}">${escapeHtml(a.name)}</div>`).join('');
    $('history-list').innerHTML = items || '<div style="padding:8px 16px;font-size:12px;color:var(--muted)">No saved analyses yet</div>';
  } catch(e) { /* saved analyses are an enhancement; the query path remains available */ }
}

function renderSemanticExplorer() {
  const semantic = state.semantic;
  const schema = state.schema;
  if (!semantic && !schema) {
    $('semantic-content').innerHTML = '<div class="empty-state">Select a connection to load the semantic contract</div>';
    return;
  }
  const metrics = semantic?.metrics || [];
  const tables = semantic?.tables || (schema?.tables || []).map(t => ({name: t.table_name, columns: t.columns || []}));
  const glossary = semantic?.glossary || [];
  const examples = semantic?.examples || [];
  let html = `<input class="semantic-search" id="semantic-search" placeholder="Search metrics, tables, definitions…" oninput="filterSemantic(this.value)">
    <div class="drawer-copy" style="margin-bottom:10px">${metrics.length} governed metrics · ${tables.length} source tables · ${glossary.length} glossary terms</div>`;
  html += `<details class="semantic-group" open><summary>Metrics and definitions</summary><div class="semantic-content" id="semantic-metrics">${metrics.map(m => `<div class="metric-line semantic-item" data-search="${escapeHtml([m.name,m.description,...(m.aliases||[])].join(' '))}"><span><b>${escapeHtml(m.name)}</b><br>${escapeHtml(m.description || '')}</span><span>${escapeHtml(m.unit || '')}</span></div>`).join('') || '<div>No metrics published.</div>'}</div></details>`;
  html += `<details class="semantic-group"><summary>Business glossary</summary><div class="semantic-content">${glossary.map(g => `<div class="semantic-item" data-search="${escapeHtml([g.term,g.description,...(g.aliases||[])].join(' '))}" style="margin-bottom:9px"><b>${escapeHtml(g.term)}</b><br>${escapeHtml(g.description || '')}<div>${(g.aliases||[]).map(a=>`<span class="semantic-tag">${escapeHtml(a)}</span>`).join('')}</div></div>`).join('') || '<div>No glossary terms published.</div>'}</div></details>`;
  html += `<details class="semantic-group"><summary>Source tables and dimensions</summary><div class="semantic-content">${tables.map(t => `<div class="semantic-item" data-search="${escapeHtml([t.name,t.description,t.grain].join(' '))}" style="margin-bottom:9px"><b>${escapeHtml(t.name)}</b> · ${escapeHtml(t.grain || 'table')}<br>${escapeHtml(t.description || '')}<div>${(t.columns||[]).slice(0,12).map(c=>`<span class="semantic-tag">${escapeHtml(c.name || c.column_name)}</span>`).join('')}${(t.columns||[]).length>12?'<span class="semantic-tag">…</span>':''}</div></div>`).join('')}</div></details>`;
  html += `<details class="semantic-group"><summary>Verified business questions</summary><div class="semantic-content">${examples.slice(0,12).map(e => `<button class="chip semantic-question" data-question="${escapeHtml(e.question)}">${escapeHtml(e.question)}</button>`).join('')}</div></details>`;
  $('semantic-content').innerHTML = html;
}

function filterSemantic(query) {
  const needle = String(query || '').toLowerCase();
  document.querySelectorAll('#semantic-content .semantic-item').forEach(el => {
    el.style.display = !needle || (el.dataset.search || '').toLowerCase().includes(needle) ? '' : 'none';
  });
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
  const home = document.createElement('div');
  home.id = 'workspace-home';
  $('chat-area').appendChild(home);
  state.sessionId = null;
  state.lastSql = '';
  state.lastData = null;
  state.lastResponse = null;
  state.activeContext = {metric: null, dimensions: [], filters: [], window: null};
  setContextPills();
  $('sql-block').innerHTML = '<span class="empty-state" style="padding:0;">Run a query to see SQL</span>';
  $('data-table-wrap').innerHTML = '<div class="empty-state">Run a query to see raw data</div>';
  $('insights-content').innerHTML = '<div class="empty-state">Run an analysis to see executive insights</div>';
  $('evidence-content').innerHTML = '<div class="empty-state">Evidence will appear after a validated query</div>';
  renderWorkspaceHome();
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
    ? `<div class="kpi-trend ${cls}">${arrow} ${Math.abs(viz.trend_pct).toFixed(1)}% ${escapeHtml(viz.comparison_label || '')}</div>`
    : '';
  return `<div class="kpi-card">
    <div class="kpi-label">${escapeHtml(viz.label || '')}</div>
    <div class="kpi-value">${escapeHtml(viz.formatted_value ?? viz.value)}</div>
    ${trendHtml}
  </div>`;
}

let chartIdCounter = 0;
function renderFallbackChart(viz) {
  const spec = viz?.spec || {};
  const values = spec?.data?.values || [];
  const enc = spec?.encoding || {};
  const xField = enc?.x?.field;
  const yField = enc?.y?.field;
  const colorField = enc?.color?.field;
  if (!values.length || !xField || !yField) {
    return '<div class="chart-fallback">Chart data is available in the Data tab.</div>';
  }
  const width = 720, height = 260, left = 58, right = 20, top = 20, bottom = 42;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const xs = [...new Set(values.map(row => String(row[xField])))];
  const ys = values.map(row => Number(row[yField])).filter(Number.isFinite);
  if (!xs.length || !ys.length) return '<div class="chart-fallback">Chart data is unavailable.</div>';
  const minY = Math.min(0, ...ys), maxY = Math.max(...ys);
  const range = maxY - minY || 1;
  const xAt = index => left + (xs.length === 1 ? plotWidth / 2 : (index / (xs.length - 1)) * plotWidth);
  const yAt = value => top + plotHeight - ((Number(value) - minY) / range) * plotHeight;
  const groups = {};
  values.forEach(row => {
    const group = colorField ? String(row[colorField]) : 'Value';
    (groups[group] ||= []).push(row);
  });
  const palette = ['#8b5cf6', '#00d4aa', '#ffb84d', '#ff4d6a', '#38bdf8'];
  let svg = `<svg class="fallback-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(spec.title || viz.title || 'Chart')}">`;
  for (let i = 0; i <= 4; i++) {
    const y = top + (plotHeight * i / 4);
    const value = maxY - (range * i / 4);
    svg += `<line x1="${left}" y1="${y}" x2="${width-right}" y2="${y}" stroke="#2a2d3a"/><text x="${left-8}" y="${y+4}" text-anchor="end" fill="#7b7f96" font-size="10">${Number(value).toLocaleString(undefined,{maximumFractionDigits:0})}</text>`;
  }
  const markType = typeof spec.mark === 'string' ? spec.mark : spec.mark?.type;
  Object.entries(groups).forEach(([group, rows], groupIndex) => {
    const points = rows.map(row => `${xAt(xs.indexOf(String(row[xField]))).toFixed(1)},${yAt(row[yField]).toFixed(1)}`).join(' ');
    const colour = palette[groupIndex % palette.length];
    if (markType === 'bar') {
      const barWidth = Math.max(4, plotWidth / Math.max(xs.length, 1) / Math.max(Object.keys(groups).length, 1) - 2);
      rows.forEach(row => {
        const x = xAt(xs.indexOf(String(row[xField]))) - (Object.keys(groups).length * barWidth / 2) + groupIndex * barWidth;
        const y = yAt(row[yField]);
        svg += `<rect x="${x}" y="${Math.min(y, yAt(0))}" width="${barWidth}" height="${Math.abs(yAt(0)-y)}" fill="${colour}" opacity=".85"/>`;
      });
    } else {
      svg += `<polyline points="${points}" fill="none" stroke="${colour}" stroke-width="2.5"/>`;
      rows.forEach(row => {
        const x = xAt(xs.indexOf(String(row[xField]))), y = yAt(row[yField]);
        svg += `<circle cx="${x}" cy="${y}" r="3" fill="${colour}"/>`;
      });
    }
    const legendX = left + groupIndex * 150;
    svg += `<circle cx="${legendX}" cy="${height-12}" r="4" fill="${colour}"/><text x="${legendX+9}" y="${height-8}" fill="#a6a8bb" font-size="10">${escapeHtml(group)}</text>`;
  });
  xs.forEach((label, index) => {
    if (xs.length <= 12 || index % Math.ceil(xs.length / 8) === 0 || index === xs.length - 1) {
      svg += `<text x="${xAt(index)}" y="${height-26}" text-anchor="middle" fill="#7b7f96" font-size="9">${escapeHtml(label.slice(0, 7))}</text>`;
    }
  });
  return svg + '</svg>';
}

function renderChart(viz) {
  const id = 'chart-' + (++chartIdCounter);
  setTimeout(() => {
    const el = document.getElementById(id);
    if (!el || !viz.spec) return;
    // Paint a deterministic inline chart first.  A remote Vega promise can
    // remain pending when a CDN or schema URL is blocked, which previously
    // left an empty chart frame.  The inline SVG is the guaranteed baseline;
    // Vega can enhance it later without being required for a visible result.
    el.innerHTML = renderFallbackChart(viz);
  }, 50);
  return `<div class="chart-wrap"><div id="${id}"></div></div>`;
}

function renderVisualisation(viz) {
  if (!viz) return '';
  if (viz.value !== undefined && viz.label !== undefined) return renderKpi(viz);
  if (viz.spec) return renderChart(viz);
  if (viz.reason) return `<div style="color:var(--muted);font-size:12px;margin-top:8px">No chart: ${escapeHtml(viz.reason)}</div>`;
  return '';
}

function renderChips(questions) {
  if (!questions || !questions.length) return '';
  const chips = questions.map(q =>
    `<button class="chip" data-question="${escapeHtml(q)}">${escapeHtml(q)}</button>`
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
  if (dq.summary) h += `<div style="font-size:11px;color:var(--muted);margin-top:3px">${escapeHtml(dq.summary)}</div>`;
  if (dq.issues && dq.issues.length) {
    h += '<div class="dq-issues">' + dq.issues.slice(0,4).map(i =>
      `<div class="dq-issue"><div class="dq-dot ${escapeHtml(i.severity)}"></div><span><b>${escapeHtml(i.column)}</b>: ${escapeHtml(i.detail || i.issue_type)}</span></div>`
    ).join('') + '</div>';
  }
  return h;
}

function renderEvidence(response) {
  if (!response || response.error) return '';
  const tables = (response.tables_used || []).map(escapeHtml).join(', ') || '—';
  const rows = response.row_count == null ? '—' : Number(response.row_count).toLocaleString();
  const execution = response.execution_time_ms == null
    ? '—'
    : `${Number(response.execution_time_ms).toFixed(1)} ms`;
  const confidence = response.critic_score == null
    ? ''
    : `<span>Evidence score: <b>${(Number(response.critic_score) * 100).toFixed(0)}%</b></span>`;
  return `<div class="ex-sec" style="margin-top:10px">
    <div class="ex-sec-lbl">Evidence</div>
    <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--muted)">
      <span>Validated read-only SQL</span><span>Rows: <b>${rows}</b></span>
      <span>Execution: <b>${execution}</b></span>${confidence}
    </div>
    <div style="font-size:11px;color:var(--muted);margin-top:4px">Sources: ${tables}</div>
  </div>`;
}

function renderEvidenceDrawer(response) {
  if (!response || response.error) return '<div class="empty-state">Evidence will appear after a validated query</div>';
  const ex = response.explainability || {};
  const tables = (response.tables_used || ex.tables_referenced || []).map(escapeHtml).join(', ') || '—';
  const filters = (ex.filters_applied || []).map(escapeHtml).join('<br>') || 'No additional filters';
  const rows = response.row_count == null ? '—' : Number(response.row_count).toLocaleString();
  const score = response.critic_score == null ? null : Math.max(0, Math.min(1, Number(response.critic_score)));
  const stats = response.statistical_analysis || {};
  const provenance = stats.provenance || {};
  const methods = (stats.methods || []).map(escapeHtml).join('<br>') || 'No statistical method was required for this result shape.';
  const assumptions = [...(stats.assumptions || []), ...(ex.assumptions || [])];
  const limitations = stats.limitations || [];
  const fingerprint = provenance.result_sha256 ? escapeHtml(String(provenance.result_sha256).slice(0, 16)) + '…' : '—';
  const steps = (ex.calculation_steps || []).map(s => `<div class="ex-step">${escapeHtml(s)}</div>`).join('') || '<div class="drawer-copy">Calculation steps were not returned for this response.</div>';
  return `<div class="drawer-section"><div class="drawer-title">Validated result</div>
    <div class="metric-line"><span>Rows analyzed</span><span>${rows}</span></div>
    <div class="metric-line"><span>Execution time</span><span>${response.execution_time_ms == null ? '—' : Number(response.execution_time_ms).toFixed(1) + ' ms'}</span></div>
    <div class="metric-line"><span>Source tables</span><span>${tables}</span></div>
  </div>
  <div class="drawer-section"><div class="drawer-title">Business definition and filters</div><div class="drawer-copy">${filters}</div></div>
  <div class="drawer-section"><div class="drawer-title">Calculation steps</div>${steps}</div>
  <div class="drawer-section"><div class="drawer-title">Statistical methods</div><div class="drawer-copy">${methods}</div></div>
  <div class="drawer-section"><div class="drawer-title">Reproducibility</div>
    <div class="metric-line"><span>Result fingerprint</span><span>${fingerprint}</span></div>
    <div class="metric-line"><span>Deterministic</span><span>${provenance.deterministic === true ? 'Yes' : '—'}</span></div>
  </div>
  ${score == null ? '' : `<div class="drawer-section"><div class="drawer-title">Evidence confidence</div><div class="drawer-copy">${(score*100).toFixed(0)}% deterministic evidence score</div><div class="confidence-bar"><i style="width:${score*100}%"></i></div></div>`}
  <div class="drawer-section"><div class="drawer-title">Assumptions and limitations</div><div class="drawer-copy">${(assumptions.length ? assumptions : ['Interpretation follows the active semantic definitions and validated query population.']).map(escapeHtml).join('<br>')}${limitations.length ? '<br><br>' + limitations.map(x => 'Limitation: ' + escapeHtml(x)).join('<br>') : ''}</div></div>`;
}

function renderInsightsDrawer(response) {
  if (!response || response.error) return '<div class="empty-state">Run an analysis to see executive insights</div>';
  const ins = response.generated_insights || {};
  const findings = [...new Set([...(ins.key_findings || []), ...(response.key_findings_bullets || [])])];
  const drivers = [...new Set([...(ins.drivers || []), ...(response.drivers || [])])];
  const recommendations = ins.recommendations || [];
  const statisticBlocks = [];
  const stats = response.statistical_analysis || {};
  const descriptive = stats.descriptive || {};
  const forecast = stats.forecast || ((!response.forecast_result || response.forecast_result.skipped) ? null : response.forecast_result);
  const fmtNumber = (value, digits = 1) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString(undefined, {maximumFractionDigits: digits});
  const labelFor = (name) => String(name || '').replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
  const fmtMetric = (name, value) => /revenue|profit|spend|mrr|value|cost|rmse|mae/i.test(name)
    ? (value == null ? '—' : '$' + fmtNumber(value, 0)) : fmtNumber(value, 2);
  statisticBlocks.push(`<div class="metric-line"><span>Returned observations</span><span>${escapeHtml(stats.sample_size ?? response.row_count ?? '—')}</span></div>`);
  if (forecast && (forecast.points || []).length) {
    const first = forecast.points[0];
    statisticBlocks.push(`<div class="metric-line"><span>Forecast horizon</span><span>${escapeHtml(forecast.forecast_periods ?? forecast.points.length)} period(s)</span></div>`);
    statisticBlocks.push(`<div class="metric-line"><span>Selected model</span><span>${escapeHtml(String(forecast.method || '—').replaceAll('_', ' '))}</span></div>`);
    statisticBlocks.push(`<div class="metric-line"><span>Latest actual</span><span>${fmtMetric(forecast.forecast_column, stats.latest_actual)}</span></div>`);
    statisticBlocks.push(`<div class="metric-line"><span>First forecast</span><span>${fmtMetric(forecast.forecast_column, first.forecast)} (${fmtNumber(stats.first_forecast_change_pct)}%)</span></div>`);
    statisticBlocks.push(`<div class="metric-line"><span>Prediction interval</span><span>${fmtMetric(forecast.forecast_column, first.lower_ci)} – ${fmtMetric(forecast.forecast_column, first.upper_ci)}</span></div>`);
    statisticBlocks.push(`<div class="metric-line"><span>Holdout RMSE</span><span>${fmtMetric(forecast.forecast_column, forecast.validation_rmse)}</span></div>`);
  } else {
    Object.entries(descriptive).slice(0, 4).forEach(([name, values]) => {
      if (!values || values.mean == null) return;
      const ci = values.mean_confidence_interval || [];
      statisticBlocks.push(`<div class="metric-line"><span>${escapeHtml(labelFor(name))} mean</span><span>${fmtMetric(name, values.mean)}</span></div>`);
      statisticBlocks.push(`<div class="drawer-copy">Median ${fmtMetric(name, values.median)} · SD ${fmtMetric(name, values.stddev)} · ${fmtNumber((values.coefficient_of_variation_pct), 1)}% CV${ci.length === 2 ? ` · CI ${fmtMetric(name, ci[0])}–${fmtMetric(name, ci[1])}` : ''}</div>`);
    });
  }
  const trendEntries = Object.entries((stats.tests || {}).trend || {});
  if (trendEntries.length) {
    const significant = trendEntries.filter(([, test]) => test && (test.q_value_significant ?? test.significant)).length;
    const [name, trendTest] = trendEntries[0];
    statisticBlocks.push(`<div class="metric-line"><span>FDR-significant trends</span><span>${significant} of ${trendEntries.length}</span></div>`);
    statisticBlocks.push(`<div class="drawer-copy">${escapeHtml(labelFor(name))}: p=${fmtNumber(trendTest.p_value, 4)} · q=${fmtNumber(trendTest.q_value, 4)} · R²=${fmtNumber(trendTest.r_squared, 3)} · slope ${fmtNumber(trendTest.slope_per_bucket, 2)}</div>`);
  }
  const correlationEntries = Object.entries((stats.tests || {}).correlation || {})
    .sort((a, b) => Math.abs(Number(b[1]?.coefficient || 0)) - Math.abs(Number(a[1]?.coefficient || 0)));
  if (correlationEntries.length) {
    const [pair, correlation] = correlationEntries[0];
    statisticBlocks.push(`<div class="metric-line"><span>Strongest association</span><span>r=${fmtNumber(correlation.coefficient, 3)}</span></div>`);
    statisticBlocks.push(`<div class="drawer-copy">${escapeHtml(labelFor(pair.replace(':', ' vs ')))} · p=${fmtNumber(correlation.p_value, 4)} · FDR q=${fmtNumber(correlation.q_value, 4)}</div>`);
  }
  const regression = (stats.tests || {}).regression;
  if (regression && regression.status === 'fitted') {
    const coefficients = Object.entries(regression.coefficients || {}).filter(([name]) => name !== 'const');
    statisticBlocks.push(`<div class="metric-line"><span>Explanatory model</span><span>adjusted R²=${fmtNumber(regression.adjusted_r_squared, 3)}</span></div>`);
    statisticBlocks.push(`<div class="drawer-copy">HC3 robust OLS on ${fmtNumber(regression.observations, 0)} observations · ${coefficients.length} predictors · association only, not causal</div>`);
  } else if (regression && regression.status === 'insufficient_sample') {
    statisticBlocks.push(`<div class="drawer-copy">Regression withheld: ${fmtNumber(regression.observations, 0)} observations; ${fmtNumber(regression.minimum_required, 0)} required.</div>`);
  }
  const distributionEntries = Object.entries((stats.tests || {}).distribution || {});
  if (distributionEntries.length) {
    const rejected = distributionEntries.filter(([, test]) => test && test.q_value_significant).length;
    statisticBlocks.push(`<div class="metric-line"><span>Non-normal distributions</span><span>${rejected} of ${distributionEntries.length} after FDR control</span></div>`);
  }
  const anomalyCount = Object.values(stats.anomalies || {}).reduce((total, item) => total + Number(item?.count || 0), 0);
  if (anomalyCount) statisticBlocks.push(`<div class="metric-line"><span>Robust anomaly flags</span><span>${anomalyCount}</span></div>`);
  const provenance = stats.provenance || {};
  if (provenance.result_sha256) statisticBlocks.push(`<div class="drawer-copy" style="margin-top:8px">Proof fingerprint: ${escapeHtml(String(provenance.result_sha256).slice(0, 16))}…</div>`);
  if (response.correlation_result && !response.correlation_result.skipped) {
    const corr = response.correlation_result;
    const top = corr.top_pairs && corr.top_pairs.length ? corr.top_pairs[0] : null;
    statisticBlocks.push(`<div class="metric-line"><span>Correlation</span><span>${escapeHtml(corr.method || 'Pearson')} · ${top?.r == null ? 'see chart' : Number(top.r).toFixed(2)}</span></div>`);
  }
  if (response.anomaly_result && !response.anomaly_result.skipped) {
    statisticBlocks.push(`<div class="metric-line"><span>Anomalies</span><span>${escapeHtml(response.anomaly_result.anomaly_count ?? 'Detected')}</span></div>`);
  }
  let html = '';
  if (response.key_finding) html += `<div class="drawer-section"><div class="drawer-title">Headline</div><div class="drawer-copy">${escapeHtml(response.key_finding)}</div></div>`;
  if (findings.length) html += `<div class="drawer-section"><div class="drawer-title">${ins.evidence_verified ? 'Verified findings' : 'Key findings'}</div>${findings.slice(0,6).map(x=>`<div class="insights-item">${escapeHtml(x)}</div>`).join('')}</div>`;
  if (drivers.length) html += `<div class="drawer-section"><div class="drawer-title">Possible explanations</div>${drivers.slice(0,6).map(x=>`<div class="insights-item">${escapeHtml(x)}</div>`).join('')}</div>`;
  if (statisticBlocks.length) html += `<div class="drawer-section"><div class="drawer-title">Statistics</div>${statisticBlocks.join('')}</div>`;
  if (recommendations.length) html += `<div class="drawer-section"><div class="drawer-title">Recommended actions</div>${recommendations.slice(0,5).map(x=>`<div class="insights-item">${escapeHtml(x)}</div>`).join('')}</div>`;
  return html || '<div class="empty-state">The response returned evidence without a narrative insight block.</div>';
}

function renderInvestigation(response) {
  const steps = response?.evidence_nodes || [];
  if (!steps.length && response?.investigation_steps == null) return '';
  const labels = steps.length ? steps.map(x => String(x).split(':').slice(1).join(':').trim() || x) : ['Intent resolved', 'Evidence collected', 'Result verified'];
  return `<div class="investigation-track"><div class="investigation-track-title">Investigation trace · ${escapeHtml(response.investigation_steps ?? labels.length)} stages</div><div class="investigation-steps">${labels.slice(0,8).map(x=>`<span class="investigation-step done">✓ ${escapeHtml(x)}</span>`).join('')}</div></div>`;
}

function updateActiveContext(response) {
  const ex = response?.explainability || {};
  const snapshot = response?.context_snapshot || {};
  const measures = snapshot.current_measures || [];
  const questionType = response?.question_type || 'analysis';
  state.activeContext = {
    metric: measures.length ? measures.join(', ') : (response?.visualisation?.label || response?.visualisation?.metric || questionType),
    dimensions: snapshot.current_dimensions || [],
    filters: snapshot.accumulated_filters || ex.filters_applied || [],
    window: snapshot.current_time_range || ex.time_range || ex.date_range || null,
  };
  setContextPills(state.activeContext);
}

// ── Explainability ──────────────────────────────────────────────────────────
let _exId = 0;
function renderExplainability(ex) {
  if (!ex) return '';
  const id = 'ex-' + (++_exId);
  let body = '';
  if (ex.calculation_steps && ex.calculation_steps.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Calculation Steps</div>${ex.calculation_steps.map(s=>`<div class="ex-step">${escapeHtml(s)}</div>`).join('')}</div>`;
  if (ex.filters_applied && ex.filters_applied.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Filters</div><div>${escapeHtml(ex.filters_applied.join(', '))}</div></div>`;
  if (ex.aggregations && ex.aggregations.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Aggregations</div><div>${escapeHtml(ex.aggregations.join(', '))}</div></div>`;
  if (ex.tables_referenced && ex.tables_referenced.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Tables</div><div>${escapeHtml(ex.tables_referenced.join(', '))}</div></div>`;
  if (ex.assumptions && ex.assumptions.length)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Assumptions</div><div>${escapeHtml(ex.assumptions.join('; '))}</div></div>`;
  if (ex.chart_rationale)
    body += `<div class="ex-sec"><div class="ex-sec-lbl">Chart Choice</div><div>${escapeHtml(ex.chart_rationale)}</div></div>`;
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
  let h = `<div class="insights-panel"><div class="insights-hdr">${ins.evidence_verified ? '✓ Verified insights' : 'Analyst insights'}</div>`;
  if (ins.key_findings && ins.key_findings.length)
    h += `<div class="insights-sec"><div class="insights-sec-lbl">Key Findings</div>${ins.key_findings.map(f=>`<div class="insights-item">${escapeHtml(f)}</div>`).join('')}</div>`;
  if (ins.drivers && ins.drivers.length)
    h += `<div class="insights-sec"><div class="insights-sec-lbl">Possible explanations</div>${ins.drivers.map(d=>`<div class="insights-item">${escapeHtml(d)}</div>`).join('')}</div>`;
  if (ins.recommendations && ins.recommendations.length)
    h += `<div class="insights-sec"><div class="insights-sec-lbl">Recommendations</div>${ins.recommendations.map(r=>`<div class="insights-item">${escapeHtml(r)}</div>`).join('')}</div>`;
  if (ins.confidence)
    h += `<div class="insights-conf">Confidence: ${escapeHtml(ins.confidence)}</div>`;
  return h + '</div>';
}

// ── Forecast Sparkline ──────────────────────────────────────────────────────
function renderForecast(fc) {
  if (!fc || fc.skipped) return '';
  const id = 'fc-' + (++chartIdCounter);
  const rawValues = fc.forecast_values || (fc.points || []).map(point => point.forecast);
  if (!rawValues.length) return '';
  const vals = rawValues.slice(0, 12);
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
    if (el) el.innerHTML = renderFallbackChart({spec, title: label});
  }, 60);
  const label = fc.method ? `Forecast · ${fc.method}` : 'Forecast';
  return `<div class="chart-wrap" style="margin-top:8px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">${escapeHtml(label)} · next ${vals.length} periods</div>
    <div id="${id}"></div>
  </div>`;
}

// ── Anomaly Highlights ──────────────────────────────────────────────────────
function renderAnomalies(an) {
  if (!an || an.skipped || !an.anomaly_count) return '';
  let h = `<div class="anomaly-badge">⚠ ${an.anomaly_count} anomal${an.anomaly_count===1?'y':'ies'} detected</div>`;
  const rows = an.top_anomalies || an.anomaly_rows || [];
  if (rows.length)
    h += rows.slice(0,3).map(row => {
      const txt = Object.entries(row).slice(0,4).map(([k,v])=>`${escapeHtml(k)}: <b>${escapeHtml(typeof v === 'object' ? JSON.stringify(v) : v)}</b>`).join(' · ');
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
    const val = typeof p.correlation === 'number' ? p.correlation : (typeof p.r === 'number' ? p.r : 0);
    const pct = Math.abs(val * 100).toFixed(0);
    const cls = val >= 0 ? 'pos' : 'neg';
    h += `<div class="corr-pair">
      <div class="corr-names">${escapeHtml(p.col_a)} ↔ ${escapeHtml(p.col_b)}</div>
      <div class="corr-bar-bg"><div class="corr-bar ${cls}" style="width:${pct}%"></div></div>
      <div class="corr-val">${val>=0?'+':''}${val.toFixed(2)}</div>
    </div>`;
  });
  return h + '</div>';
}

function renderDataTable(data) {
  if (!data || !data.columns || !data.rows || !data.rows.length) return;
  // QueryResult uses ColumnMeta objects and row arrays; keep the renderer
  // tolerant of the older string/dict shape so saved responses remain usable.
  const columns = data.columns.map(c => typeof c === 'string' ? c : c.name);
  const hdrs = columns.map(c => `<th>${escapeHtml(c)}</th>`).join('');
  const rows = data.rows.slice(0, 50).map(r =>
    '<tr>' + columns.map((c, i) => {
      const value = Array.isArray(r) ? r[i] : r[c];
      const safe = escapeHtml(value);
      return `<td title="${safe}">${safe}</td>`;
    }).join('') + '</tr>'
  ).join('');
  $('data-table-wrap').innerHTML = `<table class="data-tbl"><thead><tr>${hdrs}</tr></thead><tbody>${rows}</tbody></table>
    ${data.rows.length > 50 ? `<div style="font-size:11px;color:var(--muted);padding:6px 0">Showing 50 of ${data.rows.length} rows</div>` : ''}`;
}

async function sendQuestion(question) {
  question = question || $('question').value.trim();
  if (!question) return;
  state.lastQuestion = question;
  $('question').value = '';
  autoResize();

  // Show user bubble
  appendMsg('user', `<div class="msg-bubble">${escapeHtml(question)}</div><div class="msg-time">${now()}</div>`);
  setStatus('Thinking…', true);
  $('send-btn').disabled = true;
  $('insights-content').innerHTML = '<div class="empty-state">Analyzing the current question…</div>';
  $('evidence-content').innerHTML = '<div class="empty-state">Validating the current evidence…</div>';
  $('sql-block').textContent = 'SQL will appear after validation.';
  $('data-table-wrap').innerHTML = '<div class="empty-state">Data will appear after execution.</div>';

  const body = {question, session_id: state.sessionId};
  if (state.activeConnId) body.connection_id = state.activeConnId;

  try {
    const r = await fetch('/query', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json();
    setStatus('', false);
    $('send-btn').disabled = false;

    if (d.session_id) state.sessionId = d.session_id;
    state.lastResponse = d;
    updateActiveContext(d);
    $('insights-content').innerHTML = renderInsightsDrawer(d);
    $('evidence-content').innerHTML = renderEvidenceDrawer(d);

    // Update history
    addHistory(question);

    if (d.error) {
      state.lastSql = '';
      state.lastData = null;
      $('sql-block').textContent = 'No validated SQL was produced for this question.';
      $('data-table-wrap').innerHTML = '<div class="empty-state">No result rows were produced.</div>';
      appendMsg('ai', `<div class="msg-bubble msg-error">⚠ ${escapeHtml(d.error.user_message || d.error.message || 'Something went wrong.')}</div>`);
      return;
    }

    // SQL panel
    state.lastSql = d.sql || '';
    $('sql-block').innerHTML = d.sql ? highlightSql(d.sql) : 'No SQL returned.';

    // Data panel
    state.lastData = d.query_result || null;
    if (d.query_result) renderDataTable(d.query_result);
    else $('data-table-wrap').innerHTML = '<div class="empty-state">No result rows returned.</div>';

    // Build AI message
    let html = '<div class="msg-bubble">';
    html += `<div class="answer-head"><span class="answer-type">${escapeHtml(d.question_type || 'validated analysis')}</span><button class="save-analysis-btn" onclick="saveCurrentAnalysis()">Save analysis</button></div>`;
    if (d.answer) html += `<div class="msg-answer">${escapeHtml(d.answer)}</div>`;
    if (d.key_finding) html += `<div class="msg-finding">💡 ${escapeHtml(d.key_finding)}</div>`;

    html += renderVisualisation(d.visualisation);
    if (d.supplementary_visualisations && d.supplementary_visualisations.length) {
      html += `<div class="drawer-title" style="margin-top:12px">Additional views</div>`;
      html += d.supplementary_visualisations.slice(0,3).map(renderVisualisation).join('');
    }

    // P4-A rich response fields
    html += renderDataQuality(d.data_quality);
    html += renderEvidence(d);
    html += renderExplainability(d.explainability);
    html += renderInsights(d.generated_insights);
    html += renderForecast(d.forecast_result);
    html += renderAnomalies(d.anomaly_result);
    html += renderCorrelation(d.correlation_result);
    html += renderInvestigation(d);
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
    appendMsg('ai', `<div class="msg-bubble msg-error">Request failed: ${escapeHtml(e.message)}</div>`);
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

async function saveCurrentAnalysis() {
  if (!state.lastResponse || !state.lastQuestion) return;
  const defaultName = `${state.lastResponse.question_type || 'Analysis'} · ${state.lastQuestion.slice(0, 58)}`;
  const name = window.prompt('Name this analysis', defaultName);
  if (!name) return;
  try {
    const r = await fetch('/analyses', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
      name, question: state.lastQuestion, sql_text: state.lastSql || null,
      response: state.lastResponse, connection_id: state.activeConnId,
      tags: [state.lastResponse.question_type || 'analysis']
    })});
    if (!r.ok) throw new Error('Could not save analysis');
    setStatus('Analysis saved', false);
    loadSavedAnalyses();
  } catch(e) { setStatus(e.message, false); }
}

async function openSavedAnalysis(id) {
  try {
    const r = await fetch(`/analyses/${encodeURIComponent(id)}`);
    if (!r.ok) return;
    const d = await r.json();
    const response = d.response || {};
    state.lastQuestion = d.question || '';
    state.lastResponse = response;
    state.lastSql = d.sql_text || response.sql || '';
    state.lastData = response.query_result || null;
    updateActiveContext(response);
    $('chat-area').innerHTML = '';
    appendMsg('user', `<div class="msg-bubble">${escapeHtml(d.question || d.name)}</div><div class="msg-time">Saved analysis</div>`);
    $('insights-content').innerHTML = renderInsightsDrawer(response);
    $('evidence-content').innerHTML = renderEvidenceDrawer(response);
    if (state.lastSql) $('sql-block').innerHTML = highlightSql(state.lastSql);
    if (state.lastData) renderDataTable(state.lastData);
    let html = '<div class="msg-bubble">';
    html += `<div class="answer-head"><span class="answer-type">${escapeHtml(response.question_type || 'saved analysis')}</span></div>`;
    if (response.answer) html += `<div class="msg-answer">${escapeHtml(response.answer)}</div>`;
    if (response.key_finding) html += `<div class="msg-finding">💡 ${escapeHtml(response.key_finding)}</div>`;
    html += renderVisualisation(response.visualisation);
    html += renderDataQuality(response.data_quality) + renderEvidence(response) + renderExplainability(response) + renderInsights(response.generated_insights) + renderInvestigation(response);
    html += '</div>';
    appendMsg('ai', html);
  } catch(e) { setStatus('Could not open saved analysis', false); }
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
document.addEventListener('click', event => {
  const chip = event.target.closest('.chip[data-question]');
  if (chip) sendQuestion(chip.dataset.question);
  const action = event.target.closest('.action-card[data-question]');
  if (action) sendQuestion(action.dataset.question);
  const saved = event.target.closest('[data-analysis-id]');
  if (saved) openSavedAnalysis(saved.dataset.analysisId);
});

// ── Panel tabs ─────────────────────────────────────────────────────────────
function activatePane(pane) {
  document.querySelectorAll('.panel-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === pane));
  document.querySelectorAll('.panel-pane').forEach(p => p.classList.toggle('active', p.id === 'pane-' + pane));
}
document.querySelectorAll('.panel-tab').forEach(tab => {
  tab.addEventListener('click', () => activatePane(tab.dataset.pane));
});

document.querySelectorAll('.workspace-nav button').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.workspace-nav button').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    if (button.id === 'nav-semantic') activatePane('semantic');
    if (button.id === 'nav-overview') { activatePane('insights'); renderWorkspaceHome(); }
    if (button.id === 'nav-saved') {
      activatePane('insights');
      $('history-list').scrollIntoView({behavior:'smooth', block:'start'});
      setStatus('Saved analyses are listed in the left navigation', false);
    }
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
setContextPills();
renderWorkspaceHome();
loadConnections();
loadSavedAnalyses();
</script>
</body>
</html>"""


@router.get("/semantic-context")
async def semantic_context(request: Request) -> dict[str, object]:
    """Expose the active semantic layer for the in-product explorer.

    The workspace needs business definitions, relationships and verified examples
    to explain how a response was produced.  This endpoint deliberately returns
    the public semantic contract only; security row filters and excluded columns
    remain server-side concerns.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    manager = getattr(orchestrator, "_scl_manager", None)
    scl = getattr(manager, "scl", None)
    if scl is None:
        raise HTTPException(status_code=503, detail="Semantic context is not ready.")

    def dump(value: object) -> object:
        model_dump = getattr(value, "model_dump", None)
        return model_dump(mode="json") if callable(model_dump) else value

    tables = []
    for table in getattr(scl, "included_tables", []):
        tables.append(
            {
                "name": table.name,
                "description": table.description,
                "grain": table.grain,
                "aliases": list(table.aliases),
                "columns": [dump(column) for column in table.columns],
            }
        )

    return {
        "database": dump(scl.database),
        "tables": tables,
        "metrics": [dump(metric) for metric in scl.metrics],
        "joins": [dump(join) for join in scl.joins],
        "glossary": [dump(term) for term in scl.glossary],
        "examples": [dump(example) for example in scl.examples],
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui() -> str:
    """Serve the TachyonIQ production frontend."""
    return _PAGE
