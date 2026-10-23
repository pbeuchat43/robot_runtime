# On-demand resource monitoring

The shared resource collector supplies bounded host, GPU, and workload observations only while requested.

## Operation

Open **Resource monitoring** on the runtime page. It shows host CPU, RAM and
swap, Jetson GPU activity and temperature, and current CPU/RSS/charged memory
for each declared workload. Three small charts show host CPU, RAM use and GPU
activity over the last ten minutes. Viewing never starts an application workload
and does not require command unlock.

Closing the panel, hiding the page or leaving it releases that browser's
demand. Other browser or CLI viewers can keep the shared collector active. The
last viewer leaving stops sampling and its owned GPU telemetry helper. The
collector stores at most 600 host/GPU points in memory; it returns only points
younger than ten minutes. Paused periods and unavailable readings leave gaps.
History is not written to disk. Restarting the manager clears it.

```sh
robot-runtime --state /path/to/state resources
robot-runtime --state /path/to/state resources --history
robot-runtime --state /path/to/state resources --watch
```

The first two commands only read cached data. `--watch` activates monitoring
until interrupted, sharing the same collector with browsers and other CLI
viewers. Its first JSON line includes retained history; later lines contain
current samples. These commands do not depend on a running web server.

## Meaning and limits

| Measurement | Meaning |
| --- | --- |
| Host CPU | Busy time as a percentage of all CPU time in `/proc/stat`; idle and I/O wait are excluded; guest counters are not double-counted |
| Host RAM used | `MemTotal - MemAvailable`, from `/proc/meminfo` |
| Host swap used | `SwapTotal - SwapFree`; zram physical cost is not separately measured |
| Workload CPU | Change in its entire cgroup's `cpu.stat usage_usec`, divided by elapsed monotonic time; 100% equals one core and values can exceed 100% |
| Workload RSS | Approximate sum of resident pages across current cgroup processes and descendants; shared pages can be counted more than once |
| Charged memory | The cgroup's `memory.current`, including descendant charges, page cache and kernel memory; this is different from RSS |
| GPU activity and temperature | Host-wide NVIDIA `tegrastats` `GR3D_FREQ` activity and `GPU` temperature; no per-workload GPU attribution |

The first CPU reading after start/resume or a counter/run change is unavailable
until there are two valid observations. Supervisor ownership and current run
identity gate workload attribution. Unknown, stale, stopped, missing, inaccessible
or over-limit sources are unavailable, never fabricated zeroes. Individual
metrics can be unavailable while others remain current. Host-wide GPU activity
can include processes outside the runtime. Temperature sentinel values outside
the supported range are unavailable.

Linux `/proc` and cgroup v2 provide CPU/memory without scanning every process on
the host. Each selected cgroup traversal is limited to 64 groups and 256 PIDs;
exceeding that limit makes RSS unavailable. The GPU path uses one owned
`tegrastats --interval 1000` subprocess and nonblocking bounded output reads.
It requires `tegrastats` on the manager's executable search path; other Linux
hosts retain CPU/memory monitoring with GPU telemetry unavailable. No clocks,
power settings, services or application parameters are changed.

Measurements and charts are approximate diagnostic observations. They do not
establish provider readiness, hardware health, deterministic latency or driving
authority. RAM and GPU memory are not separate additive totals on a Jetson.

The metric definitions follow the Linux [cgroup v2 documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
and NVIDIA's [Tegrastats guide](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html).

## Interfaces and sharing

Private IPC version 1 adds `resource_status` (optional boolean `history`) and
`resource_watch` (no arguments). Public GET `/api/resources?history=true` returns
cached history without activating demand. GET `/api/resources/events` streams
SSE snapshots and activates demand. No profile change is required.

Snapshots include `api_version`, manager-local `generation`, `sequence`,
`collecting`, private consumer count, interval/retention/staleness limits, sample
age and `fresh`. `latest` has monotonic and wall-clock sample times, host/GPU
metrics and workload rows with run IDs and errors. Freshness describes the
sample's age; inspect metric errors and null values for availability.
`history_columns` describes compact history arrays. Retention uses monotonic
time, so changing the wall clock does not extend the buffer.

One web upstream watch serves up to eight resource streams; five viewers are
the supported sizing target. Up to eight private resource watches are allowed
within the manager's existing 16-connection budget. Each consumer has one
replaceable pending value, and history has a fixed 600-point ceiling. A new
browser stream and upstream reconnection receive retained history once; ordinary
updates carry only the latest sample. Missing updates cannot create an unbounded
queue. Connection loss marks browser observations stale, and read-only streams
reconnect without replaying commands.

The collector samples off the policy thread. It uses cached ownership evidence
updated by normal supervisor audits; it does not add a 1 Hz systemd audit or ROS
subscription. Resource demand is independent of ROS observations and snapshots.

## Verification

Use the [resource suites](../tests/README.md#resource-monitoring) for demand sharing, helper cleanup, history gaps, manager recovery, and standalone CLI access. A bounded resource capture is separate from functional checks; provisional charter budgets are not implied by a passing fixture.
