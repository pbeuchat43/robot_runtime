# Native event recorder

This optional ROS package consumes the existing atomic RGB-D and overlay
interfaces. It does not open a camera or depend on a camera SDK. Deployment builds
it alongside the selected interface versions and supplies immutable recording
settings through the runtime preparation contract.

Build dependencies are ROS Jazzy, GStreamer development headers and nlohmann JSON
headers. Execution requires the Jetson hardware GStreamer H.264 encoder,
`nvvidconv`, `videoconvert`, `h264parse`, `splitmuxsink`, `mp4mux` and `filesink`.
The source distribution includes this package; the Python wheel does not contain
native binaries. The independently installed runtime remains usable without it.

Each selected output owns a two-frame input queue and a separate encoder/writer.
Input overload is counted before encoding. MP4 segments and optional JSONL frame
indices use source acquisition time; per-track frame numbers are independent.
Colourized depth is a visualization using a fixed recorded range, not metric
depth storage. Zero depth is black; positive values saturate at the range limits.

Completed segments are flushed before their index and segment-ledger entries are
published. An interrupted active segment may be unreadable. The manifest and
worker state remain nonterminal after a crash; runtime reports interruption once
the process exits. No automatic restart or in-place session continuation occurs.

The managed root is exclusively locked. Reclamation considers only complete
recorder-owned sessions with recognized regular files, oldest first. Active,
failed, interrupted, symlink-containing and unrelated groups are protected. A
budget or filesystem-reserve exhaustion finalizes and reports a storage stop.

Use the shared runtime CLI/browser Start and Stop policy. Direct invocation is
intended for isolated acceptance fixtures. Source identity and compatible geometry
are fixed for a session; a new generation resumes in new files. The recorder waits
through source loss and does not restart the camera.
