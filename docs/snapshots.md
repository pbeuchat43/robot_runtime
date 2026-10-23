# Requested image snapshots

A profile may declare explicit image-producing launch modes and bounded snapshot sources. Ordinary Start uses the default mode. Stop a running unit before selecting another mode. Opening a page or requesting a snapshot never starts, restarts, or reconfigures a workload. Camera providers own which images a declared mode produces.

The browser shows one captured image, its acquisition age and its source/run
details. After two seconds it labels and dims the image as stale. It does not
refresh automatically. A stopped/changed run, lost runtime freshness or a hidden
page clears the image and cancels pending capture work. **Cancel** releases the
current viewer. Each new capture starts without the previous image. Snapshots are
not a live video or driving view.

## Limits and process boundaries

The profile may declare up to four `sensor_msgs/msg/Image` sources. One capture
may run across the installation, with up to five viewers sharing that source/run
and capture ID. Other concurrent captures are rejected as busy. A one-second
cooldown follows completion or cancellation. Results are not cached by the
manager or retained on disk. Image payloads travel in a bounded request response,
separately from status and topic event streams.

The reference snapshot profile uses an eight-second total capture deadline, 8 MiB serialized input
limit, 640 × 480 maximum output (aspect ratio preserved, no upscaling), JPEG quality
80 and 128 KiB encoded output limit. It rejects output exceeding that byte budget
rather than silently changing quality. Input supports `bgr8`, `rgb8` and `mono8`,
including row padding, with dimensions limited to 4096 per side and checked
stride/data length. These are processing and payload bounds, not an absolute DDS
memory cap: middleware can allocate incoming data before the raw-byte check.

Every capture starts a private native ROS/OpenCV helper. Subscription depth is
one, best effort, volatile. Deserialization, resizing and JPEG encoding occur
outside the manager's control thread and ordinary ROS worker. When the capture
finishes, times out or loses its last viewer, that helper and its subscription
are released. Cancellation can terminate this private helper; it never signals
the detector. The provider's image production stops only when its workload stops.
Starting a new helper includes ROS discovery, so a snapshot can take several
seconds even when the camera is already running.

The declared ROS interpreter must provide `sensor_msgs`, `rclpy`, NumPy and OpenCV
(`cv2`). These libraries are loaded by the image helper on request. Image support
does not add native imports to the web server or manager. An unavailable helper
reports an error; it cannot block lifecycle commands.

## Identity and freshness

Only the current fresh, owned, running unit without a Stop request may supply an
image. The manager checks the requested run, manager generation and profile
revision at acceptance and again before delivery. Required launch modes are
declared in the profile. A running cone-only detector therefore reports snapshots
unavailable even if an unrelated publisher appears at the configured topic.

Discovery must identify exactly one publisher of the declared type and exactly
one node with the declared name. Publisher GID is compared when the installed
ROS interface supplies it. Graph names and middleware identities do not
authenticate a process or prove it belongs to a cgroup; this shares the existing
localhost [ROS trust boundary](ros-operator-guide.md).

The image header must have the exact configured frame and a valid acquisition
timestamp in the host system clock, no more than 100 ms in the future and no
older than the configured expiry. Timestamps remain decimal nanosecond strings.
Acquisition age and bridge receipt age are separate and continue increasing
through delivery and display. A new HTTP response cannot freshen an old frame.
Invalid, stale, ambiguous or oversized images are rejected without a replacement
image. No TF transform, camera calibration or detection-accuracy claim is made.

## Configuration and interfaces

An available runtime unit may add up to three `launch_modes`, each containing
`id`, `label` and trusted `argv`. Its base `argv` remains ordinary Start. All
launch arguments are validated as installation inputs. CLI selection is
`robot-runtime start UNIT --mode MODE`; the browser Start body accepts optional
`mode`. Stop does not accept a mode. Browser callers cannot submit arguments or
executable paths. The selected mode and effective launch specification are stored
with the run, preserving ownership checks after manager recovery.

Add an optional `ros.snapshots` array. A reference declaration is:

```json
{
  "id": "oakd-annotated",
  "label": "Annotated cone detections",
  "unit_id": "oakd_cone_detector",
  "node": "/car/oakd_cone_detector",
  "name": "/car/debug/annotated_image",
  "type": "sensor_msgs/msg/Image",
  "frame_id": "oakd_rgb_camera_optical_frame",
  "acquisition_clock": "system",
  "required_mode": "images",
  "timeout_seconds": 8,
  "stale_after_seconds": 2,
  "max_serialized_bytes": 8388608,
  "max_width": 640,
  "max_height": 480,
  "max_jpeg_bytes": 131072,
  "jpeg_quality": 80
}
```

`GET /api/ros` exposes the declared sources and current availability. Public
`POST /api/ros/snapshots/ID` accepts only `expected_run`, `generation` (the manager
generation from `/api/status`) and `config_revision`. The configured HTTPS Host
and Origin are required. This read operation requires no passphrase or CSRF token;
Start still requires both command authentication and CSRF protection. Closing
the HTTP request cancels that viewer's demand. IPC action `ros_snapshot` uses the
same fields plus `capability_id` and protocol `version: 1`.

A successful JSON response includes `image_base64`, `mime_type: image/jpeg`,
dimensions, encoded byte count, original dimensions/encoding, acquisition
timestamp/clock/age, receipt age, capture ID and runtime identity. It stays within
the 512 KiB IPC response cap; the JPEG limit remains 128 KiB. Runtime ROS status exposes
`snapshot_capture` while a helper is active; ordinary topic `subscriptions`
remain listed separately. No automatic retries or persistent operation records
are needed for this read.

These additions retain profile, IPC and state schema version 1. Older profiles
continue to use ordinary Start. Downgrading requires a stopped maintenance window
and restoring a profile understood by the older package; saved run history stays
intact.
