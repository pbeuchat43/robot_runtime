/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
(() => {
  function diagnostic(spec, card) {
    card.classList.add("health-card");
    const health = node("p", "No current health evidence", "health-state"), summary = node("p", undefined, "ros-note"), values = node("dl", undefined, "diagnostic-values");
    card.append(health, summary, values);
    return (value, valid, elapsed) => {
      const data = value?.data, age = value?.progress_age_seconds;
      const progressing = valid && age !== null && age !== undefined && age + elapsed <= value.stale_after_seconds;
      const requiresProgress = spec.projection.progress_key !== null;
      const healthy = valid && data?.level === 0 && (!requiresProgress || progressing);
      card.dataset.observationState = !valid ? "unavailable" : healthy ? "progressing" : data?.level > 0 ? "warning" : "awaiting_progress";
      health.className = `health-state ${healthy ? "healthy" : ""}`;
      health.textContent = !valid ? "No current health evidence" : data?.level > 0 ?
        `${["OK", "Warning", "Error", "Stale"][data.level]} · ${data.message}` : healthy ? (requiresProgress ? "Frames advancing" : "Provider reports OK") : "Waiting for frame progress";
      summary.textContent = valid ? `Provider: ${data.message}` + (requiresProgress ? ` · counter ${data.progress_counter}${progressing ? " · recent increase observed" : " · no recent increase observed"}` : " · measurement freshness is shown separately") : "No current provider health report.";
      values.replaceChildren();
      if (valid) for (const [key, text] of Object.entries(data.values)) {
        values.append(node("dt", key.replaceAll("_", " ").replaceAll(".", " · ")), node("dd", text || "—"));
      }
    };
  }
  window.runtimeObservations = {create(spec, card) {
    return spec.projection?.kind === "diagnostic" ? diagnostic(spec, card) :
      spec.projection?.kind === "imu" ? window.runtimeImu.create(spec, card) : null;
  }};
})();
