/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
const $ = id => document.getElementById(id);
let state = null, received = 0, authenticated = false, csrf = null, streamOpen = false;
let pageClients;
const clientsReady = import("./client-page.mjs").then(({createPageClients}) => {
  pageClients = createPageClients({headers: () => csrf ? {"X-CSRF-Token": csrf} : {}});
  return pageClients;
});
let logRun = null, logCursor = null, loadingLogs = false;
let pending = Object.create(null);
const reconciling = new Set();
try {
  const saved = JSON.parse(sessionStorage.getItem("runtime-pending") || "{}");
  for (const [unit, id] of Object.entries(saved).slice(-20)) if (typeof id === "string") pending[unit] = id;
} catch { /* Empty initial history. */ }
function savePending() { sessionStorage.setItem("runtime-pending", JSON.stringify(pending)); }
function notice(message) { $("notice").textContent = message; }
async function api(path, options = {}) {
  const clients = await clientsReady;
  try {
    return await clients.http.request(path.replace(/^\//, ""), {
      method: options.method || "GET", body: options.body === undefined ? undefined : JSON.parse(options.body),
      signal: options.signal, timeoutMs: path.includes("/snapshots/") ? 10000 : 5000
    });
  } catch (error) {
    if (error.status === 401) { authenticated = false; csrf = null; renderAccess(); }
    throw error;
  }
}
function renderAccess() {
  $("unlock").hidden = authenticated;
  $("lock").hidden = !authenticated;
  $("access-status").textContent = authenticated ? "Commands unlocked for this browser. Lock them when finished." : "Viewing is public. Unlock to start or stop a workload.";
  render();
  window.runtimeRos?.updateRuntime(state);
  window.runtimeParameters?.updateRuntime(state);
}
function fresh() {
  return !!(streamOpen && state?.fresh && state.age_seconds !== null && state.age_seconds + (performance.now() - received) / 1000 <= 15);
}
function updateAge() {
  const valid = fresh();
  $("connection").textContent = valid ? "Live observations" : "Observations unavailable";
  $("connection").className = `badge ${valid ? "live" : "warning"}`;
  $("age").textContent = state?.age_seconds !== null && state?.age_seconds !== undefined ? `Last audit ${Math.max(0, Math.floor(state.age_seconds + (performance.now() - received) / 1000))}s ago` : "Waiting for the manager";
  document.querySelectorAll("button[data-command]").forEach(button => { button.disabled = !valid || !authenticated || button.dataset.blocked === "true"; });
}
function node(tag, content, className) {
  const result = document.createElement(tag);
  if (content !== undefined) result.textContent = content;
  if (className) result.className = className;
  return result;
}
function render() {
  if (!state) return;
  $("robot").textContent = state.robot_id || "Robot manager unavailable";
  $("details").textContent = `Manager generation: ${state.generation || "unavailable"}\nRevision: ${state.revision ?? "unavailable"}\nConfiguration: ${state.config_revision || "unavailable"}\n${state.error || ""}`;
  const rows = [];
  for (const unit of state.units || []) {
    const row = node("tr"); row.dataset.unit = unit.unit_id;
    const label = node("td", unit.label); label.append(node("small", unit.unit_id)); row.append(label);
    const status = node("td");
    const style = unit.lifecycle === "running" ? "running" : unit.lifecycle === "failed" ? "failed" : unit.blocked ? "warning" : "";
    const lifecycle = unit.available === false && !unit.run_id ? "Unavailable here" : unit.lifecycle.replaceAll("_", " ");
    status.append(node("span", lifecycle, `badge ${style}`));
    if (unit.available === false) status.append(node("small", unit.unavailable_reason));
    if (unit.start_blocked_by?.length) {
      const labels = unit.start_blocked_by.map(id => state.units.find(other => other.unit_id === id)?.label || id);
      status.append(node("small", `Start blocked by ${labels.join(", ")}.`));
    }
    if (unit.launch_mode) status.append(node("small", unit.launch_mode.label));
    if (unit.stop_phase === "timed_out") status.append(node("small", unit.blocked ? "Stop timed out; processes may remain" : "Previous stop timed out"));
    else if (unit.stop_phase === "in_progress") status.append(node("small", "Graceful stop requested"));
    if (pending[unit.unit_id]) status.append(node("small", "Checking command outcome…"));
    row.append(status);
    const run = node("td"); run.append(node("code", unit.run_id ? unit.run_id.slice(0, 10) : "—")); row.append(run);
    const actions = node("td"), buttons = node("div", undefined, "buttons");
    for (const [action, mode] of [["start", null], ...(unit.launch_modes || []).map(mode => ["start", mode]), ["stop", null]]) {
      const button = node("button", mode?.label || (action === "start" ? "Start" : "Stop"), action === "stop" ? "stop" : "secondary");
      button.dataset.command = action;
      if (mode) button.dataset.mode = mode.id;
      button.dataset.blocked = String(!!pending[unit.unit_id] || (action === "start" ? unit.available === false || unit.blocked || !!unit.start_blocked_by?.length || !!state.conflicts?.length : !unit.blocked || !!unit.stop_request_id || unit.ownership !== "owned"));
      button.addEventListener("click", () => command(unit, action, mode?.id)); buttons.append(button);
    }
    const logs = node("button", "Logs", "secondary"); logs.disabled = !unit.run_id;
    logs.addEventListener("click", () => openLogs(unit)); buttons.append(logs);
    actions.append(buttons); row.append(actions); rows.push(row);
  }
  if (!rows.length) { const row = node("tr"), cell = node("td", "No workload observations available."); cell.colSpan = 4; row.append(cell); rows.push(row); }
  $("units").replaceChildren(...rows);
  updateAge();
}
async function reconcile(unitId, id) {
  if (reconciling.has(unitId)) return;
  reconciling.add(unitId);
  try {
    let operation;
    for (let attempt = 0; attempt < 40; attempt++) {
      operation = await api(`/api/operations/${encodeURIComponent(id)}`);
      if (!["prepared", "in_progress"].includes(operation.phase)) break;
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    notice(`${unitId}: ${operation.phase.replaceAll("_", " ")}${operation.outcome ? ` (${operation.outcome.replaceAll("_", " ")})` : ""}. Request ${id}.`);
    delete pending[unitId]; savePending(); render();
  } catch (error) {
    if (error.code === "not_found") { delete pending[unitId]; savePending(); render(); }
    notice(`${error.message}. Request ${id}. Check the workload before issuing another command.`);
  } finally { reconciling.delete(unitId); }
}
async function command(unit, action, mode) {
  if (!authenticated || !fresh() || pending[unit.unit_id]) return;
  const id = crypto.randomUUID();
  pending[unit.unit_id] = id; savePending(); render();
  const value = { request_id: id, config_revision: state.config_revision, expires_at_ms: Math.floor(state.server_time_ms + (performance.now() - received)) + 10000 };
  if (action === "stop") value.expected_run = unit.run_id;
  if (mode !== undefined) value.mode = mode;
  notice(`${action === "start" ? "Start" : "Stop"} requested for ${unit.label}. Request ${id}.`);
  try {
    await api(`/api/units/${encodeURIComponent(unit.unit_id)}/${action}`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(value) });
    await reconcile(unit.unit_id, id);
  } catch (error) {
    notice(`${error.message}. The command will not be replayed. Request ${id}.`);
    await reconcile(unit.unit_id, id);
  }
}
$("unlock").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  const passphrase = $("passphrase").value; $("passphrase").value = "";
  try { const session = await api("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passphrase }) }); authenticated = true; csrf = session.csrf; notice("Commands unlocked for this browser."); renderAccess(); }
  catch (error) { notice(error.message); }
  finally { button.disabled = false; }
});
$("lock").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: "{}" }); authenticated = false; csrf = null; renderAccess(); notice("Commands locked."); }
  catch (error) { notice(error.message); }
});
async function openLogs(unit) {
  logRun = unit.run_id; logCursor = null; $("logs").textContent = ""; $("log-panel").hidden = false;
  $("log-title").textContent = `${unit.label} · Logs`; $("follow-logs").checked = false;
  await loadLogs();
}
async function loadLogs() {
  if (!logRun || loadingLogs) return;
  loadingLogs = true; const run = logRun;
  try {
    const page = await api(`/api/runs/${encodeURIComponent(run)}/logs?limit=100${logCursor ? `&cursor=${encodeURIComponent(logCursor)}` : ""}`);
    if (run !== logRun) return;
    const added = page.records.map(record => `${new Date(Number(record.realtime_us) / 1000).toLocaleTimeString()} ${record.message}${record.message_truncated ? " … [truncated]" : ""}`).join("\n");
    $("logs").textContent = ($("logs").textContent + (added ? added + "\n" : "")).slice(-65536);
    $("log-note").textContent = page.gap || (page.truncated ? "This page is bounded; more output may be available." : "Recent retained output. Older journal history may have expired.");
    if (page.truncated && page.next_cursor === logCursor) $("follow-logs").checked = false;
    logCursor = page.next_cursor;
  } catch (error) { if (run === logRun) { $("log-note").textContent = error.message; $("follow-logs").checked = false; } }
  finally { loadingLogs = false; }
}
$("refresh-logs").addEventListener("click", loadLogs);
$("close-logs").addEventListener("click", () => { logRun = null; $("follow-logs").checked = false; $("log-panel").hidden = true; });
setInterval(() => { if ($("follow-logs").checked && !document.hidden) loadLogs(); }, 2000);
setInterval(updateAge, 1000);
api("/api/session").then(session => { authenticated = session.authenticated; csrf = session.csrf; renderAccess(); }).catch(error => notice(error.message));
clientsReady.then(clients => {
  clients.runtime.acquire();
  clients.runtime.subscribe(value => {
    streamOpen = value.connection === "open";
    state = value.snapshot; received = performance.now(); render();
    window.runtimeRos?.updateRuntime(state);
    for (const [unit, id] of Object.entries(pending)) if (fresh()) reconcile(unit, id);
  });
  clients.runtime.connect();
  window.addEventListener("pagehide", () => clients.runtime.disconnect());
  window.addEventListener("pageshow", () => clients.runtime.connect());
}).catch(error => notice(error.message));
