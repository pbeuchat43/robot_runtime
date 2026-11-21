"""Terminal presentation only; structured payloads belong to their producers."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
from datetime import datetime, timedelta, timezone
import json
import sys


def children(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from dict.fromkeys(action.choices.values())


class ArgumentParser(argparse.ArgumentParser):
    """Keep argparse's exit code, with an envelope for explicit JSON requests."""
    json_errors = False

    def parse_args(self, args=None, namespace=None):
        argv = list(sys.argv[1:] if args is None else args)
        def configure(parser):
            parser.json_errors = "--json" in argv
            for child in children(parser):
                configure(child)
        configure(self)
        return super().parse_args(argv, namespace)

    def error(self, message):
        if not self.json_errors:
            return super().error(message)
        emit(dict(error=dict(code="invalid_arguments", message=message)), structured=True,
             error=True, file=sys.stderr)
        self.exit(2)


def label(key):
    return str(key).replace("_", " ").capitalize()


def scalar(value):
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    # Escape terminal control characters, including embedded newlines in tables.
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in str(value))


def simple(value):
    return not isinstance(value, (dict, list, tuple))


def status_summary(value):
    """Keep current observations; static mode definitions belong in catalog."""
    result = {key: item for key, item in value.items()
              if key not in ("api_version", "boot_id", "observed_monotonic", "revision")}
    if isinstance(result.get("recording"), dict) and "overlays_by_mode" in result["recording"]:
        result.pop("recording")  # Configuration, not the recorder's observation.
    result["units"] = []
    for unit in value["units"]:
        row = {key: item for key, item in unit.items()
               if key not in ("launch_modes", "launch_selections", "default_launch_mode", "public_logs")}
        if isinstance(row.get("launch_mode"), dict):
            row["launch_mode"] = row["launch_mode"]["id"]
        result["units"].append(row)
    ordered = {key: result.pop(key) for key in ("robot_id", "fresh", "age_seconds", "error", "conflicts", "units") if key in result}
    return dict(ordered, **result)


def readable(value, indent=0):
    """Render observations with tables; status omits repeated static definitions."""
    pad = " " * indent
    if isinstance(value, dict):
        if isinstance(value.get("units"), list) and any(isinstance(unit, dict) and "lifecycle" in unit for unit in value["units"]):
            value = status_summary(value)
        if not value:
            return [pad + "(none)"]
        lines = []
        for key, item in value.items():
            if key == "history" and isinstance(item, list) and value.get("history_columns"):
                item = [dict(zip(value["history_columns"], row)) for row in item]
            if key == "history_columns" and "history" in value:
                continue  # Names appear alongside each history sample instead.
            if simple(item):
                lines.append(pad + label(key) + ": " + scalar(item))
            elif not item:
                lines.append(pad + label(key) + ": (none)")
            else:
                lines.append(pad + label(key) + ":")
                lines.extend(readable(item, indent + 2))
        return lines
    if isinstance(value, (list, tuple)):
        if not value:
            return [pad + "(none)"]
        if all(isinstance(item, dict) and item for item in value):
            keys = list(dict.fromkeys(key for item in value for key in item))
            columns = [key for key in keys if all(simple(item.get(key)) for item in value)]
            if "unit_id" in columns and "lifecycle" in columns:
                columns = [key for key in ("unit_id", "lifecycle", "readiness", "ownership", "fresh", "blocked", "launch_mode")
                           if key in columns]
            rows = [[scalar(item[key]) if key in item else "-" for key in columns] for item in value]
            widths = [max(len(label(key)), *(len(row[index]) for row in rows))
                      for index, key in enumerate(columns)]
            if columns and len(columns) <= 8 and sum(widths) + 2 * (len(columns) - 1) <= 140:
                def row_text(row):
                    return pad + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
                lines = [row_text([label(key) for key in columns]), row_text(["-" * width for width in widths])]
                lines.extend(row_text(row) for row in rows)
                for index, item in enumerate(value, 1):
                    nested = {key: field for key, field in item.items() if key not in columns}
                    if nested:
                        identity = next((item[key] for key in ("unit_id", "id", "name", "capability_id") if key in item), index)
                        lines.append(pad + scalar(identity) + ":")
                        lines.extend(readable(nested, indent + 2))
                return lines
        lines = []
        for index, item in enumerate(value, 1):
            if simple(item):
                lines.append(pad + "- " + scalar(item))
            else:
                lines.append(pad + f"{index}.")
                lines.extend(readable(item, indent + 2))
        return lines
    return [pad + scalar(value)]


def emit(value, *, structured, compact=False, stream=False, error=False, file=None):
    destination = sys.stdout if file is None else file
    if structured:
        print(json.dumps(value, default=str, indent=None if compact or error or stream else 2),
              file=destination, flush=True)
        return
    if error and isinstance(value, dict) and isinstance(value.get("error"), dict):
        problem = value["error"]
        lines = [f"Error [{scalar(problem.get('code', 'error'))}]: {scalar(problem.get('message', ''))}"]
        lines.extend(readable({key: item for key, item in problem.items() if key not in ("code", "message")}, 2)
                     if problem.keys() - {"code", "message"} else [])
    else:
        if stream and isinstance(value, dict) and isinstance(value.get("history"), list) and "history_columns" in value:
            count = len(value["history"])
            value = {key: item for key, item in value.items() if key not in ("history", "history_columns")}
            value = dict(value, retained_history=f"{count} samples; read resources --history for values; --json preserves watch history")
        lines = readable(value)
    if stream:
        lines.insert(0, "--- Update ---")
    print("\n".join(lines), file=destination, flush=True)


def log_time(value):
    try:
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(value))).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return scalar(value)


def emit_logs(value, *, structured, follow=False, first=True):
    if structured:
        emit(value, structured=True, stream=follow)
        return
    # Follow stays pinned to one run. Empty polls do not flood the terminal.
    if first or not follow:
        emit({key: item for key, item in value.items() if key not in ("records", "next_cursor")}, structured=False)
    elif value.get("gap") or value.get("truncated"):
        emit(dict(gap=value.get("gap"), truncated=value.get("truncated")), structured=False)
    for record in value["records"]:
        print(f"{log_time(record.get('realtime_us', ''))}  {scalar(record['message'])}"
              + (" [message truncated]" if record.get("message_truncated") else ""), flush=True)
    if not follow:
        if not value["records"]:
            print("No log records.")
        print("Next cursor: " + scalar(value.get("next_cursor")), flush=True)
