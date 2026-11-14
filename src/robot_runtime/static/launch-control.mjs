/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */

// Restart is one durable manager operation. Its preparer freezes and validates
// replacement inputs before Stop. Disconnect cannot cancel accepted intent.
export async function changeLaunch({http, unitId, revision, mode, action, expectedRun, startOptions = {},
  signal, disconnect = () => {}, progress = () => {}, uuid = () => crypto.randomUUID()}) {
  const check = () => { if (signal?.aborted) throw Error('Request cancelled; inspect runtime state before another command.'); };
  if (!['start', 'stop', 'restart'].includes(action)) throw Error('Unknown launch action');
  check();
  const path = `api/units/${encodeURIComponent(unitId)}/`;
  const status = await http.get('api/status', {signal});
  check();
  const unit = status.units.find(item => item.unit_id === unitId);
  if (!status.fresh || status.config_revision !== revision || !unit) throw Error('Current launch state is unavailable.');
  if (action === 'start' && unit.blocked) throw Error('The current run must finish before Start.');
  if (action !== 'start' && unit.run_id !== expectedRun) throw Error('The selected run changed.');
  const request = {request_id: uuid(), expires_at_ms: status.server_time_ms + 10000, config_revision: revision,
    ...(action === 'start' ? startOptions : {expected_run: expectedRun}),
    ...(action !== 'stop' && mode ? {mode} : {})};
  progress(`${action} request ${request.request_id}`);
  if (action !== 'start') disconnect();
  return http.post(path + action, request, {signal});
}
