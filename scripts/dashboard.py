"""Local, ledger-first workflow dashboard with explicit single-attempt dispatch."""

import argparse
import copy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

import workflow_control as control
import workflow_scheduler as scheduler
import workflow_status
from monitor_task import Transport, identity_matches, sequence, validate_request


SCRIPTS = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
RECENT_FILE = Path.home() / ".agent-workflow-orchestrator" / "dashboard-recent.json"
RECENT_LIMIT = 10
TASK_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
NONCE = re.compile(r"[0-9a-f]{16}\Z")
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workflow Dashboard</title>
<style nonce="__CSP_NONCE__">
:root { color-scheme: light dark; --bg:#f4f6f8; --surface:#fff; --ink:#172634; --muted:#607586; --line:#dbe3e8;
  --accent:#086d75; --accent-soft:#e0f3ef; --danger:#ad4334; --warn:#a66812; --shadow:0 10px 28px rgba(22,42,50,.07); }
@media (prefers-color-scheme:dark) { :root { --bg:#111c23; --surface:#1b2a32; --ink:#ecf2f3;
  --muted:#a7bbc3; --line:#34505b; --accent:#7dd4ca; --accent-soft:#21413f; --danger:#f29383;
  --warn:#e2b46c; --shadow:0 10px 28px rgba(0,0,0,.15); } }
* { box-sizing:border-box } html,body { margin:0; min-width:0 } body { background:var(--bg); color:var(--ink);
  font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; overflow-x:hidden }
.shell { max-width:1400px; margin:auto; padding:24px clamp(14px,3vw,34px) 70px }
header { display:flex; gap:20px; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; margin-bottom:23px }
.eyebrow { text-transform:uppercase; font-size:11px; letter-spacing:.16em; font-weight:800; color:var(--accent) }
h1 { margin:3px 0 6px; font-size:clamp(26px,3vw,39px); line-height:1.1; letter-spacing:-.035em }
h2 { margin:0 0 14px; font-size:19px; letter-spacing:-.025em }
h3 { margin:0 0 8px; font-size:16px } p { margin:7px 0 } small,.muted { color:var(--muted) }
.sub { color:var(--muted); max-width:900px; overflow-wrap:anywhere }
.toolbar { display:flex; align-items:center; gap:9px; flex-wrap:wrap }
button { cursor:pointer; border:1px solid var(--line); border-radius:9px; background:var(--surface); color:var(--ink);
  padding:9px 13px; font:inherit; font-weight:650 }
button:hover { border-color:var(--accent); color:var(--accent) } button:disabled { opacity:.45; cursor:default }
button.primary { background:var(--accent); color:var(--bg); border-color:var(--accent) }
input,select,textarea { min-width:0; width:100%; border:1px solid var(--line); border-radius:9px;
  background:var(--surface); color:var(--ink); font:inherit; padding:9px 11px }
textarea { min-height:116px; resize:vertical } input[type=checkbox] { width:auto; margin:0 5px 0 0 }
label { display:block; font-weight:650; font-size:13px } label span { display:block; margin-bottom:5px }
.inline-label { display:flex; align-items:center; font-size:13px; color:var(--muted); font-weight:600 }
.grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(280px,1fr); gap:18px }
.stack { display:grid; gap:18px } .panel { min-width:0; background:var(--surface); border:1px solid var(--line);
  border-radius:14px; padding:20px; box-shadow:var(--shadow) }
.wide { grid-column:1 / -1 } .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(235px,1fr)); gap:12px }
.role { border:1px solid var(--line); border-radius:11px; padding:14px; min-width:0 }
.role p { margin:5px 0; font-size:13px; overflow-wrap:anywhere } .role strong { color:var(--ink) }
.badge { display:inline-block; border-radius:30px; padding:3px 9px; font-size:11px; font-weight:800;
  letter-spacing:.025em; background:var(--accent-soft); color:var(--accent) }
