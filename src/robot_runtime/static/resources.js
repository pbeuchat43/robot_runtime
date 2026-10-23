/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
(() => {
  const panel = $("resource-panel"), svgNS = "http://www.w3.org/2000/svg";
  let observing = false, connected = false, value = null, history = [], received = 0, timer = null;
  const numeric = value => typeof value === "number" && Number.isFinite(value);
  const percent = value => numeric(value) ? `${value.toFixed(1)}%` : "Unavailable";
  function bytes(value) {
    if (!numeric(value)) return "Unavailable";
    return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
  }
  function age() { return value?.age_seconds === null || value?.age_seconds === undefined ? null : value.age_seconds + (performance.now() - received) / 1000; }
  function chart(title, column, now) {
    const card = node("div", undefined, "resource-chart"); card.append(node("h3", title));
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 320 130"); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `${title}, 0 to 100 percent over the last 10 minutes. Gaps indicate unavailable measurements or paused collection.`);
    const axis = document.createElementNS(svgNS, "path"); axis.setAttribute("d", "M28 8 V104 H312 M28 56 H312 M28 8 H312"); axis.setAttribute("class", "resource-axis"); svg.append(axis);
    for (const [x,y,text] of [[0,12,"100"],[10,60,"50"],[17,106,"0"],[28,124,"−10 min"],[284,124,"now"]]) {
      const label = document.createElementNS(svgNS,"text"); label.setAttribute("x",x); label.setAttribute("y",y); label.textContent = text; svg.append(label);
    }
    let path = "", previous = null;
    for (const point of history) {
      if (!numeric(point[column])) { previous = null; continue; }
      const x = 28 + 284 * (1 - (now - point[1]) / 600), y = 104 - 96 * Math.max(0, Math.min(100, point[column])) / 100;
      path += `${previous !== null && point[1] - previous <= 2.5 ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
      previous = point[1];
    }
    const line = document.createElementNS(svgNS, "path"); line.setAttribute("d", path); line.setAttribute("class", "resource-line"); svg.append(line); card.append(svg);
    return card;
  }
  function render() {
    const sample = value?.latest, sampleAge = age();
    const fresh = !!(observing && connected && value?.fresh && sampleAge !== null && sampleAge <= value.stale_after_seconds);
    const problems = [value?.error, sample?.error, ...(sample?.host?.errors || []), sample?.gpu?.error].filter(Boolean);
    const status = !observing ? "Monitoring paused" : fresh ? "Monitoring live · shared 1 Hz collector" : "Measurements unavailable or stale";
    $("resource-status").textContent = `${status}${sampleAge === null ? "" : ` · last sample ${Math.floor(sampleAge)}s ago`}${problems.length ? ` · ${problems.join("; ")}` : ""}`;
    const host = fresh ? sample?.host || {} : {}, gpu = fresh ? sample?.gpu || {} : {};
    $("resource-values").replaceChildren(...[
      ["Host CPU", percent(host.cpu_percent)], ["RAM", `${bytes(host.memory_used_bytes)} / ${bytes(host.memory_total_bytes)}`],
      ["Swap", `${bytes(host.swap_used_bytes)} / ${bytes(host.swap_total_bytes)}`], ["GPU activity", percent(gpu.activity_percent)],
      ["GPU temperature", numeric(gpu.temperature_c) ? `${gpu.temperature_c.toFixed(1)} °C` : "Unavailable"]
    ].map(([label, text]) => { const item = node("div"); item.append(node("small", label), node("strong", text)); return item; }));
    const now = sample ? sample.monotonic + (sampleAge || 0) : 0;
    history = history.filter(point => now - point[1] < 600).slice(-600);
    $("resource-charts").replaceChildren(...[["Host CPU (%)",3],["RAM used (%)",4],["GPU activity (%)",5]].map(([label,column]) => chart(label,column,now)));
    $("resource-units").replaceChildren(...(sample?.units || []).map(unit => {
      const row = node("tr"); row.dataset.resourceUnit = unit.unit_id;
      const label = node("td", unit.label); label.append(node("small", unit.lifecycle)); row.append(label);
      row.append(node("td", fresh ? percent(unit.cpu_percent) : "Unavailable"), node("td", fresh ? bytes(unit.rss_bytes) : "Unavailable"), node("td", fresh ? bytes(unit.memory_current_bytes) : "Unavailable"));
      if (unit.errors?.length) { row.title = unit.errors.join("; "); label.append(node("small", row.title)); }
      return row;
    }));
  }
  function sync() {
    const wanted = panel.open && !document.hidden;
    if (!pageClients) return;
    if (wanted && !observing) {
      observing = true;
      connected = false;
      pageClients.resources.connect();
      timer = setInterval(() => { if (performance.now() - received >= 1100) render(); }, 1000);
    } else if (!wanted && observing) {
      pageClients.resources.disconnect(); observing = false; connected = false; clearInterval(timer); timer = null;
    }
    render();
  }
  panel.addEventListener("toggle", sync);
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", () => { pageClients?.resources.disconnect(); observing = false; connected = false; clearInterval(timer); });
  window.addEventListener("pageshow", sync);
  clientsReady.then(clients => {
    clients.resources.acquire();
    clients.resources.subscribe(state => {
      const next = state.snapshot;
      connected = state.connection === "open";
      if (next) {
        if (next.generation !== value?.generation) history = [];
        if (next.history) history = [...next.history];
        const sample = next.latest;
        if (sample && (!history.length || sample.sequence > history[history.length - 1][0])) {
          const host = sample.host || {}, memory = host.memory_total_bytes && numeric(host.memory_used_bytes) ? 100 * host.memory_used_bytes / host.memory_total_bytes : null;
          history.push([sample.sequence, sample.monotonic, sample.time_ms, host.cpu_percent, memory, sample.gpu?.activity_percent]);
        }
        value = next; received = performance.now();
      }
      render();
    });
    sync();
  });
})();
