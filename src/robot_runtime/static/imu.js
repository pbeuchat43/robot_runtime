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
  const screen = ([x, y, z], scale = 95) => [210 + scale * .866 * (x - y), 160 + scale * (.5 * (x + y) - z)];
  function rotate(q, v) {
    // Normalize only the rendering calculation after validating the norm.
    // The published/raw quaternion remains unchanged in the sample details.
    const {x, y, z, w, norm} = q, s = 1 / (norm * norm);
    return [
      (1 - 2 * s * (y*y + z*z)) * v[0] + 2*s*(x*y - z*w) * v[1] + 2*s*(x*z + y*w) * v[2],
      2*s*(x*y + z*w) * v[0] + (1 - 2*s*(x*x + z*z)) * v[1] + 2*s*(y*z - x*w) * v[2],
      2*s*(x*z - y*w) * v[0] + 2*s*(y*z + x*w) * v[1] + (1 - 2*s*(x*x + y*y)) * v[2]
    ];
  }
  function create(spec, card) {
    card.classList.add("imu-card");
    const config = spec.projection;
    const svg = svgNode("svg", {viewBox: "0 0 420 300", role: "img", "aria-label": "Sensor orientation axes in the declared reference frame", class: "imu-plot"});
    const title = svgNode("title", {}, "No live orientation"), axes = svgNode("g", {class: "imu-sensor-axes"});
    svg.append(title, svgNode("rect", {x: 5, y: 5, width: 410, height: 285, rx: 8, fill: "#f4f7f8"}));
    const basis = [[1,0,0], [0,1,0], [0,0,1]];
    for (let index = 0; index < 3; index++) {
      const [x, y] = screen(basis[index], 125);
      svg.append(svgNode("line", {x1: 210, y1: 160, x2: x, y2: y, stroke: "#b8c7ce", "stroke-dasharray": "4 4"}));
      svg.append(svgNode("text", {x, y: y - 8, "text-anchor": "middle", class: "plot-label"}, `Reference +${"XYZ"[index]}`));
    }
    svg.append(axes);
    const orientation = node("p", "No live orientation", "imu-summary");
    const values = node("dl", undefined, "imu-values");
    const gyro = node("dd"), acceleration = node("dd");
    values.append(node("dt", "Angular velocity · rad/s"), gyro,
      node("dt", config.acceleration_semantics === "specific_force" ? "Specific force · includes gravity · m/s²" : "Linear acceleration · m/s²"), acceleration);
    card.append(svg, orientation, node("p", `Reference: ${config.orientation_reference}. Quaternion maps sensor axes into this reference.`, "ros-note"),
      node("p", `Sensor frame: ${config.frame_id}. Mounting transforms are not applied to this view.`, "ros-note"), values,
      node("p", "Missing measurements are cleared for each sample. Covariance and provider accuracy are separate; see sample data and health.", "ros-note"));
    const vector = field => field ? ["x", "y", "z"].map(axis => `${axis} ${field.value[axis].toFixed(3)}`).join("   ") : "Unavailable in this sample";
    return (sample, valid) => {
      const data = valid ? sample?.data : null, q = data?.orientation?.value;
      axes.replaceChildren();
      card.dataset.observationState = !valid ? "unavailable" : q ? "fresh" : "partial";
      gyro.textContent = data ? vector(data.angular_velocity) : "No live measurement";
      acceleration.textContent = data ? vector(data.linear_acceleration) : "No live measurement";
      orientation.textContent = !valid ? "No live orientation" : !q ? "Orientation unavailable in this sample" : "Sensor axes · current orientation";
      title.textContent = orientation.textContent;
      if (!q) return;
      const colors = ["#b13d38", "#287454", "#2365a0"];
      for (let index = 0; index < 3; index++) {
        const [x, y] = screen(rotate(q, basis[index]));
        const group = svgNode("g", {"data-axis": "xyz"[index], "data-screen-x": x, "data-screen-y": y});
        group.append(svgNode("line", {x1: 210, y1: 160, x2: x, y2: y, stroke: colors[index], "stroke-width": 4, "stroke-linecap": "round"}),
          svgNode("circle", {cx: x, cy: y, r: 4, fill: colors[index]}),
          svgNode("text", {x: x + 10, y: y + 5, fill: colors[index], class: "imu-axis-label"}, `+${"XYZ"[index]}`));
        axes.append(group);
      }
      axes.append(svgNode("circle", {cx: 210, cy: 160, r: 5, fill: "#526977"}));
    };
  }
  window.runtimeImu = {create};
})();
