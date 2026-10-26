/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */

// A browser restart is an explicit Stop followed by Start, not a durable retry
// workflow. Any uncertainty, cancellation, replacement run, or changed profile
// ends the sequence. The manager rechecks all Start policy at dispatch.
export async function changeLaunch({http, unitId, revision, mode, action, expectedRun,
  signal, disconnect = () => {}, progress = () => {},
  now = () => performance.now(), sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  uuid = () => crypto.randomUUID()}) {
  const check = () => { if (signal?.aborted) throw Error('Sequence cancelled; inspect runtime state before another command.'); };
  if (!['start', 'stop', 'restart'].includes(action)) throw Error('Unknown launch action');
  const path = `api/units/${encodeURIComponent(unitId)}/`;
  const prepare = async () => {
    check();
    const value = await http.post(path + 'prepare-launch', {config_revision: revision, ...(mode ? {mode} : {})}, {signal});
    check();
    const unit = value.status.units.find(item => item.unit_id === unitId);
    if (!value.status.fresh || value.config_revision !== revision || !unit) throw Error('Current launch state is unavailable.');
    return {...value, unit};
  };
  let prepared;
  if (action !== 'stop') {
    prepared = await prepare();
    if (action === 'start' && prepared.unit.blocked) throw Error('The current run must finish before Start.');
    if (action === 'restart' && (prepared.unit.run_id !== expectedRun || prepared.unit.ownership !== 'owned' || prepared.unit.stop_request_id)) {
      throw Error('The selected run changed or already has a Stop request.');
    }
  }
  const command = async (name, state, fields) => {
    check();
    const request = {request_id: uuid(), expires_at_ms: state.server_time_ms + 10000,
      config_revision: revision, ...fields};
    progress(`${name} request ${request.request_id}`);
    const operation = await http.post(path + name, request, {signal});
    check();
    return operation;
  };
  if (action !== 'start') {
    const status = prepared?.status || await http.get('api/status', {signal});
    check(); disconnect();
    let operation = await command('stop', status, {expected_run: expectedRun});
    if (action === 'stop') return operation;
    const deadline = now() + (prepared.stop_timeout_seconds + 5) * 1000;
    while (['prepared', 'in_progress'].includes(operation.phase)) {
      if (now() >= deadline) throw Error('Stop confirmation timed out; Start was not sent.');
      await sleep(200); check();
      operation = await http.operation(operation.request_id, {signal});
      check();
    }
    if (operation.phase !== 'complete' || !['cgroup_empty', 'already_stopped'].includes(operation.outcome)) {
      throw Error(`Stop ${operation.phase}; Start was not sent. Inspect runtime state.`);
    }
    prepared = await prepare();
    if (prepared.unit.run_id !== expectedRun || prepared.unit.blocked) throw Error('Run changed or cleanup is incomplete; Start was not sent.');
  }
  return command('start', prepared.status, mode ? {mode} : {});
}
