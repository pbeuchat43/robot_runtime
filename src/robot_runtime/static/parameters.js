/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
(() => {
  let catalog = null;
  const rows = new Map(), reconciling = new Set();
  let pending = Object.create(null);
  try {
    for (const [key, id] of Object.entries(JSON.parse(sessionStorage.getItem("runtime-parameter-pending") || "{}")).slice(-8)) {
      if (typeof id === "string") pending[key] = id;
    }
  } catch { /* No saved requests. */ }
  const save = () => sessionStorage.setItem("runtime-parameter-pending", JSON.stringify(pending));
  const url = key => `/api/ros/parameters/${encodeURIComponent(key)}`;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const display = value => typeof value === "string" ? value : JSON.stringify(value);

  function valid(row) {
    const value = row.snapshot;
    const unit = state?.units?.find(item => item.unit_id === row.spec.unit_id);
    return !!value?.fresh && fresh() && catalog?.available && row.spec.available &&
      value.age_seconds + (performance.now() - row.received) / 1000 <= value.stale_after_seconds &&
      value.config_revision === state.config_revision && value.generation === state.ros?.generation &&
      value.run_id === unit?.run_id && unit.lifecycle === "running" && unit.ownership === "owned" && !unit.stop_request_id &&
      value.snapshot_id === row.spec.snapshot_id;
  }
  function updateControls() {
    for (const [key, row] of rows) {
      const current = valid(row), editing = authenticated && current && row.spec.writable && !row.spec.blocked_by && !pending[key] && !row.reading;
      const unit = state?.units?.find(item => item.unit_id === row.spec.unit_id);
      row.refresh.disabled = row.reading || !!pending[key] || !fresh() || !row.spec.available || unit?.lifecycle !== "running";
      row.apply.disabled = !editing || ![...row.inputs.values()].some(item => item.select.checked);
      const readOnlyGroup = !row.spec.parameters.some(parameter => parameter.write);
      row.apply.hidden = readOnlyGroup;
      for (const item of row.inputs.values()) {
        item.select.disabled = !editing || !item.parameter.writable;
        item.input.disabled = item.select.disabled || !item.select.checked;
      }
      const value = row.snapshot;
      row.age.textContent = value?.age_seconds === null || !value ? (row.inputs.size ? "Refresh to read current values." : "No values read yet.") :
        `Read ${(value.age_seconds + (performance.now() - row.received) / 1000).toFixed(1)}s ago · ${current ? "fresh" : "stale; refresh before editing"}`;
      row.note.textContent = pending[key] ? `Checking request ${pending[key]}.` : readOnlyGroup ? "These settings are read-only. This view cannot change them." : row.spec.blocked_by ?
        `Outcome uncertain. Inspect request ${row.spec.blocked_by} and acknowledge locally before another edit.` :
        !authenticated ? "Reading is public. Unlock commands to edit." : !current ?
        "Refresh values to establish a current editing baseline." : "Select the values to change, then apply them together. Refresh discards this draft.";
    }
  }
  function showValues(row, value) {
    row.snapshot = value; row.received = performance.now(); row.inputs.clear(); row.fields.replaceChildren();
    for (const [name, parameter] of Object.entries(value.parameters)) {
      const field = node("div", undefined, "parameter-field"); field.dataset.parameter = name;
      const title = node("label", undefined, "parameter-name"), select = document.createElement("input");
      select.type = "checkbox"; select.setAttribute("aria-label", `Edit ${name}`);
      select.hidden = !parameter.writable;
      title.append(select, document.createTextNode(` ${name} (${parameter.type})`));
      const descriptor = parameter.descriptor;
      field.append(title, node("p", descriptor.description || "No description provided.", "ros-note"),
        node("pre", `Observed: ${display(parameter.value)}`, "parameter-observed"));
      const metadata = [parameter.writable ? "Live edit permitted" : "Read-only", descriptor.dynamic_typing ? "Dynamic type; this editor preserves the current type" : ""];
      for (const range of [...descriptor.integer_range, ...descriptor.floating_point_range]) {
        metadata.push(`Range ${range.from_value} to ${range.to_value}; step ${range.step}`);
      }
      if (descriptor.additional_constraints) metadata.push(descriptor.additional_constraints);
      field.append(node("p", metadata.filter(Boolean).join(" · "), "ros-note"));
      const label = node("label", "Draft value", "parameter-value"), input = document.createElement(parameter.type.endsWith("_array") ? "textarea" : "input");
      label.hidden = !parameter.writable;
      input.setAttribute("aria-label", `Draft ${name}`);
      if (parameter.type === "bool") { input.type = "checkbox"; input.checked = parameter.value; }
      else if (parameter.type === "double") { input.type = "number"; input.step = "any"; input.value = parameter.value; input.required = true; }
      else { input.value = display(parameter.value); input.maxLength = 16384; }
      if (parameter.type.endsWith("_array")) label.append(node("small", parameter.type === "integer_array" ? 'JSON array of decimal strings, e.g. ["1", "2"]' : "JSON array"));
      label.append(input); field.append(label); row.fields.append(field);
      row.inputs.set(name, { parameter, select, input });
      select.addEventListener("change", updateControls);
    }
    updateControls();
  }
  async function read(row) {
    if (row.refresh.disabled) return;
    row.reading = true; updateControls();
    row.result.textContent = "Reading values and descriptions…";
    try {
      let value = await api(url(row.spec.id) + "?refresh=1");
      for (let attempt = 0; value.reading && attempt < 48; attempt++) {
        await sleep(250); value = await api(url(row.spec.id));
      }
      if (value.reading || value.read_status !== "complete") throw new Error(value.error || "Parameter inspection did not complete");
      // This explicit read is the only action that replaces a draft.
      row.spec.snapshot_id = value.snapshot_id;
      showValues(row, value); row.result.textContent = "Values read. No edits applied.";
    } catch (error) { row.snapshot = null; row.result.textContent = error.message; }
    finally { row.reading = false; updateControls(); }
  }
  function showOperation(row, operation) {
    const readback = operation.readback;
    let value = `Provider result: ${(operation.outcome || operation.phase).replaceAll("_", " ")}`;
    if (operation.response) value += `\n${operation.response.reason || "No reason provided."}`;
    if (readback?.status === "observed") value += `\nReadback: ${readback.matches_requested ? "matches requested values" : "DIFFERS from requested values"}\n${JSON.stringify(readback.parameters, null, 2)}`;
    else if (readback) value += `\nReadback: ${readback.status}${readback.error ? " · " + readback.error : ""}`;
    row.result.textContent = `${value}\nRequest ${operation.request_id}`;
  }
  async function reconcile(key, id) {
    if (reconciling.has(key)) return;
    reconciling.add(key);
    try {
      let operation;
      for (let attempt = 0; attempt < 48; attempt++) {
        operation = await api(`/api/operations/${encodeURIComponent(id)}`);
        if (rows.has(key)) showOperation(rows.get(key), operation);
        if (!["prepared", "in_progress"].includes(operation.phase) && operation.readback?.status !== "pending") break;
        await sleep(250);
      }
      if (!["prepared", "in_progress"].includes(operation.phase) && operation.readback?.status !== "pending") { delete pending[key]; save(); }
    } catch (error) {
      if (error.code === "not_found") { delete pending[key]; save(); }
      if (rows.has(key)) rows.get(key).result.textContent = `${error.message}. Request ${id}. The edit will not be replayed.`;
    } finally { reconciling.delete(key); updateControls(); }
  }
  async function apply(row) {
    if (row.apply.disabled || !row.form.reportValidity()) return;
    const changes = {};
    try {
      for (const [name, item] of row.inputs) if (item.select.checked) {
        const type = item.parameter.type;
        let value = type === "bool" ? item.input.checked : type === "double" ? Number(item.input.value) :
          type.endsWith("_array") ? JSON.parse(item.input.value) : item.input.value;
        if (type === "double" && !Number.isFinite(value)) throw new Error(`${name} requires a finite number`);
        changes[name] = { type, value };
      }
    } catch (error) { row.result.textContent = `Check draft values: ${error.message}`; return; }
    const value = { request_id: crypto.randomUUID(), config_revision: row.snapshot.config_revision,
      generation: row.snapshot.generation, expected_run: row.snapshot.run_id, expected_snapshot: row.snapshot.snapshot_id,
      expires_at_ms: Math.floor(state.server_time_ms + performance.now() - received) + 10000, changes };
    pending[row.spec.id] = value.request_id; save(); updateControls();
    row.result.textContent = `Request ${value.request_id} prepared. Waiting for provider acceptance and readback.`;
    try {
      await api(url(row.spec.id), { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(value) });
    } catch (error) { row.result.textContent = `${error.message}. Request ${value.request_id}. The edit will not be replayed.`; }
    row.snapshot = null;
    await reconcile(row.spec.id, value.request_id);
  }
  window.runtimeParameters = { updateCatalog(value) {
    catalog = value; $("parameter-panel").hidden = !value.enabled || !value.parameters?.length;
    for (const spec of value.parameters || []) {
      if (rows.has(spec.id)) { rows.get(spec.id).spec = spec; continue; }
      const form = node("form", undefined, "ros-card parameter-form"); form.dataset.parameterGroup = spec.id;
      const heading = node("div", undefined, "ros-heading"), refresh = node("button", "Read / refresh (discards draft)", "secondary"); refresh.type = "button";
      heading.append(node("h3", spec.label), refresh);
      const age = node("p", undefined, "ros-note"), fields = node("div", undefined, "parameter-fields"), applyButton = node("button", "Apply selected values"); applyButton.type = "submit";
      const note = node("p", undefined, "ros-note"), result = node("pre", "No parameter request made.", "ros-result"); result.setAttribute("aria-live", "polite");
      form.append(heading, node("p", `${spec.node} · ${spec.unit_id}`, "ros-note"), age, fields, applyButton, note, result);
      const row = { spec, form, refresh, age, fields, apply: applyButton, note, result, inputs: new Map(), snapshot: null, reading: false };
      rows.set(spec.id, row); $("parameter-groups").append(form);
      refresh.addEventListener("click", () => read(row)); form.addEventListener("submit", event => { event.preventDefault(); apply(row); });
    }
    updateControls();
  }, updateRuntime() {
    updateControls();
    if (fresh() && authenticated) for (const [key, id] of Object.entries(pending)) reconcile(key, id);
  }, updateControls };
  const initial = window.runtimeRos?.getCatalog(); if (initial) window.runtimeParameters.updateCatalog(initial);
  setInterval(updateControls, 500);
})();
