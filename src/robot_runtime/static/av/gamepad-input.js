import {neutralInput, observable} from './core.js';

// Headless browser input. Multiple views/clients share one sampling loop.
export class GamepadInput {
  constructor({navigator = globalThis.navigator, document = globalThis.document,
    now = () => performance.now(), requestFrame = fn => requestAnimationFrame(fn),
    cancelFrame = id => cancelAnimationFrame(id)} = {}) {
    Object.assign(this, {navigator, document, now, requestFrame, cancelFrame});
    this.events = observable({...neutralInput(), devices: [], selected: null, error: '', simulation: false});
    this.selected = null; this.simulation = false; this.started = now(); this.users = 0; this.frame = null; this.closed = false;
  }
  get state() { return this.events.state; }
  subscribe(fn) { return this.events.subscribe(fn); }
  select(index) { this.selected = Number(index); }
  simulate(value) { this.simulation = Boolean(value); this.started = this.now(); }
  sample(at = this.now()) {
    let pads = [], error = '';
    try { pads = [...(this.navigator?.getGamepads?.() || [])].filter(pad => pad?.connected); }
    catch (cause) { error = cause.message; }
    if (!pads.some(pad => pad.index === this.selected)) this.selected = pads[0]?.index ?? null;
    const devices = pads.map(({index, id, mapping}) => ({index, id, mapping}));
    const focused = !this.document || (!this.document.hidden && this.document.hasFocus());
    let input = neutralInput();
    if (focused && this.simulation) {
      const t = (at - this.started) / 1000, buttons = Array(17).fill(0);
      buttons[Math.floor(t * 2) % 17] = 1;
      buttons[6] = (Math.sin(t) + 1) / 2; buttons[7] = (Math.cos(t) + 1) / 2;
      input = {active: true, source: 'simulation', mapping: 'standard',
        axes: [Math.sin(t), Math.cos(t), Math.cos(t * .7), Math.sin(t * .7)], buttons};
    } else if (focused) {
      const pad = pads.find(value => value.index === this.selected);
      if (pad) input = {active: true, source: 'gamepad', mapping: pad.mapping === 'standard' ? 'standard' : '',
        axes: [...pad.axes].slice(0, 16).map(value => Math.max(-1, Math.min(1, value))),
        buttons: pad.buttons.slice(0, 32).map(button => Math.max(0, Math.min(1, button.value)))};
    }
    this.events.publish({...input, devices, selected: this.selected, error, simulation: this.simulation, focused});
    return this.state;
  }
  acquire() {
    if (this.closed) throw Error('Input has been disposed');
    if (++this.users === 1) {
      const tick = at => { this.sample(at); if (this.users && !this.closed) this.frame = this.requestFrame(tick); };
      this.frame = this.requestFrame(tick);
    }
    let released = false;
    return () => { if (!released) { released = true; if (--this.users === 0) { this.cancelFrame(this.frame); this.frame = null; } } };
  }
  dispose() { this.closed = true; if (this.frame !== null) this.cancelFrame(this.frame); this.frame = null; this.events.clear(); }
}