.badge.accepted,.badge.submitted { background:#e2f4e9; color:#23744a }
.badge.executing,.badge.running { background:#e5efff; color:#275da6 }
.badge.execution_unconfirmed,.badge.scheduling_blocked,.badge.awaiting_review { background:#fff0d4; color:#84571b }
.badge.failed,.badge.review_blocked,.badge.needs_inspection { background:#ffebe6; color:#a54231 }
@media (prefers-color-scheme:dark) { .badge.accepted,.badge.submitted { background:#1d3e30;color:#98dab0 }
  .badge.executing,.badge.running { background:#203c59;color:#a9ccff }
  .badge.execution_unconfirmed,.badge.scheduling_blocked,.badge.awaiting_review { background:#47371b;color:#f2d097 }
  .badge.failed,.badge.review_blocked,.badge.needs_inspection { background:#4d302d;color:#f9b1a6 } }
.table-scroll { max-width:100%; overflow-x:auto } table { width:100%; border-collapse:collapse; font-size:13px }
th { text-align:left; color:var(--muted); font-size:11px; letter-spacing:.06em; text-transform:uppercase }
th,td { padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top; overflow-wrap:anywhere }
tr.task-row { cursor:pointer } tr.task-row:hover { background:var(--accent-soft) }
.detail { background:var(--bg); padding:13px; border-radius:9px; overflow-wrap:anywhere }
pre { margin:7px 0; white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
  background:var(--bg); border:1px solid var(--line); padding:12px; border-radius:9px; max-height:320px; overflow:auto }
.dag-scroll { width:100%; overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--bg) }
svg { display:block; width:100%; min-width:0; height:auto } svg text { font:12px system-ui,sans-serif; fill:var(--ink) }
svg line { stroke:var(--muted); stroke-width:1.5 } svg rect { fill:var(--surface); stroke:var(--line); stroke-width:1.5 }
svg rect.accepted { stroke:#3baf70; stroke-width:2 } svg rect.executing { stroke:#478cd7; stroke-width:2 }
svg rect.scheduling_blocked,svg rect.review_blocked { stroke:var(--warn); stroke-width:2 }
.list { display:grid; gap:9px } .list-item { padding:9px 0; border-bottom:1px solid var(--line); overflow-wrap:anywhere }
.meter { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:center; margin:8px 0 }
progress { width:100%; height:12px; accent-color:var(--accent) }
.form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:13px }
.form-grid .full { grid-column:1 / -1 } .actions { display:flex; gap:9px; flex-wrap:wrap; margin-top:15px }
.notice { border-left:3px solid var(--accent); background:var(--accent-soft); padding:9px 12px; border-radius:5px; overflow-wrap:anywhere }
.error { color:var(--danger); font-weight:700 } .success { color:var(--accent); font-weight:700 }
#picker { margin-bottom:18px } .path-row { display:flex; gap:9px; align-items:flex-end; margin-top:12px }
.path-row label { flex:1; min-width:0 } .recent-item { text-align:left; width:100%; overflow-wrap:anywhere; font-weight:500 }
code { font:13px ui-monospace,SFMono-Regular,Consolas,monospace }
.hidden { display:none !important } .small-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap }
@media(max-width:800px) { .grid { grid-template-columns:1fr } .wide { grid-column:auto } }
@media(max-width:520px) { .shell { padding:16px 12px 44px } .panel { padding:14px } .form-grid { grid-template-columns:1fr }
  .form-grid .full { grid-column:auto } .cards { grid-template-columns:1fr } .table-scroll { overflow:visible }
  table,tbody,tr,td { display:block; width:100% } thead { display:none }
  tr.task-row { display:grid; grid-template-columns:1fr 1fr; gap:5px 8px; padding:11px 0; border-bottom:1px solid var(--line) }
  tr.task-row td { border:0; padding:0; min-width:0 } tr.task-row td:first-child { grid-column:1 / -1; font-weight:800 }
  tr.detail-row td { border:0; padding:7px 0 } }
</style>
</head>
<body>
<div class="shell">
  <header><div><div class="eyebrow">LOCAL / LEDGER VIEW</div><h1>Workflow Dashboard</h1>
    <p class="sub" id="state">Loading state...</p><p class="sub" id="goal"></p></div>
    <div class="toolbar"><label class="inline-label"><input id="auto" type="checkbox" checked>Auto refresh / 5s</label>
      <button id="refresh" type="button">Refresh ledger</button><button id="probe" type="button">Probe workers</button>
      <button id="switch" type="button" class="hidden">Switch state</button></div></header>
  <p class="sub" id="updated">Waiting for first refresh</p><p class="error" id="error" role="alert"></p>
  <section class="panel hidden" id="picker"><h2>Open a workflow state</h2>
    <p class="muted">Choose a recent state directory, or paste the absolute path given to <code>init</code>.</p>
    <div id="recent" class="list"></div>
    <div class="path-row"><label><span>State directory</span><input id="state-input" autocomplete="off"
      placeholder="E:\work\state  or  /home/me/work/state"></label>
      <button id="open-state" class="primary" type="button">Open</button></div>
    <p id="picker-error" class="error" role="alert"></p></section>
  <div class="grid" id="main">
    <section class="panel wide"><h2>Roles & bindings</h2><div id="roles" class="cards"></div></section>
    <section class="panel wide"><h2>Tasks</h2><div class="table-scroll"><table><thead><tr><th>Task</th><th>Phase</th>
      <th>Worker</th><th>Dispatch</th><th>Claim</th><th>Review</th><th>Next</th><th>Observed</th><th>Alerts</th>
      </tr></thead><tbody id="tasks"></tbody></table></div></section>
    <section class="panel wide"><h2>DAG runs</h2><div id="dags" class="stack"></div></section>
    <section class="panel"><h2>Inbox</h2><div id="inbox" class="list"></div></section>
    <section class="panel"><h2>Resources & claims</h2><div id="resources"></div></section>
    <section class="panel"><h2>Alerts</h2><div id="alerts" class="list"></div></section>
    <section class="panel"><h2>Live probe</h2><p class="muted">Only the button above contacts workers.</p>
      <div id="probe-result" class="list"></div></section>
    <section class="panel wide" id="dispatch-panel"><h2>Dispatch a task</h2>
      <p class="muted" id="dispatch-note"></p>
      <div id="dispatch-form" class="hidden">
        <div class="form-grid">
          <label><span>Worker role</span><select id="worker-role"></select></label>
          <label><span>Task ID</span><input id="task-id" autocomplete="off" placeholder="TASK-123" maxlength="64"></label>
          <label><span>Nonce (generated by preview)</span><input id="nonce" readonly></label>
          <label><span>Authorization source</span><input id="source" placeholder="Current owner instruction"></label>
          <label><span>Binding artifact</span><input id="binding" value="HANDOFF.md"></label>
          <label><span>Other artifacts (comma separated)</span><input id="artifacts" value="RESULT.md"></label>
          <label><span>Resources (JSON)</span><input id="resource-input" value="{}"></label>
          <label class="inline-label"><input id="notify" type="checkbox">Authorize direct reviewer prompt when policy permits</label>
          <label class="full"><span>Task body: objective, scope, acceptance</span>
            <textarea id="body" placeholder="Objective:&#10;Allowed changes:&#10;Acceptance:"></textarea></label>
        </div><div class="actions"><button id="preview" class="primary" type="button">Preview & validate</button>
          <button id="confirm" type="button" disabled>Confirm one-time dispatch</button></div>
        <p id="dispatch-error" class="error" role="alert"></p>
        <div id="preview-output" class="hidden"><h3>Frozen preview</h3><p class="muted">Review the full request and message before confirming.</p>
          <pre id="preview-request"></pre><pre id="preview-message"></pre></div>
        <div id="dispatch-output" class="hidden"><h3>Dispatch result</h3><p id="dispatch-status"></p>
          <p class="notice">Independently confirm execution before writing EXECUTION.md and running reconcile. Never resend an uncertain dispatch.</p>
          <div class="small-actions"><strong>EXECUTION.md template</strong><button type="button" data-copy="execution-template">Copy</button></div>
          <pre id="execution-template"></pre><div class="small-actions"><strong>Reconcile command</strong>
          <button type="button" data-copy="reconcile-command">Copy</button></div><pre id="reconcile-command"></pre>
          <div class="small-actions"><strong>Then monitor</strong><button type="button" data-copy="monitor-command">Copy</button></div>
          <pre id="monitor-command"></pre></div>
      </div></section>
  </div>
</div>
<script nonce="__CSP_NONCE__">
(() => {
  const id = name => document.getElementById(name);
  const token = location.hash.slice(1) || sessionStorage.getItem('dashboard-token') || '';
  if (token) { sessionStorage.setItem('dashboard-token', token); history.replaceState(null, '', location.pathname); }
  let preview = null;
  let lastData = null;
  const el = (tag, className, value) => {
    const x = document.createElement(tag);
    if (className) x.className = className;
    if (value !== undefined && value !== null) x.textContent = String(value);
    return x;
  };
  const add = (parent, tag, className, value) => { const x = el(tag, className, value); parent.append(x); return x; };
  const show = (name, visible) => id(name).classList.toggle('hidden', !visible);
  async function api(path, payload) {
    if (!token) throw new Error('Open the URL printed by the dashboard server.');
    const options = {headers: {'X-Dashboard-Token': token}};
    if (payload !== undefined) { options.method = 'POST'; options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(payload); }
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function empty(container, message) { container.replaceChildren(); add(container, 'p', 'muted', message); }
  function roles(data) {
    const root = id('roles'); root.replaceChildren();
    for (const role of data.roles) {
      const card = add(root, 'article', 'role'); add(card, 'h3', '', role.role);
      if (!role.bindings.length) add(card, 'p', 'muted', 'No active or pending binding');
      for (const b of role.bindings) {
        const p = add(card, 'p'); p.append(el('span', 'badge ' + b.status, b.status));
        for (const [key, value] of [['Mode', b.mode], ['Harness', b.harness], ['Adapter', b.adapter],
          ['Version', b.version], ['Policy', b.context_policy], ['Instance', b.instance_id], ['Bound', b.bound_at]]) {
          const row = add(card, 'p'); add(row, 'strong', '', key + ': '); row.append(document.createTextNode(String(value ?? 'unknown')));
        }
      }
      const tasks = role.tasks.map(t => t.task_id + ' / ' + t.phase).join(', ');
      add(card, 'p', 'muted', 'Open tasks: ' + (tasks || 'none'));
    }
  }
  function taskTable(data) {
    const root = id('tasks'); root.replaceChildren();
    if (!data.tasks.length) { const tr = add(root, 'tr'); const td = add(tr, 'td', 'muted', 'No tasks yet'); td.colSpan = 9; return; }
    for (const t of data.tasks) {
      const tr = add(root, 'tr', 'task-row');
      add(tr, 'td', '', t.task_id); const phase = add(tr, 'td'); add(phase, 'span', 'badge ' + t.phase, t.phase);
      add(tr, 'td', '', t.worker_role + ' / ' + t.harness + ' / ' + t.adapter);
      add(tr, 'td', '', t.dispatch || 'none'); add(tr, 'td', '', t.claim_held ? 'held' : 'none');
      add(tr, 'td', '', t.review_verdicts.join(', ') || 'none');
      add(tr, 'td', '', t.next_role + ' / ' + t.next_action);
      add(tr, 'td', '', t.observation_age_seconds === null ? 'unavailable' : Math.round(t.observation_age_seconds) + 's');
      add(tr, 'td', '', t.warnings.join(', ') || 'none');
      const detailRow = add(root, 'tr', 'detail-row hidden'); const td = add(detailRow, 'td'); td.colSpan = 9;
      const detail = add(td, 'div', 'detail');
      const info = data.task_details[t.task_id] || {};
      add(detail, 'p', '', 'Dependencies: ' + ((t.dependencies || []).join(', ') || 'none'));
      add(detail, 'p', '', 'Block: ' + (t.blocking_reason || 'none'));
      add(detail, 'p', '', 'Cwd: ' + (info.cwd || 'unknown'));
      const journal = (info.journal || []).map(j => [j.at, j.kind, j.actor].join(' | ')).join('\n');
      add(detail, 'pre', '', journal || 'No journal entries');
      tr.addEventListener('click', () => detailRow.classList.toggle('hidden'));
    }
  }
  const svgEl = tag => document.createElementNS('http://www.w3.org/2000/svg', tag);
  function dagGraph(data) {
    const root = id('dags'); root.replaceChildren();
    const runs = Object.entries(data.dags);
    if (!runs.length) { empty(root, 'No DAG runs'); return; }
    for (const [run, dag] of runs) {
      const box = add(root, 'div'); add(box, 'h3', '', run);
      const levels = {}, visiting = new Set();
      const level = name => { if (levels[name] !== undefined) return levels[name];
        if (visiting.has(name)) return 0; visiting.add(name);
        const deps = dag.dependencies[name] || [];
        const n = deps.length ? 1 + Math.max(...deps.map(level)) : 0;
        visiting.delete(name); levels[name] = n; return n; };
      Object.keys(dag.nodes).forEach(level);
      const groups = []; for (const name of Object.keys(dag.nodes)) (groups[levels[name]] ||= []).push(name);
      const width = Math.max(420, groups.length * 190 + 30);
      const height = Math.max(120, Math.max(...groups.map(g => g.length)) * 88 + 20);
      const wrap = add(box, 'div', 'dag-scroll'); const svg = svgEl('svg');
      svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height); svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', 'Dependency graph for ' + run); wrap.append(svg);
      const pos = {}; groups.forEach((group, col) => group.forEach((name, row) => { pos[name] = [20 + col * 190, 17 + row * 88]; }));
      for (const [name, deps] of Object.entries(dag.dependencies)) for (const dep of deps) {
        if (!pos[name] || !pos[dep]) continue;
        const line = svgEl('line'); line.setAttribute('x1', pos[dep][0] + 145); line.setAttribute('y1', pos[dep][1] + 25);
        line.setAttribute('x2', pos[name][0]); line.setAttribute('y2', pos[name][1] + 25); svg.append(line);
      }
      for (const [name, state] of Object.entries(dag.nodes)) {
        const [x,y] = pos[name]; const rect = svgEl('rect'); rect.setAttribute('x', x); rect.setAttribute('y', y);
        rect.setAttribute('width', 145); rect.setAttribute('height', 54); rect.setAttribute('rx', 8);
        rect.setAttribute('class', state); svg.append(rect);
        const label = svgEl('text'); label.setAttribute('x', x + 9); label.setAttribute('y', y + 21);
        label.textContent = name; svg.append(label);
        const sub = svgEl('text'); sub.setAttribute('x', x + 9); sub.setAttribute('y', y + 40);
        sub.textContent = state; svg.append(sub);
      }
    }
  }
  function lists(data) {
    const inbox = id('inbox'); inbox.replaceChildren();
    if (!data.inbox.length) empty(inbox, 'No events');
    else for (const e of data.inbox) add(inbox, 'div', 'list-item', e.event_id + ' / ' + e.task_id +
      ' / ' + e.status + ' / ' + (e.report_status || 'unknown') + ' / ' + (e.created_at || ''));
    const alerts = id('alerts'); alerts.replaceChildren();
    if (!data.alerts.length && !Object.keys(data.scheduling_blocks).length) empty(alerts, 'No alerts');
    else { for (const a of data.alerts) add(alerts, 'div', 'list-item', a.task_id + ': ' + a.warnings.join(', '));
      for (const [task, block] of Object.entries(data.scheduling_blocks)) add(alerts, 'div', 'list-item', task + ': ' + block.reason); }
    const resources = id('resources'); resources.replaceChildren();
    const caps = data.resource_capacities;
    if (!Object.keys(caps).length) add(resources, 'p', 'muted', 'No resource capacities configured');
    for (const [name, cap] of Object.entries(caps)) {
      const used = data.claims.reduce((sum,c) => sum + (c.resources[name] || 0), 0);
      const row = add(resources, 'div', 'meter'); const bar = add(row, 'div'); add(bar, 'strong', '', name);
      const meter = add(bar, 'progress'); meter.max = cap; meter.value = used;
      add(row, 'strong', '', used + ' / ' + cap);
    }
    add(resources, 'p', 'muted', data.claims.length + ' held claim(s)');
  }
  function workers(data) {
    const select = id('worker-role'), selected = select.value;
    select.replaceChildren();
    for (const role of data.roles) if (role.bindings.some(b => b.status === 'active' && b.mode === 'worker')) {
      const option = el('option', '', role.role); option.value = role.role; select.append(option);
    }
    if ([...select.options].some(x => x.value === selected)) select.value = selected;
  }
  function picker(data, open) {
    show('picker', open); show('main', !open && data.state !== null);
    show('switch', data.can_switch && data.state !== null); show('probe', data.state !== null);
    id('switch').textContent = open && data.state !== null ? 'Cancel' : 'Switch state';
    if (!open) return;
    const root = id('recent'); root.replaceChildren();
    if (!data.recent.length) empty(root, 'No recent state directories');
    for (const path of data.recent) {
      const button = add(root, 'button', 'recent-item', path); button.type = 'button';
      if (path === data.state) button.disabled = true;
      button.addEventListener('click', () => openState(path));
    }
  }
  let picking = false;
  async function openState(path) {
    id('picker-error').textContent = '';
    try { await api('/api/state', {state: path}); picking = false; id('state-input').value = ''; await refresh(); }
    catch (error) { id('picker-error').textContent = error.message; }
  }
  async function refresh() {
    try { const data = await api('/api/status'); lastData = data; id('error').textContent = '';
      picker(data, picking || data.state === null);
      if (data.state === null) { id('state').textContent = 'No workflow state open'; id('goal').textContent = '';
        id('updated').textContent = 'Pick a state directory to begin'; return; }
      id('state').textContent = data.state;
      id('goal').textContent = data.goal ? 'Goal: ' + data.goal : 'No workflow goal configured';
      id('updated').textContent = 'Last ledger refresh: ' + new Date().toLocaleTimeString();
      roles(data); taskTable(data); dagGraph(data); lists(data); workers(data);
      show('dispatch-form', data.allow_dispatch);
      id('dispatch-note').textContent = data.allow_dispatch ?
        'Preview checks a live idle worker. Confirm reserves and sends once through the scheduler.' :
        'Dispatch is disabled. Restart with --allow-dispatch, --actor, --version, --context main, and --tasks-root to enable it.';
    } catch (error) { id('error').textContent = error.message; }
  }
  function form(includeNonce) {
    let resources; try { resources = JSON.parse(id('resource-input').value); } catch { throw new Error('Resources must be JSON'); }
    const value = {worker_role:id('worker-role').value, task_id:id('task-id').value, body:id('body').value,
      source:id('source').value, binding_artifact:id('binding').value,
      artifacts:id('artifacts').value.split(',').map(x => x.trim()),
      notify_reviewer:id('notify').checked, resources};
    if (includeNonce) value.nonce = id('nonce').value;
    return value;
  }
  function invalidate() { preview = null; id('confirm').disabled = true; show('preview-output', false); }
  for (const name of ['worker-role','task-id','body','source','binding','artifacts','resource-input','notify'])
    id(name).addEventListener('input', invalidate);
  id('preview').addEventListener('click', async () => {
    invalidate(); id('dispatch-error').textContent = ''; show('dispatch-output', false);
    id('preview').disabled = true;
    try { preview = await api('/api/dispatch/preview', form(false)); id('nonce').value = preview.form.nonce;
      id('preview-request').textContent = JSON.stringify(preview.request, null, 2);
      id('preview-message').textContent = preview.message; show('preview-output', true);
      id('confirm').disabled = false;
    } catch (error) { id('dispatch-error').textContent = error.message; }
    finally { id('preview').disabled = false; }
  });
  id('confirm').addEventListener('click', async () => {
    if (!preview) return; const digest = preview.preview_sha256;
    id('confirm').disabled = true; id('dispatch-error').textContent = '';
    try { const result = await api('/api/dispatch/confirm', {...form(true), preview_sha256:digest});
      id('dispatch-status').textContent = result.status;
      id('dispatch-status').className = result.status === 'SUBMITTED' ? 'success' : 'error';
      for (const key of ['execution-template','reconcile-command','monitor-command']) id(key).textContent = result[key];
      show('dispatch-output', true); await refresh();
    } catch (error) { id('dispatch-error').textContent = error.message + ' Inspect the ledger before trying another task.'; }
    preview = null;
  });
  id('probe').addEventListener('click', async () => {
    id('probe').disabled = true; const root = id('probe-result'); empty(root, 'Probing...');
    try { const data = await api('/api/probe', {}); root.replaceChildren();
      if (!data.workers.length) empty(root, 'No active workers');
      for (const w of data.workers) add(root, 'div', 'list-item', w.role + ': ' +
        (w.error || (w.status + ' / seq ' + w.completion_seq + ' / identity ' + w.identity_matches)));
    } catch (error) { empty(root, error.message); } finally { id('probe').disabled = false; }
  });
  document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    await navigator.clipboard.writeText(id(button.dataset.copy).textContent); button.textContent = 'Copied';
  }));
  id('switch').addEventListener('click', () => { picking = !picking; if (lastData) picker(lastData, picking); });
  id('open-state').addEventListener('click', () => openState(id('state-input').value));
  id('state-input').addEventListener('keydown', event => { if (event.key === 'Enter') openState(id('state-input').value); });
  id('refresh').addEventListener('click', refresh);
  setInterval(() => { if (id('auto').checked) refresh(); }, 5000);
  refresh();
})();
</script>
</body>
</html>"""


def _digest(request, message, resources):
    # Resources are scheduler arguments, not request fields, but still part of the frozen preview.
    frozen = json.dumps({"request": request, "resources": resources}, sort_keys=True,
                        separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((frozen + "\n" + message).encode("utf-8")).hexdigest()


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _artifact_path(value, cwd):
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ValueError("delivery path must be a nonempty file path")
    path = Path(value)
    if path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ValueError("delivery path must stay inside the task directory")
    resolved = (cwd / path).resolve()
    if resolved == cwd.resolve():
        raise ValueError("delivery path must name a file")
    resolved.relative_to(cwd.resolve())
    return value


def _form(payload, nonce=None):
    if not isinstance(payload, dict):
        raise ValueError("form must be a JSON object")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id) or task_id in (".", ".."):
        raise ValueError("invalid task ID")
    nonce = secrets.token_hex(8) if nonce is None else nonce
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise ValueError("invalid nonce")
    worker_role = payload.get("worker_role")
    if not isinstance(worker_role, str) or not worker_role:
        raise ValueError("select a worker role")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > 20000:
        raise ValueError("task body must contain 1 to 20000 characters")
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip() or len(source) > 2000:
        raise ValueError("authorization source is required")
    notify = payload.get("notify_reviewer", False)
    if type(notify) is not bool:
        raise ValueError("notify_reviewer must be boolean")
    binding = payload.get("binding_artifact", "HANDOFF.md")
    artifacts = payload.get("artifacts", ["RESULT.md"])
    if not isinstance(artifacts, list) or not artifacts or any(not isinstance(x, str) for x in artifacts):
        raise ValueError("artifacts must be a nonempty list of paths")
    resources = payload.get("resources", {})
    if not isinstance(resources, dict) or any(not isinstance(k, str) or not k or type(v) is not int or v < 1
                                               for k, v in resources.items()):
        raise ValueError("resources must map names to positive integer units")
    return {"task_id": task_id, "nonce": nonce, "worker_role": worker_role, "body": body,
            "source": source.strip(), "notify_reviewer": notify, "binding_artifact": binding,
            "artifacts": artifacts, "resources": resources}


def _state_path(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("state path must be a nonempty string")
    # Accept pasted paths with surrounding quotes, and the ledger file itself.
    path = Path(value.strip().strip('"').strip("'")).expanduser()
    if not path.is_absolute():
        raise ValueError("state path must be absolute")
    path = path.resolve()
    if path.name == "workflow.sqlite3" and path.is_file():
        path = path.parent
    if not (path / "workflow.sqlite3").is_file():
        raise ValueError("workflow state is not initialized: " + str(path))
    return path


def _recent(recent_file):
    if recent_file is None:
        return []
    try:
        values = json.loads(Path(recent_file).read_text(encoding="utf-8")).get("states", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [value for value in values if isinstance(value, str)][:RECENT_LIMIT]


def _remember(recent_file, state):
    """Record a state path for the picker; failure only loses the convenience list."""
    if recent_file is None:
        return
    path = Path(recent_file)
    values = [str(state)] + [value for value in _recent(path) if value != str(state)]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
        temporary.write_text(json.dumps({"states": values[:RECENT_LIMIT]}, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR lets a second socket bind an in-use port; keep port conflicts visible there.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, state=None, *, allow_dispatch=False, actor=None, version=None, context=None,
                 tasks_root=None, transport_factory=Transport, recent_file=None):
        host, port = address
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("dashboard host must be loopback: 127.0.0.1 or localhost")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if state is None and allow_dispatch:
            raise ValueError("dispatch needs --state")
        self.state = None if state is None else _state_path(str(Path(state).resolve()))
        self.recent_file = recent_file
        self.allow_dispatch = bool(allow_dispatch)
        self.actor, self.version, self.context = actor, version, context
        self.tasks_root = None
        self.transport_factory = transport_factory
        self.token = secrets.token_urlsafe(32)
        self.csp_nonce = secrets.token_urlsafe(16)
        self.preview_records = {}
        self.preview_lock = threading.Lock()
        self.task_locks = {}
        if self.allow_dispatch:
            if not actor or type(version) is not int or version < 1 or context != "main" or tasks_root is None:
                raise ValueError("dispatch needs --actor, --version, --context main, and --tasks-root")
            root = Path(tasks_root)
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("tasks root must be an existing absolute directory")
            self.tasks_root = root.resolve()
            with scheduler._read_database(self.state) as con:
                binding = control.guard(con, actor, version, context, "dispatch")
                if binding["mode"] != "controller":
                    raise ValueError("actor is not the active controller")
        super().__init__((host, port), DashboardHandler)
        if self.state is not None:
            _remember(self.recent_file, self.state)

    def origin(self, host=None):
        return "http://" + (host or self.server_address[0] + ":" + str(self.server_port))

    def status_data(self):
        state = self.state
        recent = [value for value in _recent(self.recent_file) if (Path(value) / "workflow.sqlite3").is_file()]
        base = {"allow_dispatch": self.allow_dispatch, "can_switch": not self.allow_dispatch, "recent": recent}
        if state is None:
            return dict(base, read_only=True, state=None)
        summary = workflow_status.summarize(state)
        details = workflow_status.dashboard_details(state, summary)
        return dict(summary, **details, **base, state=str(state))

    def select_state(self, payload):
        # Viewing only: dispatch mode is bound to the controller checked at startup.
        if self.allow_dispatch:
            raise PermissionError("state is fixed while dispatch is enabled")
        state = _state_path(payload.get("state"))
        self.state = state
        _remember(self.recent_file, state)
        return {"state": str(state)}

    def probe(self):
        if self.state is None:
            raise ValueError("open a workflow state first")
        with scheduler._read_database(self.state) as con:
            bindings = [json.loads(row[0]) for row in con.execute(
                "SELECT data FROM bindings WHERE status='active' AND mode='worker' ORDER BY role")]
        result = []
        for binding in bindings:
            spec = binding["spec"]
            try:
                observed = self.transport_factory({"monitor": {"command_timeout_seconds": 10}}).status(spec)
                result.append({"role": binding["role"], "status": observed.get("status"),
                               "completion_seq": observed.get("completion_seq"),
                               "identity_matches": identity_matches(observed.get("identity", {}), spec["expected_identity"])})
            except Exception as exc:
                result.append({"role": binding["role"], "error": str(exc)[:300]})
        return {"workers": result}

    def prepare(self, payload, *, confirm=False):
        if confirm and "nonce" not in payload:
            raise ValueError("confirm requires the nonce returned by preview")
        form = _form(payload, payload.get("nonce") if confirm else None)
        cwd = (self.tasks_root / form["task_id"]).resolve()
        if cwd.parent != self.tasks_root or cwd.exists():
            raise FileExistsError("task directory already exists or is outside tasks root")
        binding_artifact = _artifact_path(form["binding_artifact"], cwd)
        artifacts = [_artifact_path(value, cwd) for value in form["artifacts"]]
        if binding_artifact in artifacts:
            raise ValueError("binding artifact must differ from other artifacts")
        with scheduler._read_database(self.state) as con:
            cfg = control.config_of(con)
            reviewer_role = cfg.get("workflow", {}).get("reviewer_role")
            if not reviewer_role:
                raise ValueError("workflow reviewer_role is required")
            def bound(role):
                row = con.execute("SELECT data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
                if not row:
                    raise ValueError("role has no active binding: " + role)
                return json.loads(row[0])
            worker = bound(form["worker_role"])
            reviewer = bound(reviewer_role)
            if worker["mode"] != "worker" or reviewer["mode"] not in ("controller", "reviewer"):
                raise ValueError("selected roles do not hold worker/reviewer modes")
            if worker["spec"]["adapter"] not in ("command", "herdr"):
                raise ValueError("worker adapter cannot accept scheduler dispatch")
        worker_spec = copy.deepcopy(worker["spec"])
        reviewer_spec = copy.deepcopy(reviewer["spec"])
        observed = self.transport_factory({"monitor": {"command_timeout_seconds": 10}}).status(worker_spec)
        if not identity_matches(observed.get("identity", {}), worker_spec["expected_identity"]):
            raise ValueError("worker identity changed")
        baseline = sequence(observed.get("completion_seq"))
        worker_spec["baseline_completion_seq"] = baseline
        marker = "TASK_DONE " + form["task_id"] + " " + form["nonce"]
        request = {"task_id": form["task_id"], "nonce": form["nonce"], "generation": 1,
                   "cwd": str(cwd), "workflow_state": str(self.state), "worker": worker_spec,
                   "reviewer": reviewer_spec, "dispatch_confirmed": False,
                   "authorization": {"source": form["source"], "dispatch_worker": True,
                                     "notify_reviewer": form["notify_reviewer"]},
                   "completion": {"binding_artifact": binding_artifact, "artifacts": artifacts,
                                  "marker": marker, "marker_source": "artifact"}}
        if not scheduler._worker_quiescent(request, observed):
            raise ValueError("worker is not idle")
        # Structural validation uses the existing root until the task directory exists.
        # The exact request is validated again after exclusive mkdir, before dispatch.
        structural = copy.deepcopy(request)
        structural["cwd"] = str(self.tasks_root)
        validate_request(structural, require_dispatch=False)
        message = ("Task ID: " + form["task_id"] + "\nNonce: " + form["nonce"] +
                   "\nCwd: " + str(cwd) + "\nCompletion marker: " + marker + "\n\n" +
                   form["body"].rstrip() + "\n\nDeliverables: write " +
                   ", ".join([binding_artifact] + artifacts) + ". The final line of " +
                   binding_artifact + " must be exactly:\n" + marker + "\n")
        return {"form": form, "request": request, "message": message,
                "preview_sha256": _digest(request, message, form["resources"]),
                "baseline_completion_seq": baseline}

    def preview(self, payload):
        result = self.prepare(payload)
        with self.preview_lock:
            self.preview_records[result["preview_sha256"]] = time.monotonic()
        return result

    def confirm(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("preview_sha256"), str):
            raise ValueError("preview_sha256 is required")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task ID")
        with self.preview_lock:
            lock = self.task_locks.setdefault(task_id, threading.Lock())
        with lock:
            result = self.prepare(payload, confirm=True)
            digest = result["preview_sha256"]
            if not hmac.compare_digest(payload["preview_sha256"], digest):
                raise ValueError("preview changed; preview again before dispatch")
            with self.preview_lock:
                created = self.preview_records.pop(digest, None)
            if created is None or time.monotonic() - created > 600:
                raise ValueError("preview missing or expired; preview again")
            request, message = result["request"], result["message"]
            cwd = Path(request["cwd"])
            cwd.mkdir(exist_ok=False)
            try:
                validate_request(request, require_dispatch=False)
            except Exception:
                cwd.rmdir()  # still empty; leaves the task ID usable after a validation failure
                raise
            with (cwd / "task.md").open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(message)
            with (cwd / "request.json").open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(request, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
            outcome = scheduler.dispatch(self.state, request, message, self.actor, self.version, "main",
                                         resources=result["form"]["resources"],
                                         transport=self.transport_factory(request))
            return {"status": outcome["status"], "result": outcome, "request": request,
                    "reconcile_command": "& " + _ps_quote(sys.executable) + " -B " + _ps_quote(SCRIPTS / "workflow_scheduler.py") +
                    " --state " + _ps_quote(self.state) +
                    " reconcile --task " + request["task_id"] + " --outcome running --evidence " +
                    _ps_quote(cwd / "EXECUTION.md") + " --request-out " +
                    _ps_quote(cwd / "confirmed.json") + " --actor " + self.actor +
                    " --version " + str(self.version) + " --context main",
                    "monitor_command": "& " + _ps_quote(sys.executable) + " -B " + _ps_quote(SCRIPTS / "monitor_task.py") +
                    " --request " +
                    _ps_quote(cwd / "confirmed.json") + " --notify",
                    "execution_template": "Task ID: " + request["task_id"] + "\nNonce: " + request["nonce"] +
                    "\nOutcome: running\nObserved execution: \n"}


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format, *args):
        pass

    def _headers(self, status, content_type, length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" +
                         self.server.csp_nonce + "'; style-src 'nonce-" + self.server.csp_nonce +
                         "'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()

    def _json(self, status, value):
        body = json.dumps(value, ensure_ascii=True).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _host(self):
        allowed = {"127.0.0.1:" + str(self.server.server_port),
                   "localhost:" + str(self.server.server_port)}
        return self.headers.get("Host", "").lower() in allowed

    def _token(self):
        supplied = self.headers.get("X-Dashboard-Token", "")
        return hmac.compare_digest(supplied, self.server.token)

    def _origin(self):
        host = self.headers.get("Host", "").lower()
        return self.headers.get("Origin") == "http://" + host

    def _body(self):
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if not 0 < length <= 100000:
            raise ValueError("request body must be 1 to 100000 bytes")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self):
        if not self._host():
            self._error(403, "invalid Host")
            return
        path = urlsplit(self.path).path
        if path == "/":
            body = HTML.replace("__CSP_NONCE__", self.server.csp_nonce).encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if not self._token():
            self._error(403, "invalid dashboard token")
            return
        if path != "/api/status":
            self._error(404, "not found")
            return
        try:
            self._json(200, self.server.status_data())
        except Exception:
            self._error(500, "status read failed")

    def do_POST(self):
        if not self._host():
            self._error(403, "invalid Host")
            return
        if not self._origin():
            self._error(403, "invalid Origin")
            return
        if not self._token():
            self._error(403, "invalid dashboard token")
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/dispatch/") and not self.server.allow_dispatch:
            self._error(403, "dispatch is disabled")
            return
        if path not in ("/api/state", "/api/probe", "/api/dispatch/preview", "/api/dispatch/confirm"):
            self._error(404, "not found")
            return
        try:
            payload = self._body()
            if path == "/api/state":
                result = self.server.select_state(payload)
            elif path == "/api/probe":
                result = self.server.probe()
            elif path.endswith("/preview"):
                result = self.server.preview(payload)
            else:
                result = self.server.confirm(payload)
            self._json(200, result)
        except PermissionError as exc:
            self._error(403, str(exc))
        except FileExistsError as exc:
            self._error(409, str(exc))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._error(400, str(exc)[:300])
        except Exception:
            self._error(500, "operation failed; inspect the ledger before any retry")


def open_server(host, port, state, **options):
    """Bind the requested port; when the default port is busy, fall back to a free one."""
    try:
        return DashboardServer((host, port), state, **options)
    except OSError:
        if port != DEFAULT_PORT:
            raise
        return DashboardServer((host, 0), state, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="workflow state directory; omit to choose one in the page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")
    parser.add_argument("--allow-dispatch", action="store_true")
    parser.add_argument("--actor")
    parser.add_argument("--version", type=int)
    parser.add_argument("--context", choices=("main", "side", "unknown"))
    parser.add_argument("--tasks-root")
    args = parser.parse_args(argv)
    try:
        server = open_server(args.host, args.port, args.state, allow_dispatch=args.allow_dispatch,
                             actor=args.actor, version=args.version, context=args.context,
                             tasks_root=args.tasks_root, recent_file=RECENT_FILE)
    except (ValueError, OSError) as exc:
        parser.exit(2, "dashboard: " + str(exc) + "\n")
    with server:
        url = server.origin() + "/#" + server.token
        print("Dashboard: " + url, flush=True)
        print("State: " + (str(server.state) if server.state else "choose in the page"), flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
