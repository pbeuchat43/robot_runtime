# Declared ROS observations and commands

For goal submission, feedback, cancellation and recovery, see the
[ROS action guide](ros-actions.md). Actions share the configured capability and
authenticated command boundary used by services and parameters.

The runtime provides scalar topic observation and typed service calls to the [runtime guide](runtime-operator-guide.md). The tested native environment is ROS 2 Jazzy on Ubuntu 24.04 arm64. Install `rclpy`, `rosidl_runtime_py` and the selected generated interfaces in the configured ROS interpreter's environment. The manager/web venv needs no native ROS imports. No additional ROS bridge server is required.

## Profile

Add an optional `ros` object to a schema-1 runtime profile. The example below expects an explicitly started unit named `provider` that supplies these interfaces; it does not create that provider. Its trusted launch environment must use the same domain and localhost discovery. Select a domain reserved for this deployment.

```json
{
  "schema_version": 1,
  "python": "/usr/bin/python3",
  "setup_scripts": ["/opt/ros/jazzy/setup.bash"],
  "domain_id": 42,
  "discovery": "LOCALHOST",
  "topics": [{
    "id": "counter", "label": "Counter", "unit_id": "provider",
    "node": "/example/provider", "name": "/example/counter",
    "type": "std_msgs/msg/Int64", "fields": ["data"],
    "qos": {"reliability": "best_effort", "durability": "volatile", "depth": 1},
    "max_rate_hz": 5, "stale_after_seconds": 1,
    "max_serialized_bytes": 1024, "max_json_bytes": 1024
  }],
  "services": [{
    "id": "enable", "label": "Request enable", "unit_id": "provider",
    "node": "/example/provider", "name": "/example/enable",
    "type": "std_srvs/srv/SetBool", "request_fields": ["data"],
    "response_fields": ["success", "message"], "success_field": "success",
    "timeout_seconds": 2
  }]
}
```

Setup scripts are trusted executable installation inputs, sourced in order with a cleared inherited environment. The interpreter and scripts must be absolute accessible paths. Overlays can follow the distribution setup script. ROS logs go beneath the private state directory. Native interface validation occurs in the worker at manager startup, before subscriptions or service clients are created. `robot-runtime validate` checks profile structure and paths and explicitly reports that interface loading still needs the worker. Inspect `ros status` and the manager journal for missing types or unsupported fields.

The schema admits up to 48 topics, eight services and eight fixed-choice topic commands, each bound to a declared runtime unit and exact absolute node/name/type. Capability IDs are unique across all ROS categories. Combined topic `max_json_bytes` budgets must fit within 256 KiB. Scalar fields expose direct primitive values: booleans, integers, finite floats and strings. Strings are limited to 4096 characters; declared bounded-string limits also apply. Int64 and uint64 values are canonical decimal strings in JSON, including command arguments. Other integers are JSON numbers. Generic arrays and nested messages are unsupported; bounded [detection and diagnostic projections](cone-observations.md) may be selected instead of scalar topic fields. The typed [IMU projection](spatial-observations.md) provides explicit sensor-frame, reference, timestamp and acceleration semantics. The bounded [LaserScan projection](spatial-observations.md) uses a declared fixed mounting transform and host-clock scan-start estimate. Service request fields must cover the entire request type; response fields are a projection. `success_field`, when supplied, must select an exposed boolean response field.

The aggregate limit reserves space for one JSON sample per configured topic; it
is not a measured bandwidth or process-memory limit. The IPC response
limit is 512 KiB to carry the larger snapshot and its metadata. Per-topic byte
limits, rates and queue counts remain unchanged. After the remaining runtime
units are integrated, review the selected observations and displayed data with
the planned UI work before reducing budgets or removing fields.

Topic reliability is explicitly `reliable` or `best_effort`; durability is `volatile`, history depth one. Delivery ceilings range from 0.2 to 10 Hz (up to 20 Hz for the IMU projection), stale thresholds from 0.1 to 60 seconds, serialized limits from 64 to 65536 bytes, and projected JSON limits from 64 to 16384 bytes. These limits bound bridge work and queues, not DDS's allocation before a callback. Service response deadlines range from 0.1 to 10 seconds and include admission/dispatch time.

## Observation

```sh
robot-runtime ros status
robot-runtime ros watch --topic counter
```

Public HTTPS readers use `GET /api/ros` for the catalog and `GET /api/ros/events?topic=counter` for samples; repeat `topic` for multiple IDs. The browser starts with topics paused. Select streams and choose **Observe topics**. Pausing, hiding or leaving the page closes its observation connection. The backend releases the subscription after its last consumer disconnects, allowing heartbeat/disconnect detection time. Reopening starts a new stream when no other consumer kept it active. Provider workloads continue until an explicit stop.

All viewers share one native subscription per configured topic capability, with a latest-value queue per consumer. Multiple declared projections of the same ROS topic are separate capabilities. The web adapter shares one private manager watch for its selected capability union. There are at most eight private ROS watchers and eight browser ROS streams, separate from runtime status streams. ROS discovery and the worker remain active while ROS is configured even with no topic demand.

