"""Durable action intent; feedback is an expiring, memory-only observation."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import hashlib

from .common import Fault, dumps, fields
from .core import public_operation, request_id
from .ros_codec import arguments

COMMANDS = ("ros_action_goal", "ros_action_cancel")
PENDING = ("prepared", "in_progress")


class ActionRuntime:
    def __init__(self, ros):
        self.ros, self.core = ros, ros.core
        self.specs = {spec["id"]: spec for spec in ros.config.get("actions", [])} if ros.config else {}
        self.slots, self.feedback, self.availability, self.bindings = {}, {}, {}, {}
        for op in sorted(self.core.operations.values(), key=lambda value: (value.get("goal_sequence", 0), value.get("created_at_ms", 0))):
            if op["action"] == "ros_action_goal" and op["capability_id"] in self.specs:
                self.slots[op["capability_id"]] = op["request_id"]
            if op["action"] in COMMANDS and op["phase"] in PENDING:
                self.core._save_operation(op, phase="outcome_unknown", outcome="manager_restarted")

    def goal(self, key):
        return self.core.operations.get(self.slots.get(key))

    def catalog(self):
        rows = []
        for key, spec in self.specs.items():
            run = self.ros.live_run(spec["unit_id"])
            op = self.goal(key)
            same_run = bool(run and op and op["run_id"] == run["run_id"])
            blocked = bool(same_run and not op["terminal"])
            observed = self.feedback.get(op["request_id"], {}) if op else {}
            age = self.core.clock() - observed["received_monotonic"] if observed else None
            available = bool(self.ros.ready and run and self.availability.get(key, {}).get("available"))
            cancel = self.core.operations.get(op.get("cancel_request_id")) if op else None
            value = dict(spec, **self.ros.schemas.get(key, {}), run_id=run["run_id"] if run else None,
                         available=available, error=self.availability.get(key, {}).get("error", "awaiting_discovery"),
                         can_send=available and not blocked,
                         can_cancel=available and blocked and (op.get("accepted") is True or op["phase"] == "outcome_unknown" or op["phase"] == "timed_out")
                         and not (cancel and cancel["phase"] in PENDING),
                         goal=public_operation(op) if op else None, cancellation=public_operation(cancel) if cancel else None,
                         feedback=observed.get("data"), feedback_sequence=observed.get("sequence"),
                         feedback_age_seconds=age, feedback_fresh=bool(available and same_run and observed and observed.get("data") is not None
                             and not observed.get("error") and age <= spec["stale_after_seconds"]),
                         feedback_error=observed.get("error"), same_run=same_run)
            rows.append(value)
        return rows

    def snapshot(self):
        return dict(api_version=1, config_revision=self.core.revision, boot_id=self.core.boot,
                    generation=self.ros.generation, available=self.ros.ready, error=self.ros.error,
                    server_time_ms=int(self.core.wall() * 1000), actions=self.catalog())

    def command(self, value, actor):
        cancel = value["action"] == "ros_action_cancel"
        fields(value, ("version", "action", "capability_id", "request_id", "expected_run", "generation",
                       "config_revision", "expires_at_ms", "goal_request_id" if cancel else "arguments"))
        rid = request_id(value["request_id"])
        fingerprint = hashlib.sha256(dumps(value).encode()).hexdigest()
        old = self.core.operations.get(rid)
        if old:
            if old["fingerprint"] != fingerprint:
                raise Fault("request_conflict", "Request ID was used with different arguments")
            return public_operation(old)
        self.core.store.capacity()
        key = value["capability_id"]
        if not isinstance(key, str) or key not in self.specs:
            raise Fault("not_found", "Unknown action capability")
        expiry = value["expires_at_ms"]
        remaining = expiry - int(self.core.wall() * 1000) if type(expiry) is int else -1
        if not 0 < remaining <= 30000:
            raise Fault("expired", "Command expiry must be in the next 30 seconds")
        dispatch_deadline = self.core.clock() + remaining / 1000
        if value["config_revision"] != self.core.revision:
            raise Fault("stale_config", "Profile revision changed")
        if not self.ros.ready:
            raise Fault("ros_unavailable", "ROS worker is unavailable")
        if value["generation"] != self.ros.generation:
            raise Fault("stale_generation", "ROS worker generation changed")
        spec = self.specs[key]
        run = self.ros.require_run(spec["unit_id"], value["expected_run"])
        goal = self.goal(key)
        occupied = goal and goal["run_id"] == run["run_id"] and not goal["terminal"]
        if cancel:
            goal_id = request_id(value["goal_request_id"])
            if not occupied or goal["request_id"] != goal_id:
                raise Fault("stale_goal", "Cancellation requires the exact unresolved goal of this run")
            previous_cancel = self.core.operations.get(goal.get("cancel_request_id"))
            if previous_cancel and previous_cancel["phase"] in PENDING:
                raise Fault("busy", "A cancellation request is already pending")
        else:
            if occupied:
                raise Fault("busy", "An active or uncertain goal already reserves this action", request_id=goal["request_id"])
            arguments(value["arguments"], self.ros.schemas[key]["goal_schema"])
            goal_id = rid
        if self.core.clock() >= dispatch_deadline or self.core.wall() * 1000 >= expiry:
            raise Fault("expired", "Action command expired during validation")
        op = dict(request_id=rid, action=value["action"], capability_id=key, unit_id=spec["unit_id"], run_id=run["run_id"],
                  goal_request_id=goal_id, fingerprint=fingerprint, actor=actor, phase="prepared", delivery="unknown",
                  generation=self.ros.generation, boot_id=self.core.boot, created_at_ms=int(self.core.wall() * 1000),
                  expires_at_ms=expiry, dispatch_deadline=dispatch_deadline,
                  response_deadline=self.core.clock() + spec["response_timeout_seconds"])
        if not cancel:
            op.update(terminal=False, accepted=None, goal_status="pending", action_name=spec["name"], action_type=spec["type"],
                      goal_sequence=(goal.get("goal_sequence", 0) if goal else 0) + 1)
        self.core.store.save(operation=op)
        self.core.operations[rid] = op
        if cancel:
            self.core._save_operation(goal, cancel_request_id=rid)
        else:
            if goal:
                self.feedback.pop(goal["request_id"], None)
            self.slots[key] = rid
        try:
            self.ros.transport.send(dict(kind="action_cancel" if cancel else "action_goal", request_id=rid,
                capability_id=key, goal_request_id=goal_id, run_id=run["run_id"], dispatch_deadline=dispatch_deadline,
                response_deadline=op["response_deadline"], **({} if cancel else dict(arguments=value["arguments"]))))
        except Fault:
            self.core._save_operation(op, phase="outcome_unknown", outcome="worker_delivery_unknown")
        self.ros.changed()
        return public_operation(op)

    def authorize(self, value):
        fields(value, ("version", "action", "request_id", "generation"))
        op = self.core.operations.get(request_id(value["request_id"]))
        if not op or op["action"] not in COMMANDS or op["phase"] != "prepared" or value["generation"] != self.ros.generation:
            raise Fault("stale_generation", "Action dispatch is no longer authorized")
        self.ros.require_run(op["unit_id"], op["run_id"])
        if self.core.clock() >= min(op["dispatch_deadline"], op["response_deadline"]) or self.core.wall() * 1000 >= op["expires_at_ms"]:
            raise Fault("expired", "Action command expired before dispatch")
        goal = self.goal(op["capability_id"])
        if not goal or goal["request_id"] != op["goal_request_id"] or goal["terminal"]:
            raise Fault("stale_goal", "The goal reservation has changed")
        self.core._save_operation(op, phase="in_progress")
        self.ros.changed()
        return dict(dispatch_deadline=op["dispatch_deadline"], response_deadline=op["response_deadline"])

    def sync(self):
        desired = {}
        for key in self.specs:
            op = self.goal(key)
            run = self.ros.live_run(self.specs[key]["unit_id"])
            if op and not op["terminal"] and run and run["run_id"] == op["run_id"]:
                desired[key] = dict(request_id=op["request_id"], run_id=op["run_id"])
            elif op and not op["terminal"] and op["phase"] in PENDING:
                self.core._save_operation(op, phase="outcome_unknown", outcome="runtime_unavailable")
        if self.ros.ready and desired != self.bindings:
            try:
                self.ros.transport.send(dict(kind="action_track", goals=desired))
                self.bindings = desired
            except Fault:
                self.lost("worker_delivery_unknown")

    def event(self, value):
        rid = value["request_id"]
        op = self.core.operations.get(rid)
        if not op or op["action"] not in COMMANDS:
            return
        if value["kind"] == "action_feedback":
            if op["action"] == "ros_action_goal" and not op["terminal"]:
                self.feedback[rid] = {key: value[key] for key in ("data", "sequence", "received_monotonic", "error")}
            return
        if op["action"] == "ros_action_goal" and op["terminal"]:
            return
        allowed = ("phase", "delivery", "outcome", "terminal", "accepted", "goal_status", "result", "result_status", "cancel_accepted")
        changes = {key: value[key] for key in allowed if key in value and op.get(key) != value[key]}
        if changes:
            self.core._save_operation(op, **changes)
            self.ros.changed()

    def lost(self, reason):
        self.feedback.clear()
        self.availability.clear()
        self.bindings = {}
        for op in self.core.operations.values():
            if op["action"] in COMMANDS and op["phase"] in PENDING:
                self.core._save_operation(op, phase="outcome_unknown", outcome=reason)

    def tick(self):
        changed = False
        current = [self.core.operations[rid] for rid in self.slots.values()]
        current += [self.core.operations[op["cancel_request_id"]] for op in current if op.get("cancel_request_id")]
        for op in current:
            waiting = op["action"] in COMMANDS and op["phase"] in PENDING
            if waiting and (op["action"] == "ros_action_cancel" or op.get("accepted") is None) and self.core.clock() >= op["response_deadline"]:
                self.core._save_operation(op, phase="timed_out", outcome="response_deadline_exceeded")
                changed = True
        self.sync()
        if changed:
            self.ros.changed()
