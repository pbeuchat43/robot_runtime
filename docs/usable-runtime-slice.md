# Runtime implementation boundaries

The charter and integration contract define the operating requirements. The shared foundation supports the installable package, manager, standalone CLI, and authenticated browser workflow. ROS and media workers extend this foundation through explicit capability interfaces.

## Implementation choices

- A single manager owns SQLite transactions, systemd commands and observations. Distribution `dbus-python` and GLib retain the tested supervisor integration. A private Unix socket admits only the manager's Linux UID; bounded request workers hand mutations to the manager's event loop.
- SQLite records immutable runs and command IDs before dispatch. Recovery observes; it does not replay launch or signal requests. Terminal evidence is persisted before releasing a transient service reference. A collected run whose outcome was never observed stays unknown until explicit local resolution.
- The CLI uses this socket without importing or starting a web server. The web process is a thin optional aiohttp adapter; it shares one manager observation stream among viewers. Losing the web process does not affect workloads or CLI access.
- Profiles select executable argv, working directory, explicit environment, prerequisites, graceful signal/deadline and whether logs are publicly exposed. No adapter accepts executable or profile changes from a browser. Readiness is reported as unavailable.
- Browser command access uses a shared robot passphrase over operator-configured HTTPS, separate expiring browser sessions, exact Origin/Host checks and session-bound CSRF tokens. Pairing-code enrollment is a possible later extension. HTTP on the LAN is not a supported credential transport.
- Packaging targets Python 3.12, Ubuntu 24.04, systemd 255 and cgroup v2 first. Optional web dependencies are installed only in an isolated test environment. Distribution bindings are system dependencies; no package manager changes are made to `jah`.

## Wire boundary

The initial private IPC is version 1, one bounded JSON object per line. Requests include `version: 1` and an `action`. Responses contain either `ok: true, result: ...` or `ok: false, error: {code, message, ...}`. It is an internal transport, not a stable public wire promise.

Actions: `status` (optional fresh audit), `start`, `stop`, `operation`, `logs`, `watch`, and local `resolve`. Start/stop carry a UUID request ID, expiry, configuration revision and expected run where applicable. Retries use the same arguments and identifier only to obtain the saved result; conflicting reuse fails. Clients never automatically retry an uncertain command. Observation reconnects are allowed and establish a full baseline with a new manager generation.

The manager socket is `STATE/runtime.sock`. `python -m robot_runtime.manager --profile FILE --state STATE` runs the manager; `robot-runtime --state STATE ...` operates it. The web process receives the same state path and explicit TLS/origin/credential configuration. Credentials, executable arguments and environment are absent from public status.

## Delivery and checks

The package implements profile validation, durable state, systemd ownership, recovery and explicit commands; manager IPC, CLI, bounded journal access and shared observations; and HTTPS authorization with the reference browser workflow. The [operator guide](runtime-operator-guide.md) covers installation and graceful recovery.

Run correctness checks before a bounded resource capture. Numerical objectives remain provisional; discuss a difficult target before extending optimization. Boot/logout persistence needs a separately coordinated acceptance window.