Each browser's combined topic stream is paced to the highest `max_rate_hz` among its selected topics. Independently arriving updates coalesce into the latest snapshot; they do not multiply the browser's send rate. Source sequences and ages remain intact, and a slow client still disconnects after the bounded write timeout.

The browser retries a permanently closed observation stream once per second, including a temporary admission rejection during rapid reloads. Ordinary network interruptions retain EventSource's native reconnect behavior. Pausing or hiding a topic view cancels its pending retry. This applies only to read-only observations; commands are never replayed.

Worker interface readiness, declared service discovery, runtime ownership and topic freshness are separate. A service's `available` flag is a discovery observation; dispatch repeats the checks. A sample carries its run ID, stream generation, sequence, host monotonic receipt time, receipt age and middleware source timestamp when supplied. Nanosecond timestamps are decimal strings. These scalar types supply no sensor acquisition timestamp or frame, so acquisition time is explicitly unknown. A heartbeat never refreshes a sample. A fresh empty string replaces old text. Oversized/invalid samples remove old data; stale retained data is labelled stale. Missing/duplicate graph providers, stopped runs and worker loss invalidate freshness. Graph names do not authenticate a process or prove cgroup membership.

## Commands and uncertainty

```sh
robot-runtime ros call enable --expected-run RUN_ID --arguments '{"data":true}'
robot-runtime operation REQUEST_UUID
robot-runtime ros acknowledge REQUEST_UUID --expected-run RUN_ID
```

The CLI uses the runtime account's private socket and prints a UUID before dispatch. It fills the current profile revision, worker generation and ten-second expiry unless explicitly supplied. An accepted operation initially reports `prepared`; query its UUID for the result. A complete response may say `provider_accepted`, `provider_rejected`, or `response_received` when no success field is declared. This reports the provider's response, not physical effect.

The browser renders forms from the loaded request schema. Its command endpoint is `POST /api/ros/services/CAPABILITY_ID`, requiring the same HTTPS session, Host/Origin and CSRF checks as runtime commands. The JSON body contains `request_id`, `config_revision`, `expected_run`, `generation`, `expires_at_ms` and `arguments`. Service operation results require authentication; topic data and capability metadata are public. A browser cannot choose arbitrary ROS names, types, fields, interpreters or setup paths.

Only one ROS mutation or parameter inspection can be pending globally; there is no waiting queue. The manager checks an owned running unit, no stop request, fresh supervisor evidence, revision/generation and expiry, then checks ownership again immediately before authorizing dispatch. The worker checks exactly one declared graph provider and bounded client readiness. Retained identical commands deduplicate by UUID and full request fingerprint; the existing 5000-operation history limit applies. Arguments are not retained in history, though selected provider responses are.

Timeout, worker failure or manager failure can leave an uncertain outcome. A timeout cannot cancel remote execution. No client or recovery path automatically replays the request. Further ROS mutations for that run are blocked until the operator inspects provider state and locally acknowledges the UUID, or starts a new run through ordinary lifecycle policy. Acknowledgement neither changes the old result nor repeats the command. It is unavailable through the browser. After an uncertain browser POST, use the displayed UUID for lookup; reconnecting only resumes observations and result lookup.

See the [topic/service design comparison](ros-topic-service-slice.md) and [current verification record](verification.md). Later development slices add [bounded cone/diagnostic observations](cone-observations.md), [requested image snapshots](snapshots.md) and [ROS actions](ros-actions.md). Arbitrary arrays, generic application readiness, driving and remote ROS discovery remain outside the implemented scope.

## Fixed-choice topic requests

An optional `ros.commands` list supports providers whose state-request
contract is a topic. The trusted profile supplies every permitted payload:

```json
"commands": [{
  "id": "state-request", "label": "Provider state request", "unit_id": "provider",
  "node": "/example/provider", "name": "/example/request", "type": "std_msgs/msg/UInt8",
  "fields": ["data"], "timeout_seconds": 2,
  "choices": [
    {"id": "disable", "label": "Disable", "arguments": {"data": 0}},
    {"id": "enable", "label": "Enable", "arguments": {"data": 1}}
  ]
}]
```

Each capability has one to eight unique choices, complete primitive message
fields and a one-to-five-second operation deadline. Native startup validation
checks every preset against the generated message type. The CLI is
`robot-runtime ros command state-request disable --expected-run RUN_ID`.
The browser offers a required choice selector and **Publish request**.

`POST /api/ros/commands/CAPABILITY_ID` accepts the same authority fields as a
service request, with `choice` instead of `arguments`. Authentication, CSRF,
run ownership, expiry, deduplication, uncertainty blocking and operation-result
privacy are shared with services. Neither adapter accepts custom message data.
The retained operation includes the selected choice ID.

