# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Private GStreamer setup shared by independent media and control services."""
import os

import gi
import gi.overrides
# Native dependencies are provisioned independently. The unit environment supplies
# GST_PLUGIN_PATH/GST_REGISTRY and, when needed, a private PyGObject override path.
if os.environ.get('ROBOT_AV_GI_OVERRIDES'):
    gi.overrides.__path__.insert(0, os.environ['ROBOT_AV_GI_OVERRIDES'])
for namespace in ['Gst', 'GstWebRTC', 'GstSdp']:
    gi.require_version(namespace, '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib
Gst.init(None)


def restrict_ice(rtc, host, port):
    ice = rtc.get_property('ice-agent')
    # GStreamer 1.24.2 leaves webrtcbin's default ICE GstObject floating.
    # PyGObject sinks that reference when wrapping this property, leaving one
    # native ref shared by two owners. Restore webrtcbin's missing ref only
    # when the live parent + Python wrapper demonstrably have just one.
    # Repeated calls and runtimes with correct ownership already have >= 2.
    # See runtime/check_ice_native.c and test_gst_runtime.py; upstream source:
    # https://github.com/GStreamer/gstreamer/blob/1.24.2/subprojects/gst-plugins-bad/ext/webrtc/gstwebrtcbin.c#L7890
    if ice.__grefcount__ == 1:
        ice._ref()
    ice.set_property('min-rtp-port', port)
    ice.set_property('max-rtp-port', port)
    ice.set_property('ice-tcp', False)
    agent = ice.get_property('agent')
    agent.set_property('upnp', False)
    if not ice.emit('add-local-ip-address', host):
        raise RuntimeError('Could not restrict ICE to the configured interface')
    return ice, agent  # Retain wrappers until the owning peer is removed.
