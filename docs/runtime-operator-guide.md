# Runtime CLI and browser guide

This guide covers development package `0.1.0.dev19` for Python 3.12, Ubuntu 24.04, systemd 255, and cgroup v2. It includes explicit lifecycle commands, authenticated browser access, [ROS capabilities](ros-operator-guide.md), [snapshots](snapshots.md), and [spatial observations](spatial-observations.md). Stable release and deployment acceptance remain separate work.

## Standalone installation

Use fresh state and a distinct namespace for a new installation. Runtime-owned environment names use `ROBOT_RUNTIME_*`, transient services use `rr-`, and browser sessions use `__Host-robot_runtime`. This package does not adopt another runtime's live services or saved state.

A prepared host needs distribution `python3-dbus`, `python3-gi` and Python venv support. Use a virtual environment with system site packages so those compiled bindings remain available. The optional web dependency is pinned in `pyproject.toml`; retain its dependency wheels for offline installation. Installed assets are served locally without a CDN or Internet service.

Example installation, performed by the operator on a prepared host, with a previously built wheel:

```sh
python3 -m venv --system-site-packages ~/.local/share/robot-runtime/venv
~/.local/share/robot-runtime/venv/bin/python -m pip install './robot_runtime-0.1.0.dev19-py3-none-any.whl[web]'
mkdir -p -m 700 ~/.config/robot_runtime ~/.local/state/robot_runtime
```

For offline setup, prepare a compatible wheelhouse first and add `--no-index --find-links /path/to/wheelhouse` to the install command. This repository does not install OS packages or alter account persistence automatically. Build the wheel with `python -m build --wheel --no-isolation` in a prepared build environment.

## Profile and manager

Save a profile as `~/.config/robot_runtime/profile.json`, selecting only trusted installed provider commands. This harmless example exits after ten minutes unless explicitly stopped:

```json
{
  "schema_version": 1,
  "robot_id": "example-robot",
  "namespace": "research",
  "units": [{
    "id": "example",
    "label": "Harmless example",
    "argv": ["/usr/bin/sleep", "600"],
    "working_directory": "/tmp",
    "environment": {},
    "requires_running": [],
    "public_logs": false,
    "stop": {"signal": "SIGTERM", "timeout_seconds": 5}
  }]
}
```

Profiles contain 1–20 units. Executable and working paths must be absolute and accessible. IDs use up to 32 lowercase ASCII letters, digits, underscores or hyphens, starting with a letter. Prerequisites must exist and be acyclic; they are checked at start and never launched automatically. Unknown fields/schema versions are rejected. `public_logs` defaults to false; explicitly enabling it exposes that provider's retained output to unauthenticated viewers. Provider output must be suitable for that audience.

a unit may instead contain only `id`, `label`, `available: false` and a nonempty `unavailable_reason` (up to 512 characters). Such entries carry no executable or launch configuration. The CLI/API reject start with `unit_unavailable`; the browser shows the reason and disables Start. An available unit cannot depend on an unavailable one. Their displayed state does not observe another runtime's processes. Availability defaults to true for existing profiles. This is an additive schema-1 capability; older packages reject profiles that use it. Change profiles only during stopped maintenance.

available units may declare `conflicts: ["other_unit"]`.
Each conflict must name another available unit and be declared in both
directions. Duplicates, self-conflicts and a conflicting direct prerequisite
are rejected. An omitted list preserves existing behavior and profile identity.
The shared command policy rejects every start mode with `unit_conflict` while
a conflicting run is populated or unresolved, including a pending start or
timed-out stop. It never stops the other unit automatically. Status reports
`start_blocked_by`; the browser names the blocking units and disables their
peer's start buttons. This coordinates this manager's workloads; it does not
reserve hardware against processes launched outside the runtime.

The runtime account is the manager's Linux UID. The launch wrapper is `/usr/bin/env -i` followed by the declared environment and executable argv; arbitrary manager/SSH environment is cleared. Supply required `PATH`, ROS variables and trusted setup wrappers explicitly. `ROBOT_RUNTIME_*` names are reserved. The wrapper `exec`s the provider in place; systemd's initial `Type=exec` acknowledgement concerns the wrapper, while provider failure is reported through subsequent exit evidence. Do not put secrets in profile argv/environment or provider logs. Web credential references are separate startup options; the optional `ros` block declares permitted ROS capabilities.

```sh
~/.local/share/robot-runtime/venv/bin/robot-runtime validate ~/.config/robot_runtime/profile.json
~/.local/share/robot-runtime/venv/bin/robot-runtime-manager \
  --profile ~/.config/robot_runtime/profile.json --state ~/.local/state/robot_runtime
```

The manager creates a private state directory and Unix socket. Starting it launches no workloads. It owns one SQLite history and admits local commands only from its Linux UID. One file lock prevents two managers using the same state directory. Do not clone a live state directory into another running installation. A namespace is immutable within a state directory; profile changes require all previous runs stopped and resolved.

## CLI workflow

The `robot-runtime` entry point defaults to `~/.local/state/robot_runtime` (or the corresponding `XDG_STATE_HOME` location). Use `--state DIRECTORY` before the subcommand for another installation.

```sh
robot-runtime status
robot-runtime start example
robot-runtime status --fresh
robot-runtime logs RUN_ID --limit 100
robot-runtime stop example --expected-run RUN_ID
robot-runtime operation REQUEST_UUID
robot-runtime inspect RUN_ID
robot-runtime watch
```

