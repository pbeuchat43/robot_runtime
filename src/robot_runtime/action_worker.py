"""Native ROS action protocol, including UUID-only observation after recovery."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import time
import uuid

from robot_runtime.common import Fault
from robot_runtime.ipc import request
from robot_runtime.ros_codec import arguments, projection, selected_schema

STATES = {1: "accepted", 2: "executing", 3: "canceling", 4: "succeeded", 5: "canceled", 6: "aborted"}


class ActionWorker:
    def __init__(self, worker):
        from rosidl_runtime_py.utilities import get_action
        self.worker, self.node = worker, worker.node
        self.specs = {spec["id"]: spec for spec in worker.config.get("actions", [])}
        self.types, self.tasks, self.cancels, self.availability = {}, {}, {}, {}
        for key, spec in self.specs.items():
            kind = self.types[key] = get_action(spec["type"])
            worker.schemas[key] = dict(goal_schema=selected_schema(kind.Goal(), spec["goal_fields"], complete=True),
                result_schema=selected_schema(kind.Result(), spec["result_fields"]),
                feedback_schema=selected_schema(kind.Feedback(), spec["feedback_fields"]))

    def target(self, key):
        from rclpy.action.graph import get_action_server_names_and_types_by_node
        spec = self.specs[key]
        namespace, name = spec["node"].rsplit("/", 1)
        if not self.worker.unique_node(spec["node"]):
            raise Fault("source_ambiguous", "Action requires exactly one declared graph node")
        declared = dict(get_action_server_names_and_types_by_node(self.node, name, namespace or "/"))
        if declared.get(spec["name"]) != [spec["type"]] or any(self.node.count_services(spec["name"] + "/_action/" + suffix) != 1
                for suffix in ("send_goal", "get_result", "cancel_goal")):
            raise Fault("source_unavailable", "Action name/type requires one declared provider")

    def audit(self):
        values = {}
        for key in self.specs:
            try:
                self.target(key)
                values[key] = dict(available=True, error=None)
            except Fault as exc:
                values[key] = dict(available=False, error=exc.code)
        if values != self.availability:
            self.availability = values
            self.worker.status()

    def emit(self, kind, rid, **values):
        self.worker.output.send(kind + ":" + rid, dict(kind=kind, generation=self.worker.generation, request_id=rid, **values))

    def update(self, task, **values):
        task["report"].update(values)
        self.emit("action_update", task["id"], **task["report"])

    def message_uuid(self, rid):
        from unique_identifier_msgs.msg import UUID
        return UUID(uuid=list(uuid.UUID(rid).bytes))

    def new_task(self, key, rid, run_id):
        task = dict(id=rid, key=key, run_id=run_id, clients={}, subscriptions=[], goal_future=None, result_future=None,
                    pending_feedback=None, sequence=0, next_feedback=0, next_result=0, observed_terminal=None,
                    terminal_at=None, report=dict(terminal=False, goal_status="unknown", phase="outcome_unknown", delivery="unknown"))
        self.tasks[key] = task
        return task

    def connect(self, task):
        from action_msgs.msg import GoalStatusArray
        from action_msgs.srv import CancelGoal
        from rclpy.qos import qos_profile_action_status_default
        key = task["key"]
        spec, kind = self.specs[key], self.types[key]
        self.target(key)
        clients = task["clients"]
        for suffix, service in (("send_goal", kind.Impl.SendGoalService), ("get_result", kind.Impl.GetResultService), ("cancel_goal", CancelGoal)):
            clients[suffix] = self.node.create_client(service, spec["name"] + "/_action/" + suffix)
        task["subscriptions"].append(self.node.create_subscription(GoalStatusArray, spec["name"] + "/_action/status",
            lambda raw: self.status(task, raw), qos_profile_action_status_default, raw=True))
        task["subscriptions"].append(self.node.create_subscription(kind.Impl.FeedbackMessage, spec["name"] + "/_action/feedback",
            lambda raw: self.feedback(task, raw), 1, raw=True))

    def suspend(self, task):
        for subscription in task["subscriptions"]:
            self.node.destroy_subscription(subscription)
        for client in task["clients"].values():
            self.node.destroy_client(client)
        task.update(clients={}, subscriptions=[], goal_future=None, result_future=None, pending_feedback=None)
        for rid, cancel in list(self.cancels.items()):
            if cancel["task"] is task:
                self.emit("action_cancel_result", rid, phase="outcome_unknown", delivery="unknown", outcome="provider_unavailable")
                del self.cancels[rid]

    def track(self, goals):
        for key, task in list(self.tasks.items()):
            if goals.get(key, {}).get("request_id") != task["id"]:
                self.suspend(task)
                del self.tasks[key]
        for key, goal in goals.items():
            if key not in self.tasks:
                self.new_task(key, goal["request_id"], goal["run_id"])

    def status(self, task, raw):
        if task["report"].get("terminal") or len(raw) > 65536 or not self.availability.get(task["key"], {}).get("available"):
            return
        from action_msgs.msg import GoalStatusArray
        from rclpy.serialization import deserialize_message
        try:
            values = deserialize_message(raw, GoalStatusArray).status_list
            matches = [value.status for value in values if bytes(value.goal_info.goal_id.uuid) == uuid.UUID(task["id"]).bytes]
            if len(matches) == 1 and matches[0] in STATES:
                state = matches[0]
                if state >= 4:
                    task["observed_terminal"] = state
                    task["terminal_at"] = task["terminal_at"] or time.monotonic()
                if task["report"].get("goal_status") != STATES[state]:
                    self.update(task, goal_status=STATES[state], accepted=True, phase="in_progress", delivery="accepted", outcome=None)
        except Exception:
            self.update(task, phase="outcome_unknown", outcome="invalid_status")

    def feedback(self, task, raw):
        if not task["report"].get("terminal") and self.availability.get(task["key"], {}).get("available"):
            task["pending_feedback"] = (raw if len(raw) <= 65536 else None, time.monotonic())

    def start_goal(self, message):
        rid, key = message["request_id"], message["capability_id"]
        task, sent = None, False
        try:
            previous = self.tasks.get(key)
            if previous and previous["report"].get("terminal"):
                self.suspend(previous)
                del self.tasks[key]
            if key in self.tasks:
                raise Fault("busy", "The action already has a tracked goal; goals are never replayed")
            task = self.new_task(key, rid, message["run_id"])
            self.connect(task)
            body = self.types[key].Impl.SendGoalService.Request()
            body.goal_id = self.message_uuid(rid)
            for field, value in arguments(message["arguments"], self.worker.schemas[key]["goal_schema"]).items():
                setattr(body.goal, field, value)
            client = task["clients"]["send_goal"]
            self.ready(client, message["dispatch_deadline"])
            authority = request(self.worker.state, "ros_action_authorize", request_id=rid, generation=self.worker.generation)
            self.target(key)
            if time.monotonic() >= authority["dispatch_deadline"]:
                raise Fault("expired", "Action goal expired before dispatch")
            task["goal_future"] = client.call_async(body)
            sent = True
            task["response_deadline"] = authority["response_deadline"]
            self.update(task, phase="in_progress", delivery="sent", goal_status="pending", outcome=None)
        except Exception as exc:
            code = exc.code if isinstance(exc, Fault) else "ros_error"
            if task:
                self.update(task, phase="outcome_unknown" if sent else "failed", delivery="unknown" if sent else "not_sent",
                            terminal=not sent, goal_status="unknown" if sent else "not_sent", outcome=code)
            else:
                self.emit("action_update", rid, phase="failed", delivery="not_sent", terminal=True, goal_status="not_sent", outcome=code)

    def ready(self, client, deadline):
        until = min(deadline, time.monotonic() + .5)
        while not client.service_is_ready() and time.monotonic() < until:
            self.worker.rclpy.spin_once(self.node, timeout_sec=.02)
        if not client.service_is_ready():
            raise Fault("source_unavailable", "Action service did not become ready")

    def cancel(self, message):
        from action_msgs.srv import CancelGoal
        rid, sent = message["request_id"], False
        try:
            task = self.tasks.get(message["capability_id"])
            if not task or task["id"] != message["goal_request_id"] or task["report"].get("terminal"):
                raise Fault("stale_goal", "The exact goal is no longer tracked")
            if not task["clients"]:
                self.connect(task)
            self.target(task["key"])
            client = task["clients"]["cancel_goal"]
            self.ready(client, message["dispatch_deadline"])
            authority = request(self.worker.state, "ros_action_authorize", request_id=rid, generation=self.worker.generation)
            self.target(task["key"])
            if time.monotonic() >= authority["dispatch_deadline"]:
                raise Fault("expired", "Cancellation expired before dispatch")
            body = CancelGoal.Request()
            # A nonzero exact UUID and zero timestamp cancel only this goal.
            body.goal_info.goal_id = self.message_uuid(task["id"])
            future = client.call_async(body)
            sent = True
            self.cancels[rid] = dict(task=task, future=future, deadline=authority["response_deadline"], client=client)
            self.emit("action_cancel_result", rid, phase="in_progress", delivery="sent")
        except Exception as exc:
            self.emit("action_cancel_result", rid, phase="outcome_unknown" if sent else "failed", delivery="unknown" if sent else "not_sent",
                      outcome=exc.code if isinstance(exc, Fault) else "ros_error")

    def finish(self, task, state, response=None, result_status="available"):
        result = None
        if response is not None:
            try:
                result = projection(response, self.worker.schemas[task["key"]]["result_schema"])
            except Exception:
                result_status = "invalid"
        self.update(task, phase="complete", delivery="responded", terminal=True, accepted=True,
                    goal_status=STATES[state], outcome=STATES[state], result=result, result_status=result_status)

    def progress(self):
        from rclpy.serialization import deserialize_message
        now = time.monotonic()
        for task in list(self.tasks.values()):
            key, spec = task["key"], self.specs[task["key"]]
            if task["report"].get("terminal"):
                continue
            if not self.availability.get(key, {}).get("available"):
                if task["clients"]:
                    self.suspend(task)
                    self.update(task, phase="outcome_unknown", outcome="provider_unavailable")
                continue
            if not task["clients"]:
                try:
                    self.connect(task)
                except Exception:
                    self.suspend(task)
                    continue
            future = task["goal_future"]
            if future is not None and future.done():
                task["goal_future"] = None
                try:
                    response = future.result()
                    if not response.accepted:
                        self.update(task, phase="complete", delivery="responded", accepted=False, terminal=True,
                                    goal_status="rejected", outcome="provider_rejected", result_status="not_applicable")
                        continue
                    self.update(task, phase="in_progress", delivery="accepted", accepted=True,
                                goal_status=task["report"]["goal_status"] if task["report"]["goal_status"] in STATES.values() else "accepted", outcome=None)
                except Exception:
                    self.update(task, phase="outcome_unknown", outcome="goal_response_unavailable")
            pending = task["pending_feedback"]
            if pending and now >= task["next_feedback"]:
                task["pending_feedback"] = None
                task["next_feedback"] = now + 1 / spec["max_feedback_rate_hz"]
                raw, received = pending
                try:
                    if raw is None:
                        raise Fault("sample_too_large", "Action feedback exceeds its byte limit")
                    message = deserialize_message(raw, self.types[key].Impl.FeedbackMessage)
                    if bytes(message.goal_id.uuid) != uuid.UUID(task["id"]).bytes:
                        continue
                    data = projection(message.feedback, self.worker.schemas[key]["feedback_schema"])
                    error = None
                except Exception as exc:
                    data, error = None, exc.code if isinstance(exc, Fault) else "invalid_feedback"
                task["sequence"] += 1
                self.emit("action_feedback", task["id"], data=data, error=error, sequence=task["sequence"], received_monotonic=received)
            # GetResult is read-only and uses the persisted UUID. It never sends
            # a replacement goal, even if the original acceptance reply was lost.
            can_query = task["goal_future"] is None or now >= task.get("response_deadline", 0)
            if can_query and task["result_future"] is None and now >= task["next_result"]:
                client = task["clients"]["get_result"]
                if client.service_is_ready():
                    body = self.types[key].Impl.GetResultService.Request()
                    body.goal_id = self.message_uuid(task["id"])
                    task["result_future"] = client.call_async(body)
            future = task["result_future"]
            if future is not None and future.done():
                task["result_future"] = None
                task["next_result"] = now + 2
                try:
                    response = future.result()
                    if response.status in (4, 5, 6):
                        self.finish(task, response.status, response.result)
                    elif task["observed_terminal"]:
                        self.finish(task, task["observed_terminal"], result_status="unavailable")
                    else:
                        self.update(task, phase="outcome_unknown", goal_status="unknown", outcome="provider_does_not_know_goal")
                except Exception:
                    self.update(task, phase="outcome_unknown", outcome="result_unavailable")
            if task["terminal_at"] and not task["report"].get("terminal") and now - task["terminal_at"] > spec["response_timeout_seconds"]:
                self.finish(task, task["observed_terminal"], result_status="unavailable")
        for rid, cancel in list(self.cancels.items()):
            future, task = cancel["future"], cancel["task"]
            if future.done():
                try:
                    response = future.result()
                    matches = [goal for goal in response.goals_canceling if bytes(goal.goal_id.uuid) == uuid.UUID(task["id"]).bytes]
                    accepted = response.return_code == 0 and len(matches) == len(response.goals_canceling) == 1
                    self.emit("action_cancel_result", rid, phase="complete", delivery="responded", cancel_accepted=accepted,
                              outcome="cancel_accepted" if accepted else "cancel_rejected")
                except Exception:
                    self.emit("action_cancel_result", rid, phase="outcome_unknown", delivery="sent", outcome="invalid_cancel_response")
                del self.cancels[rid]
            elif now >= cancel["deadline"]:
                cancel["client"].remove_pending_request(future)
                future.cancel()
                self.emit("action_cancel_result", rid, phase="timed_out", delivery="sent", outcome="cancel_response_deadline_exceeded")
                del self.cancels[rid]

    def close(self):
        for task in self.tasks.values():
            self.suspend(task)
        self.tasks.clear()
