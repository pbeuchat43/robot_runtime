# Camera and gamepad integration

Optional video and diagnostic input are separately managed capabilities. This document describes their runtime ownership, authentication, reusable browser clients, and native dependency boundaries. Vehicle actuation remains a separate milestone.

## Runtime and ownership

Video and diagnostic input are separate explicitly started runtime units. Each
has a private Unix socket and the runtime's current run identity. The manager and
web adapter do not import camera SDKs or GStreamer. A page opening, connecting
runtime observations or inspecting capabilities never starts either workload.

An optional `av` profile section declares the service capabilities, their owning
units, private endpoints, bounded session/port settings and permitted video
sources. Physical sources belong to their video unit; profiles declare conflicts
with other units that can open the same device. An autonomy source instead reads
its declared ROS Image topic while its owning provider/run and required image
mode are current. It never opens or reconfigures the autonomy camera.

Each service has a single source owner. A ROS service can select among declared
image topics from that owner without restarting it. Separate capabilities can use
separate units, allowing the runtime to reserve only the relevant device. Viewers
share that source and an admitted compatible profile. Incompatible profiles are
rejected while sessions remain. The imported sender uses one encoder per viewer;
shared capture does not imply shared encoding. Session counts and UDP ports are
bounded. Last-viewer cleanup releases capture and encoding; the explicitly
started service remains visible until stopped through runtime lifecycle commands.

## Authentication and lifetime

The runtime's HTTPS API is the browser entry point. Session creation, renewal,
release and settings use its existing cookie, origin and CSRF checks. Every
session belongs to its authenticated browser session and the current service run.
Protected requests carry request identity, deadline and expected runtime identity.
Media signalling uses same-origin WSS with a bounded subscription token and
authentication; upstream signalling is private and cannot grant media outside
runtime admission. No browser-supplied executable, device path or ROS target is
accepted.

The web adapter maintains short authorization leases for its admitted sessions.
Logout, credential rotation, auth expiry, stale runtime identity and web failure
invalidate those sessions. Backend expiry runs independently of browser timers.
Input packets additionally retain their sequence/epoch checks and short receipt
expiry. Reconnection requires a new explicit input connection and enable action.
The input receiver produces diagnostics only and has no actuator/ROS publisher.

## Browser composition

The existing clients and views remain importable without starting work. The page
owns acquisition, explicit connections, authentication, visibility and teardown.
The shared HTTP client carries runtime authentication and reports uncertain POST
outcomes without replay. Video and input use separate WebRTC connections.
Video-only, input-only and combined pages exercise these boundaries. The reusable
input client also supports an explicit `fpv` mode which can require fresh stamped
video. The supplied combined page uses independent diagnostic input and observer
video; neither mode grants vehicle authority. Frame arrival, capture-age bounds
and input receipt age remain separate observations.

The supplied pages are `/video`, `/input` and `/camera-gamepad`. Unlock commands,
start the declared service, then connect its stream. A page never starts a service
for you. For F310/F710 use X mode, click the page, press a controller button so the
browser exposes it, then enable diagnostic input. Losing focus or fresh receipts
pauses input; focus returning does not re-enable it. Stopping the service also
disconnects its clients. Changing the selected capability destroys the previous
client/views; it does not start the newly selected service.

For a declared ROS source, `/video` also shows its owning workload's declared
independent selections, requested choice, current resolved run, and Start/Stop/
Restart controls. Select the camera/model combination before Start or use Restart
with selection for an existing run. Restart confirms the old run has stopped
before starting the new mode; uncertainty leaves it for operator inspection.
Video reconnection remains explicit. The processing panel subscribes to the
owner's declared scalar status capabilities. Receipt freshness, provider health,
acquisition age, and process ownership remain separate observations.

For a ROS service with declared image choices, **Video source** changes only the
media subscription. The provider and its processing continue uninterrupted.
The selection is shared by all viewers of that service. A change revokes all
video subscriptions, clears the previous capture, and requires explicit Connect
video. It does not restart the source or the video service. The choice remains
in memory for that video-service run; after an owner launch change, an available
choice is retained or the applicable default is selected. Unavailable choices
are disabled. This is independent of camera/model launch selections.

Input transmission samples the device at each send, independently of animation
frames. Controller painting is deferred and unchanged text is preserved to reduce
browser rendering work. The 250 ms input freshness bound and explicit re-enable
requirement remain unchanged.

Other pages can import `RuntimeAVApi`, `MediaClient`, `ControlClient`,
`GamepadInput` and the mount functions from `/assets/av/`. Pass the shared runtime
HTTP client to `new RuntimeAVApi({http, capability})`; pass that API to the media
or input client and mount its views. Keep the owning objects, release acquired
subscriptions, destroy views and dispose clients on teardown. See `page.js` for
the supplied composition. Construction is inert; `connect()` is explicit.

