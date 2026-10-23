import {scope, drivingPreview} from '../core.js';
import {gamepadArt} from '../gamepad-art.js';

const names = ['A', 'B', 'X', 'Y', 'LB', 'RB', 'LT', 'RT', 'Back', 'Start', 'L3', 'R3', '↑', '↓', '←', '→', 'Guide'];

export function mountGamepad(root, {input, title = 'Your controller', compact = false} = {}) {
  const ui = scope(root, `<section class="robot-widget robot-gamepad ${compact ? 'robot-compact' : ''}">
    <div class="robot-heading"><h2 data-ref="title"></h2><span data-ref="state" role="status"></span></div>
    <label>Gamepad device<select data-ref="device"><option>No controller detected</option></select></label>
    ${gamepadArt}<p class="robot-small" data-ref="help"></p>
    <div class="robot-metrics"><div><b data-ref="steering">0%</b><span>Steering preview</span></div>
      <div><b data-ref="throttle">0%</b><span>RT · throttle preview</span></div><div><b data-ref="brake">0%</b><span>LT · brake preview</span></div></div>
    <details><summary>Raw input + test pattern</summary><p data-ref="mapping"></p><p class="robot-mono" data-ref="axes"></p>
      <p data-ref="buttons"></p><label class="robot-check"><input type="checkbox" data-ref="simulation"> Use simulated test pattern</label>
      <p class="robot-small">Simulation is labelled at both ends. Input becomes neutral when this page loses focus.</p></details>
  </section>`);
  ui.get('title').textContent = title;
  let signature = '', rendered = -Infinity, frame = null, latest = input.state;
  ui.on(ui.get('device'), 'change', () => input.select(ui.get('device').value));
  ui.on(ui.get('simulation'), 'change', () => input.simulate(ui.get('simulation').checked));
  ui.cleanup(input.acquire());
  function render() {
    frame = null;
    const state = latest;
    const now = performance.now();
    if (now - rendered < 33) return;
    rendered = now;
    const next = JSON.stringify(state.devices);
    if (next !== signature) {
      signature = next;
      ui.get('device').replaceChildren(...(state.devices.length ? state.devices.map(device =>
        new Option(device.id, device.index)) : [new Option('No controller detected', '')]));
    }
    ui.get('device').value = String(state.selected ?? '');
    ui.get('simulation').checked = state.simulation;
    ui.text('state', !state.focused ? 'Paused · focus this page' : state.simulation ? 'Simulated test pattern' :
      state.active ? 'Live gamepad input' : 'Waiting for your gamepad');
    ui.text('help', state.error || (!globalThis.isSecureContext ? 'Gamepad access needs localhost or HTTPS.' :
      !state.active ? 'Click this page, then press A or move a stick. For F310 / F710, use X mode.' :
      !state.mapping ? 'Nonstandard mapping: raw values are shown; labelled controls and driving preview are disabled.' :
      'Left stick → steering · RT → throttle · LT → brake. Diagnostic preview only.'));
    const standard = state.active && state.mapping === 'standard', axes = standard ? state.axes : [0, 0, 0, 0];
    for (const [name, offset] of [['left-stick', 0], ['right-stick', 2]]) {
      const transform = `translate(${(axes[offset] || 0) * 25} ${(axes[offset + 1] || 0) * 25})`;
      if (ui.get(name).getAttribute('transform') !== transform) ui.get(name).setAttribute('transform', transform);
    }
    for (let index = 0; index < names.length; index++) {
      const pressure = standard ? state.buttons[index] || 0 : 0;
      ui.get(`pad-b${index}`)?.classList.toggle('pressed', pressure > .1);
    }
    const preview = drivingPreview(state);
    for (const name of ['steering', 'throttle', 'brake']) ui.text(name, `${Math.round(preview[name] * 100)}%`);
    if (ui.get('mapping').parentElement.open) {
      ui.text('mapping', `${state.mapping || 'raw'} mapping · ${state.axes.length} axes · ${state.buttons.length} buttons`);
      ui.text('axes', state.axes.map((value, index) => `${index}: ${value.toFixed(3)}`).join('   '));
      ui.text('buttons', state.buttons.map((value, index) => value > .05 ?
        `${state.mapping ? names[index] || index : index} ${value.toFixed(2)}` : '').filter(Boolean).join(' · ') || 'No buttons pressed');
    }
  }
  // Device reads and packet delivery never synchronously render the controller.
  ui.cleanup(input.subscribe(state => {
    latest = state;
    if (frame === null) frame = requestAnimationFrame(render);
  }));
  ui.cleanup(() => { if (frame !== null) cancelAnimationFrame(frame); });
  return {destroy: () => ui.destroy()};
}
