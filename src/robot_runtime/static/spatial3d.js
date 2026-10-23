/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
"use strict";
(() => {
  const dot = (a, b) => a.reduce((sum, value, i) => sum + value * b[i], 0);
  const rotate = ([x,y,z,w], [a,b,c]) => [
    (1-2*(y*y+z*z))*a + 2*(x*y-z*w)*b + 2*(x*z+y*w)*c,
    2*(x*y+z*w)*a + (1-2*(x*x+z*z))*b + 2*(y*z-x*w)*c,
    2*(x*z-y*w)*a + 2*(y*z+x*w)*b + (1-2*(x*x+y*y))*c];
  function create(svg, changed, active) {
    let azimuth, elevation, extent, pan, drag = null, panMode = false;
    const reset = () => { azimuth = -3*Math.PI/4; elevation = .65; extent = 6; pan = [0,0]; };
    reset();
    function project(x, y, z = 0) {
      const a = azimuth, e = elevation, point = [x,y,z], scale = 250/extent;
      const right = [-Math.sin(a), Math.cos(a), 0];
      const up = [-Math.cos(a)*Math.sin(e), -Math.sin(a)*Math.sin(e), Math.cos(e)];
      const eye = [Math.cos(a)*Math.cos(e), Math.sin(a)*Math.cos(e), Math.sin(e)];
      return [300+pan[0]+scale*dot(point,right), 310+pan[1]-scale*dot(point,up), dot(point,eye)];
    }
    const zoom = factor => { extent = Math.max(.25, Math.min(25, extent*factor)); };
    function move(dx, dy, panning) {
      if (panning) { pan[0] += dx; pan[1] += dy; }
      else { azimuth -= dx*.008; elevation = Math.max(-Math.PI/2, Math.min(Math.PI/2, elevation+dy*.008)); }
      changed();
    }
    svg.addEventListener("pointerdown", event => {
      if (!active() || event.button !== 0) return;
      drag = {id:event.pointerId, x:event.clientX, y:event.clientY};
      svg.setPointerCapture(event.pointerId); svg.focus(); event.preventDefault();
    });
    svg.addEventListener("pointermove", event => {
      if (!drag || drag.id !== event.pointerId) return;
      const scale = 600/svg.getBoundingClientRect().width;
      move((event.clientX-drag.x)*scale, (event.clientY-drag.y)*scale, panMode || event.shiftKey);
      drag.x = event.clientX; drag.y = event.clientY;
    });
    for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) svg.addEventListener(name, () => { drag = null; });
    svg.addEventListener("wheel", event => {
      if (!active()) return;
      event.preventDefault(); zoom(Math.exp(Math.max(-100, Math.min(100, event.deltaY))*.003)); changed();
    }, {passive:false});
    svg.addEventListener("keydown", event => {
      if (!active()) return;
      const delta = {ArrowLeft:[-12,0], ArrowRight:[12,0], ArrowUp:[0,-12], ArrowDown:[0,12]}[event.key];
      if (delta) move(...delta, event.shiftKey || panMode);
      else if (event.key === "+" || event.key === "=") { zoom(1/1.5); changed(); }
      else if (event.key === "-") { zoom(1.5); changed(); }
      else if (event.key.toLowerCase() === "r") { reset(); changed(); }
      else return;
      event.preventDefault();
    });
    return {project, zoom, reset,
      preset(name) { pan = [0,0]; azimuth = name === "top" ? Math.PI : 0; elevation = name === "top" ? Math.PI/2 : 0; },
      panMode(value) { panMode = value; },
      extent: () => extent,
      draw(grid, origin, make, frame) {
        const edge = Math.ceil(extent), step = extent > 12 ? 5 : extent > 6 ? 2 : extent < 2 ? .5 : 1;
        const line = (parent, a, b, color, width = 1) => {
          const start = project(...a), end = project(...b);
          parent.append(make("line", {x1:start[0], y1:start[1], x2:end[0], y2:end[1], stroke:color, "stroke-width":width}));
        };
        for (let value = Math.ceil(-edge/step)*step; value <= edge; value += step) {
          line(grid, [value,-edge,0], [value,edge,0], value === 0 ? "#829aa4" : "#d5e1e4");
          line(grid, [-edge,value,0], [edge,value,0], value === 0 ? "#829aa4" : "#d5e1e4");
        }
        for (const [axis, color, end] of [["+x forward", "#be4343", [.7,0,0]], ["+y left", "#218152", [0,.7,0]], ["+z up", "#3e65bd", [0,0,.7]]]) {
          line(origin, [0,0,0], end, color, 2.5);
          const p = project(...end), center = project(0,0,0), vertical = axis === "+z up";
          const side = p[0] < center[0] ? -1 : 1;
          origin.append(make("text", {x:p[0]+(vertical ? 0 : side*6), y:p[1]-(vertical ? 10 : 5),
            "text-anchor":vertical ? "middle" : side < 0 ? "end" : "start", class:"plot-axis", style:`fill:${color}`}, axis));
        }
        const p = project(0,0,0);
        origin.append(make("circle", {cx:p[0], cy:p[1], r:4, fill:"#006d68"}));
        origin.append(make("text", {x:p[0]+8, y:p[1]+18, class:"plot-label"}, frame === "base_link" ? "Car / origin" : "Frame origin"));
        return `3D · orthographic · ground grid ${step} m · ${ (2*extent).toFixed(2)} m across plot · equal scale on all axes`;
      }
    };
  }
  function markerCorners(point) {
    const half = point.marker_size_m/2;
    return [[-half,half,0], [half,half,0], [half,-half,0], [-half,-half,0]].map(corner => {
      const rotated = rotate(point.orientation_xyzw, corner);
      return rotated.map((value, i) => value + point[["x","y","z"][i]]);
    });
  }
  window.runtimeSpatial3D = {create, markerCorners};
})();
