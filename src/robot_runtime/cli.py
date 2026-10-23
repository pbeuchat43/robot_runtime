"""Standalone runtime client. The web server is neither imported nor required."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from .common import Fault, loads
from .ipc import request, watch


def main():
    parser = argparse.ArgumentParser(description="Operate declared runtime units through the local manager")
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "robot_runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--fresh", action="store_true")
    commands.add_parser("watch")
    resources = commands.add_parser("resources", help="Read cached resource usage; --watch activates shared monitoring")
    resources.add_argument("--watch", action="store_true")
    resources.add_argument("--history", action="store_true", help="Include retained host/GPU history in the cached read")
    validate = commands.add_parser("validate")
    validate.add_argument("profile", type=Path)
    operation = commands.add_parser("operation")
    operation.add_argument("request_id")
    inspect = commands.add_parser("inspect", help="Show retained supervisor identity for Linux recovery")
    inspect.add_argument("run_id")
    logs = commands.add_parser("logs")
    logs.add_argument("run_id")
    logs.add_argument("--limit", type=int, default=100)
    logs.add_argument("--cursor")
    for action in ("start", "stop", "resolve"):
        command = commands.add_parser(action)
        command.add_argument("unit_id")
        command.add_argument("--request-id")
        command.add_argument("--config-revision")
        command.add_argument("--expires-at-ms", type=int)
        if action != "start":
            command.add_argument("--expected-run", required=True)
        else:
            command.add_argument("--mode", help="Choose an additional launch mode declared by the profile")
    credential = commands.add_parser("credential", help="Create a new web credential file; prompts without echo")
    credential.add_argument("--file", type=Path, required=True)
    ros = commands.add_parser("ros", help="Observe declared ROS capabilities and operate services, parameters or actions")
    ros_commands = ros.add_subparsers(dest="ros_command", required=True)
    ros_commands.add_parser("status")
    topic_command = ros_commands.add_parser("command", help="Publish one declared fixed-choice topic command; observe provider state separately")
    topic_command.add_argument("capability")
    topic_command.add_argument("choice")
    topic_command.add_argument("--expected-run", required=True)
    topic_command.add_argument("--request-id")
    topic_command.add_argument("--generation")
    topic_command.add_argument("--config-revision")
    topic_command.add_argument("--expires-at-ms", type=int)
    actions = ros_commands.add_parser("actions", help="Read current goals, results and expiring feedback")
    actions.add_argument("--watch", action="store_true", help="Read cached action observations once a second; never submits goals")
    action = ros_commands.add_parser("action", help="Submit a goal or explicitly cancel its exact UUID")
    action_commands = action.add_subparsers(dest="action_command", required=True)
    for name in ("send", "cancel"):
        command = action_commands.add_parser(name)
        command.add_argument("capability")
        command.add_argument("--expected-run", required=True)
        command.add_argument("--arguments", default="{}") if name == "send" else command.add_argument("--goal", required=True)
        command.add_argument("--request-id")
        command.add_argument("--generation")
        command.add_argument("--config-revision")
        command.add_argument("--expires-at-ms", type=int)
    ros_watch = ros_commands.add_parser("watch")
    ros_watch.add_argument("--topic", action="append", required=True)
    ros_call = ros_commands.add_parser("call")
    ros_call.add_argument("capability")
    ros_call.add_argument("--expected-run", required=True)
    ros_call.add_argument("--arguments", default="{}", help="JSON object; int64/uint64 fields use decimal strings")
    ros_call.add_argument("--request-id")
    ros_call.add_argument("--generation")
    ros_call.add_argument("--config-revision")
    ros_call.add_argument("--expires-at-ms", type=int)
    ros_acknowledge = ros_commands.add_parser("acknowledge", help="Acknowledge uncertainty locally after inspecting provider state; never replays")
    ros_acknowledge.add_argument("request_id")
    ros_acknowledge.add_argument("--expected-run", required=True)
    parameters = ros_commands.add_parser("parameters", help="Inspect or edit declared live parameters")
    parameter_commands = parameters.add_subparsers(dest="parameter_command", required=True)
    parameter_read = parameter_commands.add_parser("read")
    parameter_read.add_argument("capability")
    parameter_read.add_argument("--cached", action="store_true", help="Return cached observations without contacting the ROS provider")
    parameter_set = parameter_commands.add_parser("set")
    parameter_set.add_argument("capability")
    parameter_set.add_argument("--expected-run", required=True)
    parameter_set.add_argument("--expected-snapshot", required=True)
    parameter_set.add_argument("--changes", required=True, help='JSON name-to-{type,value} map; integers use decimal strings')
    parameter_set.add_argument("--request-id")
    parameter_set.add_argument("--generation")
    parameter_set.add_argument("--config-revision")
    parameter_set.add_argument("--expires-at-ms", type=int)
    args = parser.parse_args()
    try:
        if args.command == "ros":
            if args.ros_command == "command":
                status = request(args.state, "ros_status")
                identifier = args.request_id or str(uuid.uuid4())
                print("Request ID: " + identifier, file=sys.stderr, flush=True)
                result = request(args.state, "ros_topic_command", capability_id=args.capability, choice=args.choice, request_id=identifier,
                    expected_run=args.expected_run, generation=args.generation or status["generation"], config_revision=args.config_revision or status["config_revision"],
                    expires_at_ms=args.expires_at_ms if args.expires_at_ms is not None else status["server_time_ms"]+10000)
                print(json.dumps(result, indent=2))
                return
            if args.ros_command == "actions":
                while True:
                    result = request(args.state, "ros_actions")
                    print(json.dumps(result, indent=None if args.watch else 2), flush=True)
                    if not args.watch:
                        return
                    time.sleep(1)
            if args.ros_command == "action":
                status = request(args.state, "ros_actions")
                payload = dict(arguments=loads(args.arguments)) if args.action_command == "send" else dict(goal_request_id=args.goal)
                identifier = args.request_id or str(uuid.uuid4())
                print("Request ID: " + identifier, file=sys.stderr, flush=True)
                result = request(args.state, "ros_action_goal" if args.action_command == "send" else "ros_action_cancel",
                    capability_id=args.capability, request_id=identifier, expected_run=args.expected_run,
                    generation=args.generation or status["generation"], config_revision=args.config_revision or status["config_revision"],
                    expires_at_ms=args.expires_at_ms if args.expires_at_ms is not None else status["server_time_ms"] + 10000, **payload)
                print(json.dumps(result, indent=2))
                return
            if args.ros_command == "parameters":
                if args.parameter_command == "read":
                    result = request(args.state, "ros_parameters", capability_id=args.capability, refresh=not args.cached)
                    deadline = time.monotonic() + 12
                    while not args.cached and result["reading"] and time.monotonic() < deadline:
                        time.sleep(0.1)
                        result = request(args.state, "ros_parameters", capability_id=args.capability, refresh=False)
                    if not args.cached and (result["reading"] or result["read_status"] != "complete"):
                        raise Fault("parameter_read_failed", "Parameter inspection failed", result=result)
                else:
                    catalog = request(args.state, "ros_status")
                    identifier = args.request_id or str(uuid.uuid4())
                    print("Request ID: " + identifier, file=sys.stderr, flush=True)
                    result = request(args.state, "ros_parameter_set", capability_id=args.capability, request_id=identifier,
                        expected_run=args.expected_run, expected_snapshot=args.expected_snapshot,
                        generation=args.generation or catalog["generation"], config_revision=args.config_revision or catalog["config_revision"],
                        expires_at_ms=args.expires_at_ms if args.expires_at_ms is not None else int(time.time()*1000)+10000,
                        changes=loads(args.changes))
                print(json.dumps(result, indent=2))
                return
            if args.ros_command == "watch":
                for result in watch(args.state, "ros_watch", topics=args.topic):
                    print(json.dumps(result), flush=True)
                return
            if args.ros_command == "call":
                status = request(args.state, "ros_status")
                identifier = args.request_id or str(uuid.uuid4())
                print("Request ID: " + identifier, file=sys.stderr, flush=True)
                result = request(args.state, "ros_service", capability_id=args.capability, request_id=identifier,
                                 expected_run=args.expected_run, generation=args.generation or status["generation"],
                                 config_revision=args.config_revision or status["config_revision"],
                                 expires_at_ms=args.expires_at_ms if args.expires_at_ms is not None else int(time.time()*1000)+10000,
                                 arguments=loads(args.arguments))
            elif args.ros_command == "acknowledge":
                result = request(args.state, "ros_acknowledge", request_id=args.request_id, expected_run=args.expected_run)
            else:
                result = request(args.state, "ros_status")
        elif args.command == "resources":
            if args.watch:
                for result in watch(args.state, "resource_watch"):
                    print(json.dumps(result), flush=True)
                return
            result = request(args.state, "resource_status", history=args.history)
        elif args.command == "validate":
            from .profile import read
            config, revision = read(args.profile)
            result = dict(valid=True, config_revision=revision, units=[unit["id"] for unit in config["units"]])
            if "ros" in config:
                result["ros_interfaces"] = "Validated by the ROS worker at manager startup; inspect ros status"
        elif args.command == "credential":
            from .auth import create_credential
            create_credential(args.file)
            result = dict(created=True, path=str(args.file))
        elif args.command == "watch":
            for result in watch(args.state):
                print(json.dumps(result), flush=True)
            return
        elif args.command in ("start", "stop", "resolve"):
            revision = args.config_revision or request(args.state, "status")["config_revision"]
            identifier = args.request_id or str(uuid.uuid4())
            values = dict(unit_id=args.unit_id, request_id=identifier, config_revision=revision,
                          expires_at_ms=args.expires_at_ms if args.expires_at_ms is not None else int(time.time() * 1000) + 10000)
            if args.command != "start":
                values["expected_run"] = args.expected_run
            elif args.mode is not None:
                values["mode"] = args.mode
            # Print before dispatch so a lost response never loses its lookup ID.
            print("Request ID: " + identifier, file=sys.stderr, flush=True)
            result = request(args.state, args.command, **values)
        elif args.command == "status":
            result = request(args.state, "status", fresh=args.fresh)
        elif args.command == "operation":
            result = request(args.state, "operation", request_id=args.request_id)
        elif args.command == "inspect":
            result = request(args.state, "inspect", run_id=args.run_id)
        else:
            result = request(args.state, "logs", run_id=args.run_id, limit=args.limit, cursor=args.cursor)
        print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        return
    except (Fault, OSError) as exc:
        result = exc.as_dict() if isinstance(exc, Fault) else dict(code="io_error", message=str(exc))
        print(json.dumps(dict(error=result)), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
