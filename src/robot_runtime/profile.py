"""Versioned, bounded launch profiles. Browser callers cannot supply these."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import hashlib
import math
import os
import re
from pathlib import Path

from .common import Fault, dumps, fields, loads, text

IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise Fault("invalid_profile", "IDs must start with a lowercase letter and contain up to 32 ASCII letters, digits, '_' or '-'")
    return value


def absolute(value):
    text(value, 4096)
    if not value.startswith("/") or ".." in Path(value).parts:
        raise Fault("invalid_profile", "Launch paths must be absolute and normalized")
    return value


def launch_argv(argv):
    if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
        raise Fault("invalid_profile", "Provide 1 to 64 argv entries")
    for arg in argv:
        text(arg, 4096, empty=True)
    absolute(argv[0])


def public_mode(mode):
    return {key: mode[key] for key in ("id", "label", "selections") if key in mode} if mode else None


def select_launch(unit, mode_id=None):
    """Resolve only installed choices; shared by preflight and Start."""
    if not unit.get("available", True):
        raise Fault("unit_unavailable", unit["unavailable_reason"])
    mode_id = mode_id if mode_id is not None else unit.get("default_launch_mode")
    mode = next((item for item in unit.get("launch_modes", []) if item["id"] == mode_id), None)
    if mode_id is not None and (not isinstance(mode_id, str) or mode is None):
        raise Fault("invalid_request", "Unknown launch mode")
    spec = dict(unit, argv=mode["argv"]) if mode else dict(unit)
    check_launch(spec)
    return spec, mode


def validate_selections(unit, modes):
    selections = unit.get("launch_selections")
    if selections is None:
        if "default_launch_mode" in unit or any("selections" in mode for mode in modes):
            raise Fault("invalid_profile", "Mode selections require declared launch selections")
        return
    if not isinstance(selections, list) or not 1 <= len(selections) <= 3:
        raise Fault("invalid_profile", "Declare one to three independent launch selections")
    choices = {}
    for selection in selections:
        fields(selection, ("id", "label", "choices"))
        key = identifier(selection["id"])
        text(selection["label"], 80)
        if key in choices or not isinstance(selection["choices"], list) or not 1 <= len(selection["choices"]) <= 8:
            raise Fault("invalid_profile", "Selection IDs must be unique with one to eight choices")
        allowed = set()
        for choice in selection["choices"]:
            fields(choice, ("id", "label"))
            identity = identifier(choice["id"])
            text(choice["label"], 80)
            if identity in allowed:
                raise Fault("invalid_profile", "Duplicate selection choice")
            allowed.add(identity)
        choices[key] = allowed
    combinations = set()
    for mode in modes:
        selected = mode.get("selections")
        if not isinstance(selected, dict) or set(selected) != set(choices):
            raise Fault("invalid_profile", "Each launch mode must resolve every selection")
        if any(not isinstance(value, str) or value not in choices[key] for key, value in selected.items()):
            raise Fault("invalid_profile", "Unknown launch selection choice")
        combination = tuple(sorted(selected.items()))
        if combination in combinations:
            raise Fault("invalid_profile", "Ambiguous launch selection combination")
        combinations.add(combination)
    default = next((mode for mode in modes if mode["id"] == unit.get("default_launch_mode")), None)
    if default is None or default["argv"] != unit["argv"]:
        raise Fault("invalid_profile", "Selected launches require an explicit default matching the base argv")


def validate(value, *, check_paths=True):
    fields(value, ("schema_version", "robot_id", "namespace", "units"), ("ros", "av"))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Fault("invalid_profile", "Unsupported profile schema")
    identifier(value["robot_id"])
    identifier(value["namespace"])
    if not isinstance(value["units"], list) or not 1 <= len(value["units"]) <= 20:
        raise Fault("invalid_profile", "Provide between 1 and 20 runtime units")
    units = {}
    for source in value["units"]:
        if not isinstance(source, dict) or type(source.get("available", True)) is not bool:
            raise Fault("invalid_profile", "Unit availability must be boolean")
        if not source.get("available", True):
            fields(source, ("id", "label", "available", "unavailable_reason"))
            text(source["unavailable_reason"], 512)
        else:
            fields(source, ("id", "label", "argv", "working_directory", "environment", "stop"),
                   ("requires_running", "public_logs", "available", "launch_modes", "conflicts", "launch_selections", "default_launch_mode"))
        unit = dict(source)
        identifier(unit["id"])
        if unit["id"] in units:
            raise Fault("invalid_profile", "Duplicate unit ID")
        text(unit["label"], 120)
        if not unit.get("available", True):
            units[unit["id"]] = unit
            continue
        launch_argv(unit["argv"])
        modes = unit.get("launch_modes", [])
        if not isinstance(modes, list) or len(modes) > 8:
            raise Fault("invalid_profile", "Declare at most eight launch modes")
        seen = set()
        for mode in modes:
            fields(mode, ("id", "label", "argv"), ("selections",))
            identifier(mode["id"])
            text(mode["label"], 80)
            if mode["id"] in seen:
                raise Fault("invalid_profile", "Duplicate launch mode")
            seen.add(mode["id"])
            launch_argv(mode["argv"])
        validate_selections(unit, modes)
        absolute(unit["working_directory"])
        env = unit["environment"]
        if not isinstance(env, dict) or len(env) > 64:
            raise Fault("invalid_profile", "Environment must contain at most 64 entries")
        for key, item in env.items():
            if not isinstance(key, str) or not ENVIRONMENT.fullmatch(key) or key.startswith("ROBOT_RUNTIME_"):
                raise Fault("invalid_profile", "Invalid or reserved environment name")
            text(item, 4096, empty=True)
        fields(unit["stop"], ("signal", "timeout_seconds"))
        timeout = unit["stop"]["timeout_seconds"]
        if unit["stop"]["signal"] not in ("SIGINT", "SIGTERM") or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 120:
            raise Fault("invalid_profile", "Stop requires SIGINT/SIGTERM and a finite 0.1–120 second deadline")
        unit.setdefault("requires_running", [])
        unit.setdefault("public_logs", False)
        if type(unit["public_logs"]) is not bool or not isinstance(unit["requires_running"], list):
            raise Fault("invalid_profile", "Invalid log exposure or prerequisites")
        for dependency in unit["requires_running"]:
            identifier(dependency)
        if len(unit["requires_running"]) > 19 or len(set(unit["requires_running"])) != len(unit["requires_running"]):
            raise Fault("invalid_profile", "Duplicate or excessive prerequisites")
        conflicts = unit.get("conflicts", [])
        if not isinstance(conflicts, list) or len(conflicts) > 19:
            raise Fault("invalid_profile", "Declare at most 19 conflicting units")
        for other in conflicts:
            identifier(other)
        if len(set(conflicts)) != len(conflicts) or unit["id"] in conflicts:
            raise Fault("invalid_profile", "Conflicts must be unique other units")
        if check_paths:
            check_launch(unit)
            for mode in modes:
                check_launch(dict(unit, argv=mode["argv"]))
        units[unit["id"]] = unit
    def visit(name, trail):
        if name not in units or name in trail:
            raise Fault("invalid_profile", "Missing or cyclic prerequisite")
        for dependency in units[name].get("requires_running", []):
            visit(dependency, trail | {name})
            if not units[dependency].get("available", True):
                raise Fault("invalid_profile", "An available unit cannot require an unavailable unit")
    for name in units:
        visit(name, set())
        for other in units[name].get("conflicts", []):
            if other not in units or not units[other].get("available", True) or name not in units[other].get("conflicts", []):
                raise Fault("invalid_profile", "Conflicts must name available units and be declared in both directions")
            if other in units[name].get("requires_running", []):
                raise Fault("invalid_profile", "A prerequisite cannot also conflict with the unit")
    result = dict(value, units=list(units.values()))
    if "ros" in result:
        from .ros_profile import validate as validate_ros
        result["ros"] = validate_ros(result["ros"], set(units), check_paths=check_paths)
        for topic in result["ros"]["topics"]:
            declared = {mode["id"] for mode in units[topic["unit_id"]].get("launch_modes", [])}
            if not set(topic.get("active_modes", [])) <= declared:
                raise Fault("invalid_ros_profile", "Topic active modes must name installed launches of its owner")
        for snapshot in result["ros"].get("snapshots", []):
            mode = snapshot.get("required_mode")
            if mode and mode not in {item["id"] for item in units[snapshot["unit_id"]].get("launch_modes", [])}:
                raise Fault("invalid_profile", "Snapshot requires an undeclared launch mode")
    if "av" in result:
        from .av_profile import validate as validate_av
        result["av"] = validate_av(result["av"], units)
    return result, hashlib.sha256(dumps(result).encode()).hexdigest()


def check_launch(unit):
    executable = Path(unit["argv"][0])
    directory = Path(unit["working_directory"])
    if not executable.is_file() or not os.access(executable, os.X_OK) or not directory.is_dir() or not os.access(directory, os.X_OK):
        raise Fault("invalid_profile", "Executable or working directory is unavailable", unit_id=unit["id"])


def read(path):
    path = Path(path)
    if path.stat().st_size > 262144:
        raise Fault("invalid_profile", "Profile exceeds 256 KiB")
    return validate(loads(path.read_bytes()))