## Native installation and declarations

The wheel contains Python/JavaScript, policy defaults and local browser assets.
Native dependencies are separately provisioned: GStreamer 1.24.2, the tested
gst-plugins-rs WebRTC/SRTP 0.14.5 build, libnice, matching GI overrides, NumPy,
and the chosen encoder. OAK capture additionally uses DepthAI 3.9.0; ROS Image
capture uses the provisioned ROS 2 environment and OpenCV. Runtime-only installs
do not load these libraries. No runtime download or Internet STUN/TURN is used.

`deployment/prepare-av-native.py` copies an explicitly supplied validated native
build and optional SDK into a new independent prefix and records per-file SHA256
and licenses. It is a target-host artifact preparation helper, not a cross-platform
installer or a substitute for building/provisioning those dependencies. The
running services use this copied prefix, never the prototype checkout. The
imports' pre-adaptation identities are in `camera-gamepad-source-sha256.json`; third-party
browser license notices are packaged in `static/av/vendor/`.

Declare one runtime unit per capability with an absolute installed interpreter
and arguments `-m robot_runtime.av.worker --profile /absolute/profile.json
--state /absolute/private-state --capability video`. Configure its environment
with `GST_PLUGIN_PATH` pointing to the prefix's `gstreamer/`, a writable private
`GST_REGISTRY`, `ROBOT_AV_GI_OVERRIDES` pointing to `gi/`, and, if used, the copied
SDK's `python/` on `PYTHONPATH`. Keep the unit's graceful SIGINT/SIGTERM policy.
`tests/av_fixture.py` is an executable example of both unit declarations.

The matching optional profile section looks like:

```json
{"av":{"services":[
  {"id":"video","label":"Camera","kind":"media","unit_id":"video",
   "host":"192.0.2.10","udp_port":41400,"max_sessions":5,
   "encoder":"hardware","source":{"kind":"oak","label":"RGB","serial":"EXACTSERIAL"}},
  {"id":"input","label":"Gamepad receipts","kind":"input","unit_id":"input",
   "host":"192.0.2.10","udp_port":41410,"max_sessions":5}
]}}
```

Replace addresses, paths and identifiers for the target installation. Each service
reserves its base UDP port through `base + max_sessions - 1`. Allow those local
UDP ranges alongside the existing HTTPS port. Declare mutual unit conflicts with
every other runtime unit that opens the same physical camera. This contract
coordinates configured runtime units; it cannot reserve a device against
unmanaged programs. A V4L2 source uses a stable `/dev/v4l/by-id/` or `by-path/`
path. A ROS source instead declares `owner_unit`, `domain_id` and optional
`required_mode`; it must not conflict with that owner. It specifies either one
`topic` (the existing form) or `choices` plus `default_choice`. The latter contains
one to eight unique `{id, label, topic}` entries. Optional `active_modes` lists
the installed launch IDs of that owner in which a choice is available; omitting
it makes that choice available in every launch. At least one choice must cover
each supported launch. All choices share the source owner and ROS domain.
The browser submits only a declared ID, never an arbitrary ROS name.
Its sessions expire when
the owning run or required mode changes. A `test` source needs only kind and label.

Choice-enabled media configuration includes `source_selection` with `id`,
`revision` and the labelled choices with availability. The `camera` operation
requires `{id, source_revision}`; media admission also supplies the observed
`source_revision`. Stale requests fail before changing the source or admitting
a session. This revision changes with selection or owner-run binding, separately
from encoding settings and capture activity. The legacy single-topic form and
its no-op same-source camera request remain supported. Choice-enabled profiles
require runtime 0.1.0.dev21 or later; older runtime versions reject these fields.

ROS image conversion fits the complete source image inside the encoded canvas,
preserving aspect ratio with black padding when shapes differ. This keeps boxes
already rendered on a square or rectangular source in proportion. Encoded
dimensions describe the canvas, including padding; they do not redefine camera
intrinsics or detector source-image coordinates.
Conversion admission advances a rate deadline without resetting it on every
arrival, retaining ordinary source jitter without accumulating missed intervals.
It allows at most one frame of burst credit after a long gap.

The observer profile defaults to 640×360 at 5 FPS; the separate stamped teleop
profile is 640×360 at 30 FPS. The shared settings must be changed with all viewers
disconnected. The supplied pages select observer; another composition may
explicitly request teleop. Five admitted sessions is a bound, not five-video
throughput acceptance. Software encoding is an explicit `encoder: "software"`
choice and needs the corresponding `x264enc` plugin.

## Verification

Unit tests cover admission, sessions, signaling, and input freshness. Packaged Chromium checks use synthetic video and controller packets with prepared native dependencies. They do not establish physical input, camera support, or driving acceptance. See the [test runbook](../tests/README.md#camera-and-diagnostic-gamepad).
