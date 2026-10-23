// Small shared contracts; importing a module starts no timers or browser work.
export function observable(initial) {
  let value = initial;
  const listeners = new Set();
  return {
    get state() { return value; },
    publish(next) { value = next; for (const listener of [...listeners]) listener(value); },
    subscribe(listener) { listeners.add(listener); listener(value); return () => listeners.delete(listener); },
    clear() { listeners.clear(); },
  };
}

const mounts = new WeakMap();
export function scope(root, html) {
  mounts.get(root)?.destroy();
  root.innerHTML = html;
  const removers = [], refs = new Map();
  const view = {
    get(name) {
      if (!refs.has(name)) refs.set(name, root.querySelector(`[data-ref="${name}"]`));
      return refs.get(name);
    },
    text(name, value) {
      const node = view.get(name), text = String(value);
      // Replacing identical text still invalidates layout and accessibility work.
      if (node.textContent !== text) node.textContent = text;
    },
    on(element, type, listener) {
      element.addEventListener(type, listener);
      removers.push(() => element.removeEventListener(type, listener));
    },
    cleanup(fn) { removers.push(fn); return fn; },
    destroy() {
      for (const remove of removers.splice(0)) remove();
      if (mounts.get(root) === view) { root.replaceChildren(); mounts.delete(root); }
    },
  };
  mounts.set(root, view);
  return view;
}

export const number = (value, digits = 1) => Number.isFinite(value) ? value.toFixed(digits) : '—';
export const neutralInput = () => ({active: false, source: 'gamepad', mapping: 'standard',
  axes: [0, 0, 0, 0], buttons: Array(17).fill(0)});

export function drivingPreview(input) {
  if (!input.active || input.mapping !== 'standard') return {steering: 0, throttle: 0, brake: 0};
  const axis = input.axes[0];
  return {steering: Math.sign(axis) * Math.max(0, Math.abs(axis) - .1) / .9,
    throttle: input.buttons[7] || 0, brake: input.buttons[6] || 0};
}
