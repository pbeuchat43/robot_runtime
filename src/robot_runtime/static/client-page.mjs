/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
import {HttpClient} from './client-http.mjs';
import {StreamClient} from './client-stream.mjs';
import {MultiplexSseTransport} from './client-multiplex.mjs';

// Construction is inert; the composition chooses acquisition, connection,
// visibility and teardown. Each page can mount any number of views.
export function createPageClients({base = '/', headers = () => ({})} = {}) {
  const transport = new MultiplexSseTransport({base});
  const http = new HttpClient({base, headers});
  const clients = Object.fromEntries(['runtime', 'observations', 'resources'].map(channel =>
    [channel, new StreamClient({channel, transport})]));
  return {...clients, http, dispose() {
    for (const client of Object.values(clients)) client.dispose();
    transport.dispose(); http.dispose();
  }};
}
