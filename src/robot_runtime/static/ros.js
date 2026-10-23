/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
(() => {
  let catalog = null, catalogKey = null, topicRevision = null, fetching = false, refreshAgain = false, wanted = false;
  let connected = false, latest = null, receivedAt = 0;
  const selected = new Set(), topicRows = new Map(), serviceRows = new Map(), reconciling = new Set();
  const spatialViews = new Map();
  let pending = Object.create(null);
  try {
    const saved = JSON.parse(sessionStorage.getItem("runtime-ros-pending") || "{}");
    for (const [key, id] of Object.entries(saved).slice(-8)) if (typeof id === "string") pending[key] = id;
  } catch { /* No saved commands. */ }
  const save = () => sessionStorage.setItem("runtime-ros-pending", JSON.stringify(pending));

  function stopStream() {
    pageClients?.observations.disconnect(); connected = false;
    updateTopics();
  }
  function openStream() {
    stopStream(); latest = null;
    if (!wanted || document.hidden || !selected.size || !catalog?.enabled) return;
    pageClients.observations.connect({topics: [...selected]});
  }
  function updateTopics(imuOnly = false) {
    for (const [key, row] of topicRows) {
      if (imuOnly && row.spec.projection?.kind !== "imu") continue;
      const value = latest?.topics?.find(topic => topic.id === key);
      const elapsed = (performance.now() - receivedAt) / 1000;
      const age = value?.age_seconds === null || value?.age_seconds === undefined ? null : value.age_seconds + elapsed;
      const acquisitionAge = value?.acquisition_age_seconds == null ? null : value.acquisition_age_seconds + elapsed;
      const sampleAge = value?.sample_age_seconds == null ? null : value.sample_age_seconds + elapsed;
      const unit = state?.units?.find(unit => unit.unit_id === row.spec.unit_id);
      const sameRun = fresh() && unit?.lifecycle === "running" && unit?.ownership === "owned" && !unit?.stop_request_id && unit?.run_id === value?.run_id &&
        latest?.boot_id === state?.boot_id && latest?.generation === state?.ros?.generation && latest?.config_revision === state?.config_revision;
      const valid = sameRun && connected && wanted && selected.has(key) && !document.hidden && !!value?.fresh && age !== null && age <= value.stale_after_seconds &&
        (acquisitionAge === null || acquisitionAge <= value.stale_after_seconds) && (sampleAge === null || sampleAge < value.stale_after_seconds);
      let label = !wanted || !selected.has(key) ? "Paused" : document.hidden ? "Paused while hidden" : !connected ? "Waiting for observations" : (value?.status || "awaiting sample").replaceAll("_", " ");
      if (wanted && !document.hidden && selected.has(key) && value?.status === "current") label = valid ? "Fresh sample" : "Stale sample";
      if (wanted && selected.has(key) && !document.hidden && ["stopped", "exited", "failed"].includes(unit?.lifecycle)) label = "Workload stopped";
      row.badge.textContent = label;
      row.badge.className = `badge ${valid ? "live" : "warning"}`;
      row.data.textContent = value?.data === null || value?.data === undefined ? "No sample" : JSON.stringify(value.data, null, 2);
      row.age.textContent = age === null ? "No receipt timestamp" : `Received ${Math.max(0, age).toFixed(1)}s ago · sequence ${value.sequence}`;
      if (acquisitionAge !== null) row.age.textContent += ` · ${value.data?.timestamp_semantics === "estimated_scan_start" ? "estimated scan start" : "acquired"} ${Math.max(0, acquisitionAge).toFixed(2)}s ago (host clock)`;
      if (sampleAge !== null) row.age.textContent += ` · ${value.data?.timestamp_semantics === "estimated_mcu_callback_receipt" ? "estimated MCU receipt" : "acquired"} ${Math.max(0, sampleAge).toFixed(3)}s ago (host clock)`;
      row.render?.(value, valid, elapsed);
    }
    $("ros-observe").textContent = wanted ? "Pause topics" : "Observe topics";
  }
  function updateControls() {
    for (const [key, row] of serviceRows) {
      const unit = state?.units?.find(value => value.unit_id === row.spec.unit_id);
      row.button.disabled = !authenticated || !fresh() || !catalog?.available || !row.spec.available || state?.ros?.generation !== catalog.generation ||
        unit?.lifecycle !== "running" || unit?.ownership !== "owned" || !!unit?.stop_request_id || !!pending[key] || !!row.spec.blocked_by;
      row.note.textContent = pending[key] ? `Checking request ${pending[key]}` : row.spec.blocked_by ?
        `Outcome uncertain. Inspect request ${row.spec.blocked_by} and acknowledge locally before another request.` :
        !authenticated ? "Unlock commands to send a request." : !row.spec.available ? "Waiting for the declared provider." : row.spec.choices ?
        "Publishes one request. Publication does not acknowledge a state change; observe current provider state separately." : "Provider availability is checked again before dispatch.";
    }
  }
  function buildTopics() {
    for (const spec of catalog.topics) {
      if (topicRows.has(spec.id)) continue;
      selected.add(spec.id);
      const card = node("article", undefined, "ros-card"); card.dataset.rosTopic = spec.id;
      const heading = node("div", undefined, "ros-heading"), label = node("label"), toggle = document.createElement("input");
      toggle.type = "checkbox"; toggle.checked = true;
      toggle.addEventListener("change", () => { toggle.checked ? selected.add(spec.id) : selected.delete(spec.id); openStream(); });
      label.append(toggle, document.createTextNode(` ${spec.label}`));
      const badge = node("span", "Paused", "badge"); heading.append(label, badge);
      const data = node("pre", "No sample"), age = node("p", "No receipt timestamp", "ros-note");
      const details = node("details"), summary = node("summary", "Source details");
      details.append(summary, node("pre", `${spec.name}\n${spec.type}\nExpected node: ${spec.node}\nReceipt age measures bridge receipt, not sensor acquisition. Graph names do not authenticate a process.`));
      card.append(heading);
      const spatial = ["detections", "laser_scan", "fiducials"].includes(spec.projection?.kind);
      let render;
      if (spatial) {
        const frame = spec.projection.frame_id;
        if (!spatialViews.has(frame)) {
          const coneView = catalog.topics.find(topic => topic.projection?.kind === "detections" && topic.projection.frame_id === frame)?.projection;
          spatialViews.set(frame, window.runtimeSpatial.create(frame, catalog.topics.some(topic => topic.projection?.kind === "laser_scan" && topic.projection.frame_id === frame), coneView?.forward_m, coneView?.half_width_m));
        }
        render = spatialViews.get(frame).add(spec, card);
      } else render = window.runtimeObservations?.create(spec, card);
      if (render) {
        const raw = node("details"); raw.append(node("summary", "Sample data and timestamps"), data); card.append(age, raw);
      } else card.append(data, age);
      card.append(details); if (!spatial) $("ros-topics").append(card);
      topicRows.set(spec.id, { spec, badge, data, age, render });
    }
  }
  function buildServices() {
    for (const spec of [...catalog.services, ...(catalog.commands || [])]) {
      const old = serviceRows.get(spec.id);
      if (old && JSON.stringify([old.spec.request, old.spec.choices]) === JSON.stringify([spec.request, spec.choices])) { old.spec = spec; continue; }
      old?.form.remove();
      const form = node("form", undefined, "ros-card ros-service"); form.dataset.rosService = spec.id;
      form.append(node("h3", spec.label));
      const inputs = new Map();
      let choice = null;
      if (spec.choices) {
        form.dataset.rosCommand = spec.id;
        choice = document.createElement("select"); choice.name = "choice"; choice.required = true;
        const placeholder = node("option", "Choose a request…"); placeholder.value = ""; choice.append(placeholder);
        for (const value of spec.choices) { const option = node("option", value.label); option.value = value.id; choice.append(option); }
        const label = node("label", "State request", "ros-field"); label.append(choice); form.append(label);
      }
      for (const [name, kind] of Object.entries(spec.request || {})) {
        const label = node("label", `${name.replaceAll("_", " ")} (${kind})`, "ros-field");
        const input = document.createElement("input"); input.name = name;
        if (["boolean", "bool"].includes(kind)) { input.type = "checkbox"; input.checked = true; }
        else if (/^(u?int64|string)/.test(kind)) { input.type = "text"; input.value = kind.includes("64") ? "0" : ""; input.maxLength = 4096; }
        else { input.type = "number"; input.step = "any"; input.value = "0"; input.required = true; }
        label.append(input); form.append(label); inputs.set(name, { input, kind });
      }
      const button = node("button", spec.choices ? "Publish request" : "Send request"); button.type = "submit";
      const note = node("p", undefined, "ros-note"), result = node("pre", "No request sent", "ros-result");
      form.append(button, note, result);
      const row = { spec, form, inputs, choice, button, note, result }; serviceRows.set(spec.id, row);
      form.addEventListener("submit", event => { event.preventDefault(); send(row); });
      $("ros-services").append(form);
    }
  }
  async function refreshCatalog() {
    if (fetching) { refreshAgain = true; return; }
    fetching = true;
    try {
      catalog = await api("/api/ros");
      const topicsChanged = topicRevision !== catalog.config_revision;
      if (topicsChanged) {
        stopStream(); latest = null;
        topicRows.clear(); selected.clear(); spatialViews.clear(); $("ros-topics").replaceChildren(); $("ros-spatial").replaceChildren();
        topicRevision = catalog.config_revision;
      }
      $("ros-services-heading").hidden = !catalog.services.length && !catalog.commands?.length;
      $("ros-panel").hidden = !catalog.enabled || (!catalog.topics.length && !catalog.services.length && !catalog.commands?.length);
      $("ros-availability").textContent = catalog.available ? "ROS interfaces loaded. Provider availability and topic freshness are checked separately." : catalog.error || "ROS is unavailable.";
      buildTopics(); buildServices(); updateControls(); updateTopics();
      if (topicsChanged) openStream();
      window.runtimeParameters?.updateCatalog(catalog);
      window.runtimeActions?.updateCatalog(catalog);
    } catch (error) { $("ros-availability").textContent = error.message; }
    finally {
      fetching = false;
      if (refreshAgain) { refreshAgain = false; refreshCatalog(); }
    }
  }
  async function reconcile(key, id) {
    if (reconciling.has(key)) return;
    reconciling.add(key);
    try {
      let operation;
      for (let attempt = 0; attempt < 40; attempt++) {
        operation = await api(`/api/operations/${encodeURIComponent(id)}`);
        if (!["prepared", "in_progress"].includes(operation.phase)) break;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      const row = serviceRows.get(key);
      if (row) row.result.textContent = `${operation.phase.replaceAll("_", " ")} · ${(operation.outcome || operation.delivery).replaceAll("_", " ")}\n${JSON.stringify(operation.response || {}, null, 2)}\nRequest ${id}`;
      delete pending[key]; save(); await refreshCatalog(); updateControls();
    } catch (error) {
      if (error.code === "not_found") { delete pending[key]; save(); }
      const row = serviceRows.get(key);
      if (row) row.result.textContent = `${error.message}. Request ${id}. The command will not be replayed.`;
      updateControls();
    } finally { reconciling.delete(key); }
  }
  async function send(row) {
    if (row.button.disabled || !row.form.reportValidity()) return;
    const unit = state.units.find(value => value.unit_id === row.spec.unit_id);
    const values = {};
    for (const [name, { input, kind }] of row.inputs) {
      values[name] = input.type === "checkbox" ? input.checked : /^(u?int64|string)/.test(kind) ? input.value : Number(input.value);
    }
    const id = crypto.randomUUID(); pending[row.spec.id] = id; save(); updateControls();
    const value = { request_id: id, config_revision: catalog.config_revision, expected_run: unit.run_id, generation: catalog.generation,
      expires_at_ms: Math.floor(state.server_time_ms + performance.now() - received) + 10000,
      ...(row.choice ? {choice:row.choice.value} : {arguments:values}) };
    row.result.textContent = `Request ${id} prepared. Waiting for its outcome.`;
    try {
      await api(`/api/ros/${row.choice ? "commands" : "services"}/${encodeURIComponent(row.spec.id)}`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(value) });
    } catch (error) { row.result.textContent = `${error.message}. The command will not be replayed. Request ${id}.`; }
    await reconcile(row.spec.id, id);
  }
  $("ros-observe").addEventListener("click", () => { wanted = !wanted; openStream(); updateTopics(); });
  document.addEventListener("visibilitychange", () => { document.hidden ? stopStream() : openStream(); });
  window.addEventListener("pagehide", stopStream);
  window.addEventListener("pageshow", openStream);
  setInterval(() => { updateTopics(); updateControls(); }, 500);
  // Bound stale-pose display for short-lived IMU samples even if delivery stops.
  setInterval(() => { if (wanted && !document.hidden) updateTopics(true); }, 50);
  window.runtimeRos = { getCatalog: () => catalog, updateRuntime(value) {
    if (!value) return;
    window.runtimeParameters?.updateRuntime(value);
    window.runtimeSnapshots?.update();
    window.runtimeActions?.updateRuntime();
    $("ros-panel").hidden = !value.ros?.enabled || !catalog || (!catalog.topics.length && !catalog.services.length && !catalog.commands?.length);
    if (!value.ros?.enabled) { stopStream(); return; }
    const key = JSON.stringify([value.config_revision, value.ros.generation, value.ros.available, value.ros.error, value.ros.capability_revision,
      value.units.map(unit => [unit.unit_id, unit.run_id, unit.lifecycle, unit.ownership, unit.stop_request_id])]);
    if (key !== catalogKey) { catalogKey = key; refreshCatalog(); }
    updateControls();
    if (fresh() && authenticated) for (const [id, requestId] of Object.entries(pending)) reconcile(id, requestId);
  } };
  window.runtimeRos.updateRuntime(state);
  clientsReady.then(clients => {
    clients.observations.acquire();
    clients.observations.subscribe(value => {
      latest = value.snapshot; receivedAt = performance.now(); connected = value.connection === "open";
      updateTopics();
    });
  });
})();
