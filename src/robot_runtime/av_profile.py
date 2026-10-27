"""Optional video/input capabilities and run-bound private service discovery."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import ipaddress
from pathlib import Path

from .common import Fault, fields, text
from .profile import absolute, identifier


def validate(value, units):
    fields(value, ("services",))
    services = value["services"]
    if not isinstance(services, list) or not 1 <= len(services) <= 8:
        raise Fault("invalid_profile", "Declare one to eight video/input services")
    identities, owners, ports, devices = set(), set(), set(), {}
    result = []
    for source in services:
        fields(source, ("id", "label", "kind", "unit_id", "host", "udp_port"),
               ("max_sessions", "source", "profiles", "encoder"))
        item = dict(source)
        identity, owner = identifier(item["id"]), identifier(item["unit_id"])
        text(item["label"], 120)
        if identity in identities or owner in owners or owner not in units or not units[owner].get("available", True):
            raise Fault("invalid_profile", "AV services require unique IDs and available owning units")
        if item["kind"] not in ("media", "input"):
            raise Fault("invalid_profile", "AV service kind must be media or input")
        try:
            address = ipaddress.IPv4Address(item["host"])
            if address.is_unspecified or address.is_multicast:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise Fault("invalid_profile", "AV host must be a specific local IPv4 address") from exc
        maximum = item.setdefault("max_sessions", 5)
        port = item["udp_port"]
        if type(maximum) is not int or not 1 <= maximum <= 5 or type(port) is not int or not 1024 <= port <= 65536 - maximum:
            raise Fault("invalid_profile", "AV requires 1–5 sessions and a valid UDP port range")
        allocated = {(str(address), number) for number in range(port, port + maximum)}
        if allocated & ports:
            raise Fault("invalid_profile", "AV service UDP ranges must not overlap")
        ports.update(allocated)
        identities.add(identity)
        owners.add(owner)
        if item["kind"] == "input":
            if set(item) & {"source", "profiles", "encoder"}:
                raise Fault("invalid_profile", "Input services have no camera or encoder configuration")
        else:
            if "source" not in item:
                raise Fault("invalid_profile", "Media requires a declared source")
            if not isinstance(item["source"], dict):
                raise Fault("invalid_profile", "Media source must be an object")
            camera = item["source"] = dict(item["source"])
            kind = camera.get("kind")
            required = {"test": (), "oak": ("serial",), "v4l2": ("path",),
                        "ros": ("owner_unit", "domain_id")}.get(kind)
            if required is None:
                raise Fault("invalid_profile", "Unknown media source kind")
            fields(camera, ("kind", "label", *required), ("required_mode", "topic", "choices", "default_choice") if kind == "ros" else ())
            text(camera["label"], 120)
            if kind == "oak":
                serial = text(camera["serial"], 64)
                if not serial.isalnum():
                    raise Fault("invalid_profile", "OAK source requires its exact device serial")
            if kind == "v4l2":
                path = absolute(camera["path"])
                if not path.startswith(("/dev/v4l/by-id/", "/dev/v4l/by-path/")):
                    raise Fault("invalid_profile", "V4L2 sources require a stable /dev/v4l path")
            if kind in ("oak", "v4l2"):
                device = (kind, camera["serial" if kind == "oak" else "path"])
                for previous in devices.get(device, []):
                    if previous not in units[owner].get("conflicts", []) or owner not in units[previous].get("conflicts", []):
                        raise Fault("invalid_profile", "Services opening the same physical camera require mutual unit conflicts")
                devices.setdefault(device, []).append(owner)
            if kind == "ros":
                provider = camera["owner_unit"]
                if provider == owner or provider not in units or not units[provider].get("available", True):
                    raise Fault("invalid_profile", "ROS video requires an available separate owner")
                if provider in units[owner].get("conflicts", []):
                    raise Fault("invalid_profile", "ROS video cannot conflict with its source owner")
                if ("topic" in camera) == ("choices" in camera):
                    raise Fault("invalid_profile", "Declare a ROS topic or a list of topic choices")
                declared_modes = {m["id"] for m in units[provider].get("launch_modes", [])}
                choices = camera.get("choices", [dict(topic=camera.get("topic"))])
                if "choices" in camera:
                    if not isinstance(choices, list) or not 1 <= len(choices) <= 8:
                        raise Fault("invalid_profile", "Declare one to eight ROS video choices")
                    ids = set()
                    for choice in choices:
                        fields(choice, ("id", "label", "topic"), ("active_modes",))
                        key = identifier(choice["id"])
                        text(choice["label"], 120)
                        if key in ids:
                            raise Fault("invalid_profile", "Video choice IDs must be unique")
                        ids.add(key)
                        if "active_modes" in choice:
                            modes = choice["active_modes"]
                            if (not isinstance(modes, list) or not 1 <= len(modes) <= 8 or
                                    any(not isinstance(m, str) for m in modes) or
                                    len(set(modes)) != len(modes) or not set(modes) <= declared_modes):
                                raise Fault("invalid_profile", "Video choices require unique installed owner launch modes")
                    if identifier(camera.get("default_choice")) not in ids:
                        raise Fault("invalid_profile", "Select a declared default video choice")
                    if declared_modes and any(not any("active_modes" not in c or mode in c["active_modes"] for c in choices)
                                              for mode in declared_modes if not camera.get("required_mode") or mode == camera["required_mode"]):
                        raise Fault("invalid_profile", "Every supported owner launch needs a video choice")
                elif "default_choice" in camera:
                    raise Fault("invalid_profile", "A default choice requires ROS video choices")
                for choice in choices:
                    topic = text(choice["topic"], 240)
                    if not topic.startswith("/") or "//" in topic or not all(c.isalnum() or c in "_/" for c in topic):
                        raise Fault("invalid_profile", "Video topic must be an absolute ROS name")
                if type(camera["domain_id"]) is not int or not 0 <= camera["domain_id"] <= 232:
                    raise Fault("invalid_profile", "Invalid video ROS domain")
                if camera.get("required_mode") and camera["required_mode"] not in {m["id"] for m in units[provider].get("launch_modes", [])}:
                    raise Fault("invalid_profile", "Video requires an undeclared source launch mode")
            item.setdefault("encoder", "hardware")
            if item["encoder"] not in ("hardware", "software"):
                raise Fault("invalid_profile", "Select a hardware or software video encoder explicitly")
            profiles = item.setdefault("profiles", {})
            if not isinstance(profiles, dict) or set(profiles) - {"observer", "teleop"}:
                raise Fault("invalid_profile", "Unknown media profile")
            from .av.settings_config import load_profile
            from .av.stream_settings import FIELDS
            for name in ("observer", "teleop"):
                overrides = profiles.get(name, {})
                if not isinstance(overrides, dict) or set(overrides) - set(FIELDS):
                    raise Fault("invalid_profile", "Media profile overrides are bounded encode settings")
                try:
                    policy = load_profile(name, overrides=overrides)
                    if policy["width"] > 1920 or policy["height"] > 1080 or policy["max_bitrate"] > 20_000_000:
                        raise ValueError("Media is bounded to 1920×1080 and 20 Mbit/s per viewer")
                except ValueError as exc:
                    raise Fault("invalid_profile", str(exc)) from exc
        result.append(item)
    return {"services": result}


def socket_path(state, capability, run):
    # Per-run names prevent a new owner from adopting a predecessor's endpoint.
    return Path(state) / ("av-" + capability + "-" + run[:12] + ".sock")


def current_target(core, capability, *, fresh=False):
    service = next((s for s in core.profile.get("av", {}).get("services", []) if s["id"] == capability), None)
    if service is None:
        raise Fault("not_found", "Unknown video/input capability")
    if fresh:
        core.audit(full=True)
    status = core.status()
    unit = next(u for u in status["units"] if u["unit_id"] == service["unit_id"])
    if not status["fresh"] or unit["lifecycle"] != "running" or unit["ownership"] != "owned" or unit.get("stop_request_id"):
        raise Fault("av_unavailable", "Start the declared video/input service before connecting")
    source_run = source_mode = None
    source = service.get("source", {})
    if source.get("kind") == "ros":
        provider = next(u for u in status["units"] if u["unit_id"] == source["owner_unit"])
        mode = provider.get("launch_mode") or {}
        if (provider["lifecycle"] != "running" or provider["ownership"] != "owned" or provider.get("stop_request_id")
                or (source.get("required_mode") and mode.get("id") != source["required_mode"])):
            raise Fault("av_unavailable", "The configured camera owner and image mode must be running")
        source_run = provider["run_id"]
        source_mode = mode.get("id")
    return dict(id=service["id"], kind=service["kind"], unit_id=service["unit_id"], run_id=unit["run_id"],
                config_revision=core.revision, generation=core.generation, source_run=source_run, source_mode=source_mode,
                socket=str(socket_path(core.store.directory, capability, unit["run_id"])))


def catalog(core):
    services = []
    for service in core.profile.get("av", {}).get("services", []):
        value = {k: service[k] for k in ("id", "label", "kind", "unit_id", "max_sessions")}
        value.update(actuators_enabled=False, available=False, run_id=None, reason=None)
        if service["kind"] == "media":
            value["source"] = {k: v for k, v in service["source"].items() if k in ("kind", "label", "owner_unit", "required_mode")}
        try:
            target = current_target(core, service["id"])
            value.update(available=True, run_id=target["run_id"], source_run=target["source_run"])
        except Fault as exc:
            value["reason"] = exc.message
        services.append(value)
    return dict(api_version=1, config_revision=core.revision, generation=core.generation, services=services)
