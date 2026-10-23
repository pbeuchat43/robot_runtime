# ROS actions

Declared ROS actions are exposed to the manager, CLI and reference
browser. Its demonstration counts numbers without hardware access. Goal
acceptance, feedback, cancellation acknowledgement and terminal result are
separate observations.

## Operating policy

- One unresolved goal reserves each declared action capability. Separate
  capabilities may run concurrently, up to four configured actions.
- Any unlocked browser session may request cancellation of that exact goal.
  Cancellation has its own request ID; acknowledgement does not prove the action
  has finished. The provider may refuse either a goal or cancellation.
- Leaving, locking or closing a browser does not cancel a goal. Returning reads
  its current state. Neither browser nor manager automatically resends a goal or
  cancellation after a lost reply or restart.
- A timeout does not prove rejection or completion. An uncertain goal retains
  its reservation until a terminal state is observed or the managed provider run
  is replaced. `ros acknowledge` cannot release an action reservation. Inspect
  the provider before deliberately stopping its run.
- The latest goal and terminal result are retained. Feedback is a bounded,
  expiring observation in memory, not a durable event history.

This policy is for ordinary actions. It does not implement vehicle control,
driving leases, a dead-man timer or a physical stop guarantee.

## Profile contract

`ros.actions` is optional in profile schema version 1. Declare at most four
entries, with IDs unique across all ROS capabilities. For example:

```json
{
  "id": "count-demo",
  "label": "Count without hardware",
  "unit_id": "action_demo",
  "node": "/robot_runtime_demo/provider",
  "name": "/robot_runtime_demo/count",
  "type": "robot_runtime_demo_interfaces/action/Count",
  "goal_fields": ["steps", "period_seconds", "abort_at_end"],
  "result_fields": ["success", "completed", "message"],
  "feedback_fields": ["completed", "total", "message"],
  "response_timeout_seconds": 2,
  "max_feedback_rate_hz": 2,
  "stale_after_seconds": 2
}
```

The configured ROS interpreter/setup scripts must provide the exact generated
action interface. Every goal field must be declared and be a supported primitive
scalar. Result/feedback may expose selected scalar fields; nested messages and
arrays need a later projection extension. The worker validates interfaces before
reporting ready. Goals use the existing strict codec: JSON booleans, finite
numeric floats, and canonical decimal strings for int64/uint64. Arguments and
selected result/feedback JSON each have an 8 KiB limit; strings have a
4096-character limit. Raw status/feedback has a 64 KiB decode limit.

The response deadline is 0.5–10 seconds for acceptance/cancellation replies,
not execution duration. Feedback is coalesced to 0.2–5 Hz with a configured
expiry of 0.5–60 seconds. Its age is host-monotonic bridge receipt age; this slice
does not infer a provider acquisition timestamp. The browser polls cached action
state at 1 Hz while visible. All readers share the same native goal tracking.

Before dispatch, the manager rechecks configuration revision, worker generation,
expiry and exact supervised run ownership. The worker checks the declared native
action name/type and unique graph provider. ROS graph names are not process
authentication; these checks do not establish an OS identity for a ROS node.

## CLI and HTTP

Pass the private state directory to the CLI:

```sh
robot-runtime --state PATH ros actions
robot-runtime --state PATH ros actions --watch
robot-runtime --state PATH ros action send count-demo \
  --expected-run RUN_ID \
  --arguments '{"steps":20,"period_seconds":0.2,"abort_at_end":false}'
robot-runtime --state PATH operation GOAL_REQUEST_ID
robot-runtime --state PATH ros action cancel count-demo \
  --expected-run RUN_ID --goal GOAL_REQUEST_ID
```

The CLI prints each request ID before dispatch. `--request-id`, `--generation`,
`--config-revision` and `--expires-at-ms` support explicit clients. An identical
retained request ID returns its stored operation; changing its body is a conflict.
A new request ID is a new instruction. Watching only reads cached state and
works without a web server.

| HTTP path | Purpose |
| --- | --- |
| `GET /api/ros/actions` | Public cached catalog, latest goals/results and expiring feedback |
| `POST /api/ros/actions/{id}/goals` | Authenticated, CSRF-protected goal submission |
| `POST /api/ros/actions/{id}/cancel` | Authenticated, CSRF-protected exact-goal cancellation |
| `GET /api/operations/{request_id}` | Authenticated durable action operation lookup |

Both POST bodies require `request_id`, `config_revision`, `generation`,
`expected_run` and `expires_at_ms` (within the next 30 seconds). Goals add
`arguments`; cancellations add `goal_request_id`. Paths select configured
capabilities, not arbitrary native targets. Public observations omit private
actor labels and request fingerprints.

## Recovery and native protocol

The canonical goal request UUID is also the native ROS goal UUID. Durable
operation records reserve it before delivery. A per-capability sequence preserves
goal order across wall-clock adjustments. Each cancellation has a separate
durable UUID referring to the goal; its native request contains that exact
nonzero goal UUID and a zero timestamp, never a cancel-all selector.

The worker uses generated SendGoal/GetResult services and native status/feedback
messages. After restart it reconnects with the persisted UUID and performs
read-only GetResult/status observation, following the
[ROS 2 action protocol](https://design.ros2.org/articles/actions.html). This avoids
requiring a Python `ClientGoalHandle` to survive a process restart. No goal
arguments are replayed. Provider result caches are finite: if the provider no
longer knows the goal and no terminal status was observed, its outcome remains
unknown. Known terminal status and unavailable/invalid result are reported
separately.

Native clients/subscriptions exist only for tracked goals and are released on
completion, provider loss or run replacement. Closing the UI leaves tracking
active until the goal resolves. Provider stop/replacement and manager failure
invalidate current feedback. API/profile/state versions remain 1; actions add
operation kinds without changing the SQLite schema.

## Synthetic provider

`deployment/action_demo_interfaces` is a small MIT-licensed rosidl package. Build
it with the prepared ROS 2 toolchain and add its installed `local_setup.bash` to
the manager's ROS setup scripts. The runtime wheel does not depend on this demo
interface.

`deployment/action_demo.py` accepts 1–100 steps and a 0.05–1 second period. It
succeeds at the end, or deliberately aborts when `abort_at_end` is true. It
accepts explicit cancellation and exits after its finite 600-second lifetime
(300 seconds is also supported). Normal stop uses SIGINT. It touches no hardware
and publishes no vehicle commands. `--test-controls` enables isolated test-only
delay/refusal/feedback-pause controls; the installed demo omits it.

Explicitly start **Synthetic counting action**, enter **20** steps and **0.2**
seconds, leave **abort at end** unchecked, and send the goal. Use **Cancel goal**
on a longer count to observe cancellation. Stop the demo when finished.

## Verification

The [test runbook](../tests/README.md#ros-actions) covers policy, native ROS, CLI, and Chromium checks with the finite synthetic provider. These checks do not establish hardware behavior, vehicle control, or stable-release sizing.
