"""Explicitly started, finite counting action. No hardware access or actuation."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path


def main():
    import rclpy
    from rclpy.action import ActionServer, CancelResponse, GoalResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from robot_runtime_demo_interfaces.action import Count

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--lifetime", type=int, default=600, choices=(300, 600))
    parser.add_argument("--test-controls", action="store_true")
    parser.add_argument("--duplicate", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.area.mkdir(parents=True, exist_ok=True, mode=0o700)
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("provider", namespace=args.namespace, enable_rosout=False, start_parameter_services=False)
    lock, stopped = threading.RLock(), threading.Event()
    events = (args.area / ("duplicate-events.jsonl" if args.duplicate else "provider-events.jsonl")).open("a")
    status_path = args.area / ("duplicate-status.json" if args.duplicate else "provider-status.json")
    stats = dict(goals_received=0, goals_accepted=0, cancel_requests=0, completed=0, active_goal=None, goal_ids=[])

    def controls():
        if not args.test_controls:
            return {}
        try:
            return json.loads((args.area / "control.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def record(event, **values):
        with lock:
            events.write(json.dumps(dict(event=event, monotonic=time.monotonic(), **values)) + "\n")
            events.flush()
            temporary = status_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(stats))
            os.replace(temporary, status_path)

    def goal(body):
        with lock:
            stats["goals_received"] += 1
            valid = not stats["active_goal"] and 1 <= body.steps <= 100 and .05 <= body.period_seconds <= 1
            valid = valid and not controls().get("reject_goals", False)
            if valid:
                stats["active_goal"] = "pending"
        record("goal_received", accepted=valid)
        time.sleep(min(3, max(0, controls().get("accept_delay", 0))))
        return GoalResponse.ACCEPT if valid else GoalResponse.REJECT

    def accepted(handle):
        goal_id = bytes(handle.goal_id.uuid).hex()
        with lock:
            stats["goals_accepted"] += 1
            stats["active_goal"] = goal_id
            stats["goal_ids"].append(goal_id)
            stats["goal_ids"] = stats["goal_ids"][-100:]
        record("goal_accepted", goal_id=goal_id)
        handle.execute()

    def cancel(handle):
        with lock:
            stats["cancel_requests"] += 1
        allow = not controls().get("reject_cancel", False)
        record("cancel_received", goal_id=bytes(handle.goal_id.uuid).hex(), accepted=allow)
        return CancelResponse.ACCEPT if allow else CancelResponse.REJECT

    def execute(handle):
        completed = 0
        while completed < handle.request.steps and not stopped.is_set():
            if handle.is_cancel_requested:
                time.sleep(min(3, max(0, controls().get("cancel_delay", 0))))
                handle.canceled()
                result = Count.Result(success=False, completed=completed, message="Counting canceled")
                break
            time.sleep(handle.request.period_seconds)
            completed += 1
            if not controls().get("pause_feedback", False):
                handle.publish_feedback(Count.Feedback(completed=completed, total=handle.request.steps,
                    message="Counting without hardware"))
        else:
            if stopped.is_set() or handle.request.abort_at_end:
                handle.abort()
                result = Count.Result(success=False, completed=completed, message="Synthetic abort")
            else:
                handle.succeed()
                result = Count.Result(success=True, completed=completed, message="Counting complete")
        with lock:
            stats["completed"] += 1
            stats["active_goal"] = None
        record("goal_finished", goal_id=bytes(handle.goal_id.uuid).hex(), success=result.success, completed=completed)
        return result

    group = ReentrantCallbackGroup()
    server = ActionServer(node, Count, args.namespace + "/count", execute_callback=execute,
        goal_callback=goal, cancel_callback=cancel, handle_accepted_callback=accepted, callback_group=group, result_timeout=60)
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(number, lambda *_: stopped.set())
    signal.alarm(args.lifetime)
    record("ready")
    print("Synthetic counting action ready; no hardware access", flush=True)
    try:
        while not stopped.is_set():
            executor.spin_once(timeout_sec=.1)
    finally:
        stopped.set()
        executor.shutdown(timeout_sec=4)
        server.destroy()
        node.destroy_node()
        rclpy.shutdown()
        events.close()


if __name__ == "__main__":
    main()
