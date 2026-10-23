# Cone observations and detector progress

Cone and diagnostic observations are public and demand-driven. Start their provider explicitly, then select **Live observations → Observe topics**. The browser and CLI share the manager/worker observation path; observing never starts a camera.

The plot places the car origin at bottom center: **+x upward, +y left, +z up**, in metres in `base_link`. Both screen axes use the same metric scale. The current view covers 0–6 m forward and ±3 m sideways; this rectangle is a plotting extent, not a camera field-of-view model. Blue, yellow and unknown classifications use blue, yellow and gray markers. Hover a marker to inspect its full x/y/z position and classification confidence. The sample details retain full 3D coordinates and lossless acquisition timestamps.

## What the states mean

| Evidence | Presentation |
| --- | --- |
| Current batch with cones | Show that batch's positions, count and receipt/acquisition ages. |
| Current empty batch | Clear all previous cones and show “Fresh empty frame”. |
| Expired receipt or acquisition timestamp | Remove live positions; mark the sample stale. |
| Unsupported frame, invalid/oversized sample, ambiguous source or worker loss | Remove live positions and report the unavailable/error state. |
| Pause, hidden page, disconnected runtime observations or stopped workload | Remove live positions and positive health; release this page's topic demand where applicable. |
| Fresh diagnostic level OK with a recently increasing frame counter | Show “Frames advancing”. |
| Fresh diagnostic level OK with no observed counter increase | Show “Waiting for frame progress”. A running process or a recent diagnostic alone is insufficient. |
| Provider warning/error/stale level | Show the provider's diagnostic level and message. |

A reference configuration uses a **0.75 s** cone expiry and **3 s** diagnostic/progress expiry. These are observation policies, not driving timeouts. Old raw sample data may remain under the explicitly labelled details; it is never retained as live geometry. No stable object IDs or tracking are invented.

Batches over 20 detections retain the **first 20 in provider order**, with original count, retained count and truncation reported. Points outside the plotting extent are counted separately. No distance or confidence ranking is applied. The raw DDS byte budget can reject a still larger batch before conversion.

## Configurable adapters

A topic declares either the existing scalar `fields` or a bounded `projection`, never both. The runtime imports no robot deployment internals. The configured ROS interpreter loads the declared public message type and validates its shape before subscribing. The integration profile supplies the provider-specific mapping.

For a spatial message with a standard ROS Header, a sequence of point records and a latency field:

```json
"projection": {
  "kind": "detections",
  "array_field": "detections",
  "position_field": "position",
  "color_field": "color",
  "confidence_field": "classification_confidence",
  "latency_field": "acquisition_to_publish_latency_s",
  "frame_id": "base_link",
  "max_items": 20,
  "forward_m": 6,
  "half_width_m": 3,
  "acquisition_clock": "system"
}
```

This adapter's contract is `geometry_msgs/Point` in metres with x forward, y left, z up; uint8 color 0 unknown, 1 yellow, 2 blue (other values render gray); floating confidence in [0, 1]; finite nonnegative acquisition-to-publish latency in seconds; and a Header acquisition stamp using the host system clock. The frame must match exactly; no implicit TF conversion is performed. `max_items` is 1–20, forward extent 1–50 m and half-width 1–25 m. Unsupported clock domains, including simulated ROS time, require a later adapter extension.

`acquisition_timestamp_ns` and diagnostic progress counters are decimal strings. Acquisition age is computed against the declared host clock at receipt, then ages monotonically through the manager, web adapter and browser. This prevents republishing or reconnecting from refreshing old acquisition evidence. Zero/invalid timestamps and stamps more than 0.1 s ahead of that clock are rejected. A subsequent system-clock correction can affect new samples; the configured clock agreement is a deployment assertion, not clock synchronization proof. DDS source time is retained separately and is not labelled sensor acquisition time.

The diagnostic adapter supports `diagnostic_msgs/msg/DiagnosticArray` and selects one exact status name:

```json
"projection": {
  "kind": "diagnostic",
  "status_name": "oakd_cone_detector: OAK-D cone detector",
  "keys": ["runtime_state", "stalled", "frames_delivered", "tf_available"],
  "progress_key": "frames_delivered"
}
```

Expose 1–16 unique diagnostic keys, including a nonnegative cumulative integer counter. The first counter establishes a baseline; an increase establishes progress. A decrease resets that baseline. Missing or duplicate status records, duplicate selected keys, missing/invalid counters and exceeded byte limits invalidate the sample. Other absent keys are omitted. Provider diagnostic levels remain authoritative; “Frames advancing” is an observation of progress, not general application readiness or detection accuracy.

Both adapters inherit explicit topic/node/type, volatile depth-one QoS, rate, receipt-expiry, serialized-byte and JSON-byte limits. One native subscription and bounded latest-value conversion serve all consumers of a configured capability. Functional stream sharing does not establish resource or five-device acceptance targets. Pausing or hiding the last view releases its subscriptions. Reopening establishes a new stream generation and requires new samples and counter progress.

The worker requires exactly one matching topic publisher and declared node. Multiple publishers on the shared `/diagnostics` topic currently make that diagnostic capability unavailable; it does not merge or authenticate publishers. Graph names and middleware IDs are not proof of process identity. Runtime ownership, run ID, boot ID, worker generation, configuration revision and freshness remain separate checks.

## Spatial view

The [shared spatial view](spatial-observations.md) supports cone, scan, and fiducial layers in 2D/3D. Detections represent selected observed objects, not every obstacle in the scene. They do not establish a driving view or physical safety. See the [verification runbook](../tests/README.md).