CLI output is JSON. A mutation prints its request ID to stderr before dispatch. Start completion establishes supervisor acceptance, not application readiness. Readiness is explicitly unavailable in this slice. `status` uses cached observations; `--fresh` audits the supervisor. Ownership-property age is separate from lifecycle observation age. Browser status keeps acquisition age across delivery/reconnection and becomes unavailable if observation is too old.

Every accepted start gets a unique run ID and never-reused service name. A populated or unresolved run blocks replacement. Stop requires its expected run ID, sends one declared graceful signal to the cgroup and observes the deadline. It never escalates or repeats automatically. A timeout remains in operation history even if the provider subsequently exits. A launcher that forwards/escalates signals needs provider-specific validation; these mechanics do not prove ROS shutdown or physical stopping.

After a lost response, query `operation REQUEST_UUID` and current status. Do not issue a new start merely because the response was lost. The CLI/API support explicit UUID and expiry fields; identical retained requests return their recorded operation, while reuse with different arguments fails. Clients never replay a mutation automatically. A new browser connection may resume observations and query operation IDs.

If a run was collected before terminal evidence was saved, the runtime reports `outcome_unknown`. After inspection, `robot-runtime resolve UNIT --expected-run RUN_ID` permits the local operator to acknowledge a freshly verified absent service. It does not claim the launch never happened, rewrite its old operation or signal anything. This recovery action is deliberately absent from the browser adapter.

History retains up to 5,000 operations without automatic eviction, preserving the deduplication boundary. At the limit, new mutations fail with `history_full`. Archive the stopped installation's complete state during explicit maintenance before creating a new installation. Never delete or replace state while any workload or ownership question remains. State schema 1 has no implicit migration.

## HTTPS and command access

Choose one HTTPS origin, such as `https://robot.example:8443`, and provide a certificate/private key valid for that hostname or IP. Each browser must trust the certificate's issuing authority. Certificate/DNS provisioning belongs to the deployment operator; this package does not install a CA, bypass warnings or open firewall rules. HTTP credential transport and reverse-proxy forwarding headers are unsupported in this slice.

```sh
robot-runtime credential --file ~/.config/robot_runtime/credential.json
robot-runtime-web --state ~/.local/state/robot_runtime \
  --origin https://robot.example:8443 --bind 0.0.0.0 --port 8443 \
  --credential ~/.config/robot_runtime/credential.json \
  --cert ~/.config/robot_runtime/tls.crt --key ~/.config/robot_runtime/tls.key
```

Credential creation prompts without echo and refuses to overwrite a file. It stores a salted scrypt verifier, not the passphrase. The page and configured observations are public; every start/stop requires a robot-verified session, exact configured Host/Origin and a session CSRF token. Secure, HttpOnly, SameSite=Strict cookies hold separate random browser sessions. Sessions expire after 30 minutes without an authenticated command or after eight hours overall. Browser labels/session IDs are attribution aids, not individual identities.

Locking commands revokes that session. Web restart revokes all sessions and preserves managed workloads. To rotate the passphrase, create a new private credential file and atomically replace the configured file; the web adapter invalidates sessions when it detects that replacement. Verifications are serialized and rate-limited; at most 32 sessions and eight observation streams are retained. One upstream manager stream is shared by viewers, with one replaceable queued snapshot per viewer. Slow writes expire rather than block other clients. Five connections are the intended sizing target; eight is a connection cap with reconnect headroom.

The reference page supports unlock, start, observed status, bounded logs, graceful stop and lock. It uses only local static assets. Log pages contain at most 100 entries, truncate individual messages, and impose a 64 KiB record budget. Cursor gaps and host retention limits are explicit. Following logs is user-controlled and pauses while the page is hidden. Application output may contain terminal escape characters or untrusted text; the browser renders it as text, never HTML.

## Service examples and recovery

The wheel contains `robot_runtime/resources/robot-runtime-manager.service` and `robot-runtime-web.service`. Review their paths and replace the example origin before copying them to `~/.config/systemd/user/`. Only management services should be enabled. Workload services remain transient and have no dependency that propagates a manager/web stop.

Use `robot-runtime inspect RUN_ID` to obtain the retained service name and invocation before Linux recovery, then compare them with fresh supervisor facts. Inspection returns retained identity, not permission to act on a changed service. Never infer ownership from a prefix alone. Ordinary inspection commands are:

```sh
systemctl --user show EXACT_SERVICE_NAME \
  -p InvocationID -p ActiveState -p SubState -p Result -p ControlGroup -p SendSIGKILL
journalctl _SYSTEMD_INVOCATION_ID=EXACT_INVOCATION_ID --no-pager -n 100
```

Prefer the CLI's single-signal stop. `systemctl --user stop EXACT_SERVICE_NAME` is an operator recovery tool whose stop phases can repeat the configured graceful signal even with force-kill disabled; use it only after checking ownership and provider tolerance. Inspect the actual cgroup for remaining members. `inactive`/`failed` alone does not prove all work is gone. Do not use wildcard kill commands or discard unresolved metadata.

The user manager, kernel and same-account administrative actions remain outside the manager's protection. Lost supervisor connectivity invalidates freshness and triggers reconnect/reconciliation without command replay. These mechanisms do not establish survival of user-manager failure, OOM, reboot, storage corruption or hostile code under the shared Linux account.

## References

- [Foundation decisions and evidence](runtime-foundation-design.md).
- [systemd 255 transient-unit reference handling](https://github.com/systemd/systemd/blob/v255/src/core/dbus-unit.c): `AddRef` retains short-lived results during creation.
- [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html): the implemented scrypt parameters are N=32768, r=8, p=3.
- [aiohttp graceful shutdown](https://docs.aiohttp.org/en/stable/web_advanced.html#graceful-shutdown): observation streams are closed explicitly before shutdown waits.
