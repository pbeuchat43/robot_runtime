/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
(() => {
  const rows = new Map();
  let catalog = null, receivedAt = 0, fetching = false, enabled = false, connected = false;
  let pending = Object.create(null);
  try {
    for (const [key, id] of Object.entries(JSON.parse(sessionStorage.getItem("runtime-action-pending") || "{}")).slice(-4))
      if (typeof id === "string") pending[key] = id;
  } catch { /* No saved commands. */ }
  const save = () => sessionStorage.setItem("runtime-action-pending", JSON.stringify(pending));
  const words = value => String(value || "unknown").replaceAll("_", " ");
  function current() {
    return connected && catalog?.available && !document.hidden && fresh() && performance.now() - receivedAt < 3000 &&
      catalog.generation === state?.ros?.generation && catalog.config_revision === state?.config_revision && catalog.boot_id === state?.boot_id;
  }
  function render() {
    $("action-availability").textContent = current() ? "One goal per action. Any unlocked operator may request cancellation." : "Action observations are unavailable or stale.";
    for (const [key, row] of rows) {
      const spec = row.spec, goal = spec.goal, cancel = spec.cancellation;
      const valid = current(), allowed = valid && authenticated && !pending[key];
      row.send.disabled = !allowed || !spec.can_send;
      row.cancel.disabled = !allowed || !spec.can_cancel;
      row.note.textContent = pending[key] ? `Checking request ${pending[key]}. It will not be replayed.` : !authenticated ?
        "Unlock commands to send or cancel a goal." : !spec.available ? "Waiting for the declared provider." :
        goal && spec.same_run && !goal.terminal ? "This goal reserves the action until its terminal state is known or its provider run is replaced." : "Ready for an explicit goal.";
      row.state.textContent = !goal ? "No goal sent" : `${words(goal.goal_status)} · ${words(goal.phase)}${!spec.same_run ? " · previous run" : ""}${valid ? "" : " · observation stale"}`;
      if (goal) row.state.textContent += `\nGoal ${goal.request_id}${goal.outcome ? `\n${words(goal.outcome)}` : ""}`;
      const age = spec.feedback_age_seconds == null ? null : spec.feedback_age_seconds + (performance.now() - receivedAt) / 1000;
      const freshFeedback = valid && spec.feedback_fresh && age !== null && age <= spec.stale_after_seconds;
      row.feedback.textContent = `${freshFeedback ? "Progress" : "Progress unavailable or stale"}${age === null ? "" : ` · ${age.toFixed(1)}s old`}${spec.feedback_error ? ` · ${words(spec.feedback_error)}` : ""}\n${JSON.stringify(spec.feedback || {}, null, 2)}`;
      row.feedback.dataset.fresh = String(!!freshFeedback);
      row.result.textContent = goal?.terminal ? `Result: ${words(goal.result_status)}\n${JSON.stringify(goal.result || {}, null, 2)}` : "Result pending";
      row.cancellation.textContent = !cancel ? "" : `Cancellation: ${words(cancel.phase)} · ${words(cancel.outcome || cancel.delivery)}. ${cancel.cancel_accepted ? "Acceptance is not proof that the goal has finished canceling." : ""}`;
    }
  }
  function build() {
    const keys = new Set((catalog?.actions || []).map(spec => spec.id));
    for (const [key, row] of rows) if (!keys.has(key)) { row.form.remove(); rows.delete(key); }
    for (const spec of catalog.actions) {
      const old = rows.get(spec.id);
      if (old && JSON.stringify(old.spec.goal_schema) === JSON.stringify(spec.goal_schema)) { old.spec = spec; continue; }
      old?.form.remove();
      const form = node("form", undefined, "ros-card ros-action"), inputs = new Map(); form.dataset.rosAction = spec.id;
      form.append(node("h3", spec.label));
      for (const [name, kind] of Object.entries(spec.goal_schema || {})) {
        const label = node("label", `${words(name)} (${kind})`, "ros-field"), input = document.createElement("input");
        input.name = name;
        if (["boolean", "bool"].includes(kind)) input.type = "checkbox";
        else if (/^(u?int64|string)/.test(kind)) { input.type = "text"; input.value = kind.includes("64") ? "0" : ""; input.maxLength = 4096; }
        else { input.type = "number"; input.step = "any"; input.value = "0"; input.required = true; }
        label.append(input); form.append(label); inputs.set(name, input);
      }
      const send = node("button", "Send goal"), cancel = node("button", "Cancel goal", "secondary"), buttons = node("div", undefined, "buttons");
      send.type = "submit"; cancel.type = "button"; buttons.append(send, cancel);
      const note = node("p", undefined, "ros-note"), status = node("pre", undefined, "action-state"), feedback = node("pre", undefined, "action-feedback");
      const result = node("pre", undefined, "action-result"), cancellation = node("p", undefined, "action-cancellation"), message = node("p", undefined, "action-message");
      status.setAttribute("role", "status");
      const row = {spec, form, inputs, send, cancel, note, state:status, feedback, result, cancellation, message}; rows.set(spec.id, row);
      form.append(buttons, note, status, feedback, result, cancellation, message);
      form.addEventListener("submit", event => { event.preventDefault(); command(row, false); });
      cancel.addEventListener("click", () => command(row, true));
      $("action-groups").append(form);
    }
    render();
  }
  async function refresh() {
    if (!enabled || document.hidden || fetching) return;
    fetching = true;
    try {
      catalog = await api("/api/ros/actions"); receivedAt = performance.now(); connected = true; build();
      if (authenticated) for (const [key, rid] of Object.entries(pending).slice(0, 4)) {
        try {
          const operation = await api(`/api/operations/${encodeURIComponent(rid)}`);
          if (rows.has(key)) rows.get(key).message.textContent = `Request ${rid}: ${words(operation.phase)}.`;
          delete pending[key]; save();
        } catch (error) {
          if (error.code === "not_found") { delete pending[key]; save(); }
        }
      }
    } catch { connected = false; }
    finally { fetching = false; render(); }
  }
  async function command(row, cancel) {
    if ((cancel ? row.cancel : row.send).disabled || (!cancel && !row.form.reportValidity())) return;
    const id = crypto.randomUUID(), key = row.spec.id;
    pending[key] = id; save(); render();
    const value = {request_id:id, config_revision:catalog.config_revision, expected_run:row.spec.run_id, generation:catalog.generation,
      expires_at_ms:Math.floor(state.server_time_ms + performance.now() - received) + 10000};
    if (cancel) value.goal_request_id = row.spec.goal.request_id;
    else value.arguments = Object.fromEntries([...row.inputs].map(([name, input]) => [name, input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value]));
    row.message.textContent = `Request ${id} prepared.`;
    try {
      await api(`/api/ros/actions/${encodeURIComponent(key)}/${cancel ? "cancel" : "goals"}`, {method:"POST",
        headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf}, body:JSON.stringify(value)});
    } catch (error) { row.message.textContent = `${error.message}. Request ${id} will not be replayed.`; }
    await refresh();
  }
  window.runtimeActions = {updateCatalog(value) {
    enabled = !!value.actions?.length;
    $("action-panel").hidden = !enabled;
    if (enabled) refresh();
  }, updateRuntime: render};
  document.addEventListener("visibilitychange", () => { if (document.hidden) { connected = false; render(); } else refresh(); });
  setInterval(() => { refresh(); render(); }, 1000);
})();