The worker requires exactly one declared subscription/node, waits at most
0.5 seconds for its finite reliable, volatile, depth-one publisher to match,
reauthorizes dispatch, publishes once, and destroys the publisher after a
bounded 0.2-second middleware acknowledgement wait. Successful publication
reports `published_once` and `application_acceptance: unconfirmed`, even if the
application ignores it. Observe provider state separately. DDS acknowledgement
is not an application acknowledgement, and publication is not a physical-effect
claim. Failure after a publication attempt is conservative uncertainty; restart,
reconnect and duplicate request IDs never resend it.

This fixed-choice mechanism is unsuitable for continuous driving or other commands requiring
a controller lease, fresh input stream or reconnect re-arming.

## Live parameters

The optional `ros.parameters` list extends the same schema-1 profile. Existing profiles remain valid. Each group binds an exact node to a runtime unit and exposes explicit names and write permissions, for example:

```json
"parameters": [{
  "id": "settings", "label": "Counter settings", "unit_id": "provider",
  "node": "/example/provider",
  "parameters": [
    {"name": "rate_hz", "write": true},
    {"name": "increment", "write": true},
    {"name": "identity", "write": false}
  ],
  "timeout_seconds": 3, "stale_after_seconds": 30
}]
```

Expose startup-only or sensitive-to-change parameters with `write: false`. Provider `read_only` descriptors also prohibit edits. Inspections are public, so expose only values/descriptions suitable for public viewing. Up to eight groups with sixteen names each are supported. Names may contain dotted namespaces; wildcards and arbitrary node discovery are not exposed. The native environment needs `rcl_interfaces`, supplied with Jazzy's parameter support.

In **Live parameters**, choose **Read / refresh (discards draft)** after explicitly starting the workload. The editor shows observed values, ROS types, descriptions, constraints, ranges and read-only state. Unlock commands, select the parameters to change, edit their draft values, and choose **Apply selected values**. Each submission uses the node's `SetParametersAtomically` service. A node without that service cannot be edited; there is no non-atomic fallback. Refresh intentionally discards the draft. Another client's mutation invalidates the baseline without silently replacing the draft.

```sh
robot-runtime ros parameters read settings
robot-runtime ros parameters read settings --cached
robot-runtime ros parameters set settings --expected-run RUN_ID --expected-snapshot SNAPSHOT_ID \
  --changes '{"rate_hz":{"type":"double","value":5.0},"increment":{"type":"integer","value":"7"}}'
robot-runtime operation REQUEST_UUID
```

Copy `run_id` and `snapshot_id` from the inspection. The write also accepts explicit `--request-id`, `--generation`, `--config-revision` and `--expires-at-ms`. A type/value object is required for each edited name. Types are `bool`, `integer`, `double`, `string`, `byte_array`, `bool_array`, `integer_array`, `double_array` and `string_array`. Integers, including array elements, use decimal strings to preserve signed 64-bit values. Byte arrays use numbers 0–255; other arrays use JSON values of their corresponding type. Empty arrays preserve their inspected type. Type changes, declarations and deletion are not supported, including for dynamically typed parameters.

An inspection uses the native `GetParameters` and `DescribeParameters` services only on request. Concurrent reads of the same group share a request; refreshes are limited to one start per second per group. There is no parameter polling or event subscription. Original receipt age, run, worker generation and content identity travel with the snapshot; cached reads and reconnects do not renew freshness. Parameter strings are bounded to 4096 characters, arrays to 128 elements, descriptor text to 1024 characters and each complete parameter result to 16 KiB. DDS can allocate incoming data before these checks.

The write verifies the current run, permissions, types, freshness and expected snapshot, then re-reads the edited values/descriptors before dispatch. Detected changes fail with `parameter_conflict` without sending a mutation. This is not compare-and-set: external ROS writers can still race between the check and the atomic call. Ranges and provider callbacks remain authoritative; a rejection applies none of the requested values when the provider honors ROS atomic semantics.

Provider acknowledgement and readback are separate. An accepted response remains accepted even if the following readback fails. A successful readback may differ from the requested values, for example because another writer changed them. Neither observation proves physical effect. A mutation timeout or lost acknowledgement remains uncertain and blocks further ROS mutations for that run until the same local `ros acknowledge` procedure is used. Browser reloads reconcile saved UUIDs without replaying writes.

Public HTTP inspection uses `GET /api/ros/parameters/CAPABILITY_ID?refresh=1`, then cached `GET` requests while `reading` is true. A cached result is the default without `refresh=1`. Authenticated writes use `POST` to the same path with `request_id`, `config_revision`, `expected_run`, `generation`, `expires_at_ms`, `expected_snapshot` and `changes`, plus the usual CSRF header. Operation results require authentication. Writes share the global ROS mutation slot and uncertainty policy with service calls.

Edits affect the live node only. The runtime neither saves them into startup configuration nor reapplies them after restart. See the [parameter design](ros-parameters-slice.md) and [current verification record](verification.md).
