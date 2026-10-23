"""Systemd user-manager adapter. All calls belong to the GLib manager thread."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import os
import signal
import time
from pathlib import Path

import dbus

from .common import Fault

DEST = "org.freedesktop.systemd1"
MANAGER = DEST + ".Manager"
UNIT = DEST + ".Unit"
SERVICE = DEST + ".Service"
PROPS = "org.freedesktop.DBus.Properties"
CGROOT = Path("/sys/fs/cgroup")


def plain(value):
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, float):
        return float(value)
    return int(value)


def members(cgroup):
    if not cgroup:
        return []
    directory = (CGROOT / cgroup.lstrip("/")).resolve()
    if directory == CGROOT or not directory.is_relative_to(CGROOT):
        raise Fault("supervisor_unavailable", "Invalid supervisor cgroup path")
    if not directory.exists():
        return []
    result = set()
    for path in [directory / "cgroup.procs", *directory.glob("**/cgroup.procs")]:
        try:
            result.update(int(item) for item in path.read_text().split())
        except FileNotFoundError:
            pass
    return sorted(result)


def launch_argv(run):
    # Clear the user manager's inherited environment, including ROS settings.
    # env execs the target in place; Type=exec acknowledges this trusted wrapper.
    env = dict(run["spec"]["environment"], ROBOT_RUNTIME_RUN=run["run_id"])
    return ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in sorted(env.items())], *run["spec"]["argv"]]


def expected_properties(run):
    return dict(Description="robot-runtime:" + run["run_id"], Type="exec", ExitType="cgroup",
                Restart="no", RemainAfterExit=False, KillMode="control-group",
                KillSignal=int(getattr(signal, run["spec"]["stop"]["signal"])),
                SendSIGKILL=False, SendSIGHUP=False, TimeoutStopUSec=2_000_000,
                TimeoutStopFailureMode="terminate", OOMPolicy="continue", Delegate=False,
                WorkingDirectory=run["spec"]["working_directory"])


def owns(run, observed):
    if not observed or observed.get("Id") != run["name"] or not observed.get("Transient"):
        return False
    if run.get("invocation") and run["invocation"] != observed["InvocationID"]:
        return False
    if any(observed.get(k) != v for k, v in expected_properties(run).items()):
        return False
    start = observed.get("ExecStart", [])
    return (len(start) == 1 and start[0][0] == "/usr/bin/env" and start[0][1] == launch_argv(run)
            and observed.get("Environment") == ["ROBOT_RUNTIME_RUN=" + run["run_id"]])


class Systemd:
    def __init__(self, prefix, changed, disconnected):
        if not (CGROOT / "cgroup.controllers").is_file():
            raise Fault("unsupported_host", "cgroup v2 is required")
        self.prefix = prefix
        self.changed = changed
        self.bus = dbus.bus.BusConnection(f"unix:path=/run/user/{os.getuid()}/bus")
        self.bus.call_on_disconnection(lambda _bus: disconnected())
        self.manager = dbus.Interface(self.bus.get_object(DEST, "/org/freedesktop/systemd1", introspect=False), MANAGER)
        self.proxies = {}
        self.paths = {}
        self.cache = {}
        self.refs = set()
        self.bus.add_signal_receiver(self._new, signal_name="UnitNew", dbus_interface=MANAGER, bus_name=DEST)
        self.bus.add_signal_receiver(self._removed, signal_name="UnitRemoved", dbus_interface=MANAGER, bus_name=DEST)
        self.bus.add_signal_receiver(self._properties, signal_name="PropertiesChanged", dbus_interface=PROPS,
                                     bus_name=DEST, path_keyword="path")
        self.bus.add_signal_receiver(lambda *_args: disconnected(), signal_name="NameOwnerChanged",
                                     dbus_interface="org.freedesktop.DBus", arg0=DEST)
        self.manager.Subscribe(timeout=2)

    def close(self):
        self.bus.close()

    def _new(self, name, path):
        if str(name).startswith(self.prefix):
            self.paths[str(path)] = str(name)
            self.changed(str(name))

    def _removed(self, name, path):
        self.paths.pop(str(path), None)
        if str(name).startswith(self.prefix):
            self.changed(str(name))

    def _properties(self, _interface, _values, _invalidated, path=None):
        name = self.paths.get(str(path))
        if name:
            self.changed(name)

    def names(self):
        result = []
        for row in self.manager.ListUnits(timeout=2):
            if str(row[0]).startswith(self.prefix):
                result.append(str(row[0]))
                self.paths[str(row[6])] = str(row[0])
        if len(result) > 64:
            raise Fault("ownership_conflict", "Runtime namespace exceeds 64 services")
        return sorted(result)

    def _properties_for(self, name):
        if name not in self.proxies:
            path = self.manager.GetUnit(name, timeout=2)
            self.paths[str(path)] = name
            self.proxies[name] = dbus.Interface(self.bus.get_object(DEST, path, introspect=False), PROPS)
        return self.proxies[name]

    def snapshot(self, name, *, full=True):
        try:
            proxy = self._properties_for(name)
            unit = proxy.GetAll(UNIT, timeout=2)
            if str(unit["LoadState"]) == "not-found":
                self.forget(name)
                return None
            previous = self.cache.get(name) if not full else None
            same = previous and bytes(unit["InvocationID"]).hex() == previous["InvocationID"]
            same = same and all(plain(unit[k]) == previous[k] for k in ("ActiveState", "SubState"))
            if same:
                same = (int(proxy.Get(SERVICE, "MainPID", timeout=2)) == previous["MainPID"]
                        and str(proxy.Get(SERVICE, "Result", timeout=2)) == previous["Result"])
            if same:
                current = dict(previous, Job=plain(unit["Job"]), members=members(previous["ControlGroup"]),
                               observed_monotonic=time.monotonic())
                self.cache[name] = current
                return current
            service = proxy.GetAll(SERVICE, timeout=2)
        except dbus.DBusException as exc:
            if exc.get_dbus_name() in (DEST + ".NoSuchUnit", "org.freedesktop.DBus.Error.UnknownObject"):
                self.forget(name)
                return None
            raise Fault("supervisor_unavailable", "Cannot read systemd state") from exc
        keys = ("Id", "Description", "LoadState", "ActiveState", "SubState", "Transient", "Job")
        service_keys = ("Type", "ExitType", "Restart", "NRestarts", "MainPID", "ExecMainPID", "ExecMainCode",
                        "ExecMainStatus", "Result", "ControlGroup", "ExecStart", "Environment", "KillMode",
                        "KillSignal", "SendSIGKILL", "SendSIGHUP", "TimeoutStopUSec", "TimeoutStopFailureMode",
                        "OOMPolicy", "Delegate", "RemainAfterExit", "WorkingDirectory")
        result = {k: plain(unit[k]) for k in keys}
        result.update({k: plain(service[k]) for k in service_keys})
        result.update(InvocationID=bytes(unit["InvocationID"]).hex(), members=members(result["ControlGroup"]),
                      observed_monotonic=time.monotonic(), properties_observed_monotonic=time.monotonic())
        self.cache[name] = result
        return result

    def start(self, run):
        values = expected_properties(run)
        values.update(KillSignal=dbus.Int32(values["KillSignal"]), TimeoutStopUSec=dbus.UInt64(values["TimeoutStopUSec"]),
                      CPUAccounting=True, MemoryAccounting=True, AddRef=True,
                      Environment=dbus.Array(["ROBOT_RUNTIME_RUN=" + run["run_id"]], signature="s"))
        argv = launch_argv(run)
        values["ExecStart"] = dbus.Array([dbus.Struct((argv[0], dbus.Array(argv, signature="s"), False), signature="sasb")], signature="(sasb)")
        self.manager.StartTransientUnit(run["name"], "fail", dbus.Array(list(values.items()), signature="(sv)"),
                                        dbus.Array([], signature="(sa(sv))"), timeout=3)
        self.refs.add(run["name"])

    def retain(self, name):
        if name not in self.refs:
            self.manager.RefUnit(name, timeout=2)
            self.refs.add(name)

    def release(self, name, *, failed=False):
        if failed:
            self.manager.ResetFailedUnit(name, timeout=2)
        if name in self.refs:
            self.manager.UnrefUnit(name, timeout=2)
            self.refs.remove(name)

    def forget(self, name):
        self.proxies.pop(name, None)
        self.cache.pop(name, None)

    def signal(self, run):
        self.manager.KillUnit(run["name"], "all", dbus.Int32(getattr(signal, run["spec"]["stop"]["signal"])), timeout=3)
