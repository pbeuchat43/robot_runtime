/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
(() => {
  const rows = new Map();
  let identity = null;
  function live(row) {
    const unit = state?.units?.find(item => item.unit_id === row.spec.unit_id);
    return fresh() && unit?.lifecycle === "running" && unit?.ownership === "owned" && !unit?.stop_request_id &&
      (!row.spec.required_mode || unit.launch_mode?.id === row.spec.required_mode) ? unit : null;
  }
  function clear(row) {
    row.controller?.abort(); row.controller = null;
    if (row.url) URL.revokeObjectURL(row.url);
    row.url = null; row.value = null;
    row.image.removeAttribute("src"); row.image.hidden = true;
    row.metadata.textContent = `${row.spec.name}\n${row.spec.type}\nExpected frame: ${row.spec.frame_id}`;
  }
  function update() {
    const catalog = window.runtimeRos?.getCatalog();
    const key = JSON.stringify([state?.config_revision, state?.generation, state?.boot_id]);
    if (key !== identity) {
      for (const row of rows.values()) clear(row);
      rows.clear(); $("snapshot-groups").replaceChildren(); identity = key;
    }
    const specs = catalog?.config_revision === state?.config_revision ? catalog?.snapshots || [] : [];
    $("snapshot-panel").hidden = !specs.length;
    for (const spec of specs) {
      if (rows.has(spec.id)) continue;
      const card = node("article", undefined, "ros-card snapshot-card"); card.dataset.snapshot = spec.id;
      const button = node("button", "Take snapshot", "secondary"), cancel = node("button", "Cancel", "secondary");
      cancel.hidden = true;
      const badge = node("span", "No snapshot", "badge"), note = node("p", "", "ros-note"), image = document.createElement("img");
      image.alt = spec.label; image.hidden = true;
      const details = node("details"), metadata = node("pre");
      details.append(node("summary", "Image source and capture details"), metadata);
      const controls = node("div", undefined, "buttons"); controls.append(button, cancel, badge);
      card.append(node("h3", spec.label), controls, note, image, details);
      const row = {spec, card, button, cancel, badge, note, image, metadata, controller: null, value: null, url: null};
      button.addEventListener("click", () => capture(row));
      cancel.addEventListener("click", () => { clear(row); row.note.textContent = "Capture cancelled."; update(); });
      rows.set(spec.id, row); $("snapshot-groups").append(card);
    }
    for (const row of rows.values()) {
      const unit = live(row), value = row.value;
      if (!unit || document.hidden || (value && (value.run_id !== unit.run_id || value.generation !== state.generation))) clear(row);
      row.button.disabled = !unit || document.hidden || !!row.controller;
      row.cancel.hidden = !row.controller;
      if (row.value) {
        const age = row.value.acquisition_age_seconds + (performance.now() - row.received) / 1000;
        const stale = age > row.value.stale_after_seconds;
        row.badge.textContent = stale ? "Stale snapshot" : "Recent snapshot";
        row.badge.className = `badge ${stale ? "warning" : "live"}`;
        row.image.classList.toggle("snapshot-stale", stale);
        row.note.textContent = `Acquired ${age.toFixed(1)}s ago · ${row.value.width} × ${row.value.height} · one captured image`;
      } else {
        row.badge.textContent = row.controller ? "Capturing…" : !unit ? "Unavailable" : "No snapshot";
        row.badge.className = "badge warning";
        if (!unit) row.note.textContent = "Start the workload with images enabled. To change an existing run, stop it first.";
      }
    }
  }
  async function capture(row) {
    const unit = live(row);
    if (!unit || row.controller || document.hidden) return;
    clear(row);
    const controller = row.controller = new AbortController(), started = performance.now();
    const binding = {expected_run: unit.run_id, generation: state.generation, config_revision: state.config_revision};
    row.note.textContent = "Waiting for one fresh image…"; update();
    try {
      const value = await api(`/api/ros/snapshots/${encodeURIComponent(row.spec.id)}`, {method: "POST", signal: controller.signal,
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(binding)});
      if (row.controller !== controller || document.hidden || live(row)?.run_id !== value.run_id || state.generation !== value.generation) return;
      // Conservatively add request transit time, excluding the server's capture wait.
      value.acquisition_age_seconds += Math.max(0, (performance.now() - started) / 1000 - value.request_duration_seconds);
      const bytes = Uint8Array.from(atob(value.image_base64), char => char.charCodeAt(0));
      row.url = URL.createObjectURL(new Blob([bytes], {type: "image/jpeg"}));
      row.image.src = row.url; row.image.hidden = false;
      row.value = value; row.received = performance.now();
      row.metadata.textContent = `${row.spec.name}\n${row.spec.type}\nNode: ${row.spec.node}\nFrame: ${value.frame_id}\nRun: ${value.run_id}\nCapture: ${value.snapshot_id}\nAcquisition timestamp (host system ns): ${value.acquisition_timestamp_ns}\nSource: ${value.source_width} × ${value.source_height} ${value.source_encoding}\nJPEG: ${value.jpeg_bytes} bytes\nGraph names do not authenticate a process.`;
      delete value.image_base64;
    } catch (error) {
      if (row.controller === controller) row.note.textContent = error.name === "AbortError" ? "Capture cancelled." : error.message;
    } finally {
      if (row.controller === controller) row.controller = null;
      update();
    }
  }
  document.addEventListener("visibilitychange", update);
  window.addEventListener("pagehide", () => { for (const row of rows.values()) clear(row); });
  window.runtimeSnapshots = {update};
  setInterval(update, 250);
  update();
})();
