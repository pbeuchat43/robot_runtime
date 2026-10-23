# Runtime foundation design

The runtime uses a persistent Python manager, durable SQLite state, and systemd transient user services. The [charter](repository-charter.md) and [integration contract](integration-contract.md) define required behavior; this document explains the implementation boundaries.

## Supervision and lifetime

Management and HTTPS services may restart independently. They do not own application lifetime through a parent process, and stopping them does not implicitly stop workloads. Only explicitly requested workloads start. Dependencies are preconditions, not implicit start requests.

The manager launches each run using `Type=exec`, `ExitType=cgroup`, `KillMode=control-group`, `Restart=no`, and an explicit graceful signal. Automatic SIGKILL escalation is disabled. A stop timeout remains visible as blocked/uncertain and preserves ownership for inspection and recovery. The runtime does not declare success merely because the main PID exits while descendants remain.

`/usr/bin/env -i` clears inherited environment before executing the declared argv. `ROBOT_RUNTIME_RUN` is reserved for run identity. Transient names begin with `rr-` and bind namespace, installation identity, unit, and run. Descriptions use `robot-runtime:<run_id>`.

Ownership requires matching transient service identity, invocation, launch argv/environment, working directory, stop properties, and other expected supervisor properties. A similar service name or PID alone is insufficient. Fresh command validation is separate from cached observations.

## Durable operations and recovery

The manager writes immutable run specifications and request records before dispatch. Repeating an identical retained request ID returns the recorded operation; a conflicting body is rejected. Request expiry, configuration revision, and expected run constrain commands.

Recovery observes supervisor state and recorded operations; it does not replay uncertain start/stop requests. Terminal evidence is saved before releasing service references. A lost outcome stays unknown until reconciled or explicitly resolved through supported local recovery. PID reuse cannot establish ownership.

Management uses systemd D-Bus signals, cached proxies, and bounded audits of its namespace. Cgroup membership is inspected for relevant runs. Optional resource sampling is separate from lifecycle policy. Provider readiness, ROS freshness, supervisor state, and command outcomes remain distinct observations.

## Shared application and clients

The policy core is shared by the Unix-socket CLI and HTTPS adapter. The web adapter does not implement an independent command policy. Bounded latest-state streams replace stale baselines after reconnect. Clients reconcile uncertain operations without automatic replay.

Native ROS and image/media processing use separate workers with explicit lifetime and private endpoints. Their availability cannot block lifecycle recovery. See [browser delivery](modular-runtime-delivery.md) and the [media contract](camera-gamepad-integration.md).

## Installation and verification

The initial target is Python 3.12 on Ubuntu 24.04, systemd 255, and cgroup v2. D-Bus/GLib bindings come from the prepared host; the optional web dependency and local assets are packaged separately from robot profiles. A new installation uses fresh private state and a distinct namespace. It does not adopt another runtime's live services or saved state.

Use the [operator guide](runtime-operator-guide.md) and [verification runbook](../tests/README.md). Unit fixtures establish policy behavior; native systemd, ROS, and Chromium checks establish selected software behavior on the test host. Hardware, boot/logout persistence, stable sizing, and deployment acceptance require their own evidence.

## Primary references

- [systemd v255 service reference](https://github.com/systemd/systemd/blob/v255/man/systemd.service.xml)
- [systemd v255 kill reference](https://github.com/systemd/systemd/blob/v255/man/systemd.kill.xml)
- [systemd v255 D-Bus reference](https://github.com/systemd/systemd/blob/v255/man/org.freedesktop.systemd1.xml)
