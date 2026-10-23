/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
(() => {
  const svgNode = (name, attributes = {}, text) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    if (text !== undefined) element.textContent = text;
    return element;
  };
  let sequence = 0;
  function create(frame, surround, forward = 6, halfWidth = 3) {
    const card = node("article", undefined, "ros-card spatial-card"), controls = node("div", undefined, "buttons");
    card.dataset.spatialFrame = frame;
    const heading = node("div", undefined, "ros-heading"), title = node("h3", `Top-down view · ${frame}`); heading.append(title, controls);
    const content = node("div", undefined, "spatial-content"), layers = node("div", undefined, "spatial-layers");
    const svg = svgNode("svg", {viewBox: "0 0 600 600", role: "img", "aria-label": `Spatial observations in ${frame}, x forward and y left, metres`, class: "spatial-plot cone-plot"});
    const grid = svgNode("g"), marks = svgNode("g"), car = svgNode("g", {class: "plot-car"});
    const clipId = `spatial-clip-${++sequence}`, defs = svgNode("defs"), clip = svgNode("clipPath", {id:clipId});
    clip.append(svgNode("rect", {x:35, y:30, width:530, height:530})); defs.append(clip);
    svg.append(defs, grid, marks, car); content.append(svg, layers);
    const note = node("p", undefined, "ros-note spatial-extent");
    const hint = node("p", "Drag to orbit · Shift-drag or Pan to move · scroll to zoom. Keyboard: arrows orbit, Shift+arrows pan, +/− zoom, R resets.", "ros-note");
    card.append(heading, note, hint, content, node("p", "Metres · +x forward · +y left · +z up. Car-relative frame, independent of IMU orientation. Each layer replaces its own latest sample. No tracking, scan deskew or time synchronization between sensors.", "ros-note"));
    $("ros-spatial").append(card);
    let mode = surround ? "surround" : "forward", extent = surround ? 6 : forward, dimension = "2d", panMode = false;
    const renderers = [], viewButtons = new Map(), dimensionButtons = new Map(), only3d = [];
    const camera = runtimeSpatial3D.create(svg, redraw, () => dimension === "3d");
    const bounds = () => mode === "surround" ? [-extent, extent, extent] : [0, extent, extent * halfWidth / forward];
    const screen = (x, y, z = 0) => {
      if (dimension === "3d") return camera.project(x,y,z);
      const [low, high, width] = bounds(), scale = 500 / Math.max(high - low, 2 * width);
      return [300 - y * scale, 550 - (x - low) * scale];
    };
    const inside = (x, y, z = 0) => {
      if (dimension === "3d") { const [cx,cy] = screen(x,y,z); return cx >= 35 && cx <= 565 && cy >= 30 && cy <= 560; }
      const [low, high, width] = bounds(); return x >= low && x <= high && Math.abs(y) <= width;
    };
    function renderLayers() {
      marks.replaceChildren();
      const objects = renderers.flatMap(render => render());
      if (dimension === "3d") objects.sort((a,b) => a.depth-b.depth);
      for (const object of objects) marks.append(object.element);
    }
    function redraw() {
      grid.replaceChildren(); car.replaceChildren();
      title.textContent = `${dimension === "3d" ? "3D" : "Top-down"} view · ${frame}`;
      card.dataset.dimension = dimension;
      svg.classList.toggle("spatial-interactive", dimension === "3d");
      svg.setAttribute("tabindex", dimension === "3d" ? "0" : "-1");
      svg.setAttribute("aria-label", `${dimension === "3d" ? "Interactive 3D" : "Top-down"} observations in ${frame}, x forward, y left, z up, metres`);
      hint.hidden = dimension !== "3d";
      for (const button of viewButtons.values()) button.hidden = dimension !== "2d";
      for (const button of only3d) button.hidden = dimension !== "3d";
      for (const [name, button] of dimensionButtons) button.setAttribute("aria-pressed", String(dimension === name));
      for (const group of [grid, marks, car]) {
        if (dimension === "3d") group.setAttribute("clip-path", `url(#${clipId})`); else group.removeAttribute("clip-path");
      }
      grid.append(svgNode("rect", {x: 40, y: 35, width: 520, height: 520, rx: 6, fill: "#f4f7f8"}));
      if (dimension === "3d") { note.textContent = camera.draw(grid, car, svgNode, frame); renderLayers(); return; }
      const [low, high, width] = bounds(), step = extent > 12 ? 5 : extent > 6 ? 2 : extent <= 2 ? .5 : 1;
      for (let x = Math.ceil(low / step) * step; x <= high; x += step) {
        const a = screen(x, width), b = screen(x, -width);
        grid.append(svgNode("line", {x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: x === 0 ? "#829aa4" : "#d5e1e4"}));
        grid.append(svgNode("text", {x: b[0] + 6, y: b[1] + 4, class: "plot-label"}, `${x} m`));
      }
      for (let y = Math.ceil(-width / step) * step; y <= width; y += step) {
        const a = screen(low, y), b = screen(high, y);
        grid.append(svgNode("line", {x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: y === 0 ? "#829aa4" : "#d5e1e4"}));
        if (y) grid.append(svgNode("text", {x: a[0], y: 575, "text-anchor": "middle", class: "plot-label"}, `${y > 0 ? "+" : ""}${y}`));
      }
      grid.append(svgNode("text", {x: 300, y: 23, "text-anchor": "middle", class: "plot-axis"}, "+x · forward ↑"));
      grid.append(svgNode("text", {x: 55, y: 598, class: "plot-axis"}, "← +y · left"));
      const [cx, cy] = screen(0, 0);
      car.append(svgNode("path", {d: `M ${cx} ${cy-15} L ${cx-10} ${cy+9} L ${cx+10} ${cy+9} Z`, fill: "#006d68"}));
      car.append(svgNode("text", {x: cx + 15, y: cy + 18, class: "plot-label"}, frame === "base_link" ? "Car / origin" : "Frame origin"));
      note.textContent = `${mode === "surround" ? "Surround" : "Forward"} · x ${low.toFixed(1)} to ${high.toFixed(1)} m · y ±${width.toFixed(1)} m · equal scale on both axes`;
      for (const [name, button] of viewButtons) button.setAttribute("aria-pressed", String(mode === name));
      renderLayers();
    }
    for (const [name, label] of [["2d", "2D"], ["3d", "3D"]]) {
      const button = node("button", label, "secondary"); button.type = "button";
      button.addEventListener("click", () => { dimension = name; redraw(); });
      controls.append(button); dimensionButtons.set(name, button);
    }
    for (const [name, label] of [["surround", "Surround"], ["forward", "Forward"]]) {
      const button = node("button", label, "secondary"); button.type = "button";
      button.addEventListener("click", () => { mode = name; extent = name === "surround" ? 6 : forward; redraw(); });
      controls.append(button); viewButtons.set(name, button);
    }
    for (const [label, factor] of [["Zoom in", 1 / 1.5], ["Zoom out", 1.5]]) {
      const button = node("button", label, "secondary"); button.type = "button";
      button.addEventListener("click", () => { if (dimension === "3d") camera.zoom(factor); else extent = Math.max(1, Math.min(25, extent * factor)); redraw(); }); controls.append(button);
    }
    for (const [name, label] of [["top", "Top"], ["front", "Front"], ["reset", "Reset view"], ["pan", "Pan"]]) {
      const button = node("button", label, "secondary"); button.type = "button";
      if (name === "pan") button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => {
        if (name === "pan") { panMode = !panMode; camera.panMode(panMode); button.setAttribute("aria-pressed", String(panMode)); }
        else if (name === "reset") camera.reset(); else camera.preset(name);
        redraw();
      });
      controls.append(button); only3d.push(button);
    }
    redraw();
    return {add(spec, layer) {
      const lidar = spec.projection.kind === "laser_scan", fiducials = spec.projection.kind === "fiducials";
      layer.classList.add(lidar ? "lidar-card" : fiducials ? "fiducial-card" : "cone-card");
      const pointClass = lidar ? "lidar-points" : fiducials ? "fiducial-points" : "cone-points";
      const summary = node("p", "No live sample", lidar ? "lidar-summary" : fiducials ? "fiducial-summary" : "cone-summary");
      layer.append(summary, node("p", lidar ? "Teal points · fixed sensor mount → view frame. Scan-start time is a host estimate, not a hardware timestamp." : fiducials ? "Purple markers · IDs belong to the reported dictionary. Invalid poses are listed in sample data and omitted from the plot. Repeated IDs may represent different observations." : "Yellow · blue · gray = unknown. Plot extent is not the camera field of view.", "ros-note"));
      layers.append(layer);
      let current = null;
      function render() {
        const objects = [];
        if (!current) { summary.textContent = lidar ? "No live lidar returns shown." : fiducials ? "No live marker positions shown." : "No live cone positions shown."; return objects; }
        let outside = 0;
        const data = current, values = lidar ? data.points.map(([x,y,z,index]) => ({x,y,z,index})) : fiducials ? data.detections.filter(item => item.pose_valid).map(item => ({...item, ...item.position})) : data.detections;
        for (const point of values) {
          if (!inside(point.x, point.y, point.z)) { outside++; continue; }
          const [cx, cy, depth = 0] = screen(point.x, point.y, point.z);
          const points = svgNode("g", {class:pointClass, "data-spatial-layer":spec.id});
          const attributes = {"data-x": point.x, "data-y": point.y, "data-z": point.z, "data-screen-x": cx, "data-screen-y": cy};
          const corners = fiducials && dimension === "3d" ? runtimeSpatial3D.markerCorners(point) : null;
          const marker = corners ? svgNode("polygon", {...attributes, "data-marker-id":point.id, "data-size-m":point.marker_size_m,
            "data-corners":JSON.stringify(corners), points:corners.map(corner => screen(...corner).slice(0,2).join(",")).join(" "), fill:"#c8a5e3", "fill-opacity":.8, stroke:"#63328b", "stroke-width":1.5}) :
            lidar ? svgNode("circle", {...attributes, cx, cy, r: 2.2, fill: "#138d91", "data-ray-index": point.index}) : fiducials ?
            svgNode("path", {...attributes, "data-marker-id": point.id, d: `M ${cx} ${cy-7} L ${cx+7} ${cy} L ${cx} ${cy+7} L ${cx-7} ${cy} Z`, fill: "#8855b5", stroke: "#4d266b"}) :
            svgNode("path", {...attributes, d: `M ${cx} ${cy-8} L ${cx-7} ${cy+6} L ${cx+7} ${cy+6} Z`, fill: point.color === 1 ? "#e7b629" : point.color === 2 ? "#287ac0" : "#83949d", stroke: "#263f4d", "stroke-width": 1.2});
          marker.append(svgNode("title", {}, `x ${point.x.toFixed(2)}, y ${point.y.toFixed(2)}, z ${point.z.toFixed(2)} m` + (lidar ? ` · ray ${point.index}` : fiducials ? ` · ${data.dictionary_name} / ${point.id} · ${(point.marker_size_m*1000).toFixed(1)} mm · reprojection ${point.reprojection_error_px.toFixed(2)} px` : ` · confidence ${(point.confidence * 100).toFixed(0)}%`)));
          points.append(marker);
          if (corners) points.append(svgNode("circle", {cx, cy, r:2, fill:"#63328b", class:"marker-center"}));
          if (fiducials) points.append(svgNode("text", {x: cx+10, y: cy-8, class: "marker-label"}, `ID ${point.id}`));
          objects.push({element:points, depth});
        }
        summary.textContent = lidar ? `${data.valid_count} valid returns / ${data.original_count} rays · ${values.length-outside} visible${data.downsampled ? ` · sampled ${data.displayed_count} evenly across valid rays` : ""}${outside ? ` · ${outside} outside plot` : ""}` :
          fiducials ? `${data.dictionary_name} · ${data.original_count === 0 ? "Fresh empty frame · no markers detected" : `${data.original_count} markers · ${values.length-outside} positioned and visible`}${data.invalid_pose_count ? ` · ${data.invalid_pose_count} displayed records have invalid poses` : ""}${outside ? ` · ${outside} outside plot` : ""}${data.truncated ? ` · truncated to first ${data.displayed_count} of ${data.original_count}` : ""}` :
          data.original_count === 0 ? "Fresh empty frame · no cones detected" : `${data.original_count} cones in this frame · ${data.displayed_count-outside} visible${outside ? ` · ${outside} outside plot` : ""}${data.truncated ? ` · truncated to first ${data.displayed_count} of ${data.original_count}` : ""}`;
        return objects;
      }
      // Lidar is below detections in 2D; 3D sorts all objects by depth.
      lidar ? renderers.unshift(render) : renderers.push(render);
      return (value, valid) => {
        current = valid && value?.data?.frame_id === frame ? value.data : null;
        layer.dataset.observationState = !current ? "unavailable" : (lidar ? current.valid_count : current.original_count) === 0 ? "empty" : "fresh";
        renderLayers();
      };
    }};
  }
  window.runtimeSpatial = {create};
})();
