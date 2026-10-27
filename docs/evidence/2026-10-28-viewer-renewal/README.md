# Bounded media renewal recovery

Runtime 0.1.0.dev22 retains the existing media subscription after a transient
network renewal failure. It retries after one second, with a request timeout of
at most three seconds and a deadline bounded by the latest confirmed lease.
It never automatically requests a replacement subscription. Expiry, authorization
rejection, runtime replacement and source changes still require explicit Connect.
An obsolete renewal completion cannot alter a newer connection.

The [software report](software.json) records 147 passing unit tests and 22 passing
packaged Chromium checks on reserved Jah. The new cases cover transient recovery,
persistent failure, suspension beyond expiry, bounded request duration, authority
rejection and obsolete success/failure callbacks. An initial fixture omitted its
signalling URL; that failure was retained and the corrected fixture passed.

Wheel and sdist were built. All 100 installed runtime package files matched the
candidate source; 1,038 native/model/composition files matched the prior release.
No native camera, detector, model or input-control behavior changed.

The composition repository records the live OAK-D/COCO comparison. On dev21,
one injected renewal failure removed media demand while inference continued.
On dev22, a two-second injected failure retained the same token and transport;
sustained failures expired the subscription and explicit Connect restored video.
Both runs included a two-minute uninterrupted baseline and clean isolated cleanup.
The original operator-observed interruption's exact trigger remains unproven;
these tests do not measure Wi-Fi interference or physical display scanout.
