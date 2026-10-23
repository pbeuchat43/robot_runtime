"""Serialized runtime policy shared by local and browser adapters."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import hashlib
import time
import uuid

from .common import Fault, dumps, fields, text
from .profile import check_launch
from .supervisor import owns


def request_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError) as exc:
        raise Fault("invalid_request", "request_id must be a canonical UUID") from exc
    return value


def public_operation(operation):
    return {k: v for k, v in operation.items() if k not in ("fingerprint", "actor")}


class Core:
    def __init__(self, profile, revision, store, boot_id, *, clock=time.monotonic, wall=time.time):
        self.profile, self.revision, self.store, self.boot = profile, revision, store, boot_id
        self.clock, self.wall = clock, wall
        self.units = {unit["id"]: unit for unit in profile["units"]}
        self.runs = {run["run_id"]: run for run in store.runs()}
        self.operations = {op["request_id"]: op for op in store.operations()}
        previous = store.meta("profile_revision")
        if previous and previous != revision and any(run["blocked"] for run in self.runs.values()):
            raise Fault("maintenance_required", "Changing profiles requires all runs resolved and stopped")
        namespace = store.meta("namespace")
        if namespace and namespace != profile["namespace"]:
            raise Fault("maintenance_required", "A state directory's namespace cannot change")
        store.set_meta("namespace", profile["namespace"])
        store.set_meta("profile_revision", revision)
        self.prefix = "rr-" + profile["namespace"] + "-" + store.meta("installation") + "-"
        self.generation = uuid.uuid4().hex
        self.version = 0
        self.supervisor = None
        self.observations = {}
        self.last_audit = None
        self.error = "Supervisor has not been observed"
        self.foreign = []
        self.ros = None
        self.on_change = lambda: None

    def changed(self):
        self.version += 1
        self.on_change()

    def disconnected(self):
        self.error = "Supervisor connection unavailable"
        for operation in self.operations.values():
            if operation["phase"] in ("prepared", "in_progress"):
                self._save_operation(operation, phase="outcome_unknown")
        self.changed()

    def connected(self, supervisor):
        self.supervisor = supervisor
        self.generation = uuid.uuid4().hex
        self.observations.clear()
        self.audit(full=True)

    def _save_run(self, run, **values):
        if any(run.get(k) != v for k, v in values.items()):
            run.update(values)
            self.store.save(run=run)

    def _save_operation(self, operation, **values):
        if any(operation.get(k) != v for k, v in values.items()):
            operation.update(values)
            self.store.save(operation=operation)
            return True
        return False

    def current(self, unit_id):
        return next((run for run in reversed(list(self.runs.values())) if run["unit_id"] == unit_id), None)

    def _observe(self, run, *, full):
        observed = self.supervisor.snapshot(run["name"], full=full)
        self.observations[run["run_id"]] = observed
        if run["boot_id"] != self.boot:
            if observed:
                self._save_run(run, lifecycle="ownership_conflict")
            else:
                self._save_run(run, lifecycle="interrupted_by_reboot", blocked=False)
            return
        if not observed:
            self._save_run(run, lifecycle="outcome_unknown")
            return
        if not owns(run, observed):
            self._save_run(run, lifecycle="ownership_conflict")
            return
        self._save_run(run, invocation=observed["InvocationID"])
        self.supervisor.retain(run["name"])
        terminal = not observed["members"] and observed["ActiveState"] in ("inactive", "failed") and observed["Job"][0] == 0
        if terminal:
            lifecycle = "exited" if observed["Result"] == "success" else "failed"
            self._save_run(run, lifecycle=lifecycle, blocked=False, terminal=observed)
            # Persist before releasing the only terminal evidence reference.
            self.supervisor.release(run["name"], failed=observed["ActiveState"] == "failed")
        else:
            lifecycle = "starting" if observed["ActiveState"] == "activating" else "running"
            if not observed["MainPID"] and observed["members"]:
                lifecycle = "descendants_surviving"
            self._save_run(run, lifecycle=lifecycle)

    def _finish_operations(self):
        now = self.clock()
        changed = False
        for operation in self.operations.values():
            if operation["action"] not in ("start", "stop"):
                continue
            if operation["phase"] not in ("prepared", "in_progress", "outcome_unknown"):
                continue
            run = self.runs[operation["run_id"]]
            observation = self.observations.get(run["run_id"]) or run.get("terminal")
            if operation["action"] == "start":
                if run["lifecycle"] == "interrupted_by_reboot":
                    changed |= self._save_operation(operation, phase="outcome_unknown")
                elif observation and owns(run, observation) and observation["ActiveState"] != "activating":
                    # Successful start means supervisor launch acceptance, not readiness.
                    changed |= self._save_operation(operation, phase="complete", delivery="observed", outcome=run["lifecycle"])
                elif run["lifecycle"] in ("outcome_unknown", "ownership_conflict"):
                    changed |= self._save_operation(operation, phase="outcome_unknown")
            elif operation["action"] == "stop":
                # Durable terminal evidence can finish a request after a crash,
                # but a first empty observation after the deadline cannot turn a
                # missed observation deadline into success.
                if (observation and owns(run, observation) and not observation["members"] and not run["blocked"]
                        and run["boot_id"] == operation["boot_id"]
                        and observation["observed_monotonic"] <= operation["deadline_monotonic"]):
                    changed |= self._save_operation(operation, phase="complete", outcome="cgroup_empty")
                elif operation["boot_id"] != self.boot:
                    changed |= self._save_operation(operation, phase="outcome_unknown")
                elif now >= operation["deadline_monotonic"]:
                    changed |= self._save_operation(operation, phase="timed_out", outcome="stop_deadline_exceeded")
        return changed

    def progress(self):
        if self._finish_operations():
            self.changed()

    def needs_progress(self):
        return any(op["action"] in ("start", "stop") and (op["phase"] in ("prepared", "in_progress") or
                   op["action"] == "stop" and op["phase"] == "outcome_unknown" and op.get("boot_id") == self.boot)
                   for op in self.operations.values())

    def audit(self, *, full=False):
        if self.supervisor is None:
            raise Fault("supervisor_unavailable", self.error)
        try:
            names = self.supervisor.names()
            known = {run["name"]: run for run in self.runs.values()}
            self.foreign = []
            for name in names:
                run = known.get(name)
                if run is None:
                    self.foreign.append(name)
                elif not run["blocked"]:
                    observed = self.supervisor.snapshot(name, full=True)
                    if observed and (not owns(run, observed) or observed["members"]):
                        self.foreign.append(name)
            for run in list(self.runs.values()):
                if run["blocked"]:
                    self._observe(run, full=full)
            self._finish_operations()
            self.last_audit = self.clock()
            self.error = None
        except Exception:
            self.error = "Supervisor audit failed; state is unknown"
            self.changed()
            raise
        self.changed()

    def status(self):
        now = self.clock()
        age = now - self.last_audit if self.last_audit is not None else None
        fresh = self.error is None and age is not None and age <= 15
        units = []
        for unit in self.units.values():
            run = self.current(unit["id"])
            item = dict(unit_id=unit["id"], label=unit["label"], public_logs=unit.get("public_logs", False),
                        available=unit.get("available", True), unavailable_reason=unit.get("unavailable_reason"),
                        run_id=run["run_id"] if run else None, lifecycle=run["lifecycle"] if run else "stopped",
                        blocked=bool(run and run["blocked"]), readiness="unavailable", fresh=fresh)
            item["launch_modes"] = [{"id": mode["id"], "label": mode["label"]} for mode in unit.get("launch_modes", [])]
            item["launch_mode"] = run.get("launch_mode") if run else None
            item["start_blocked_by"] = self._start_conflicts(unit["id"])
            observed = self.observations.get(run["run_id"]) if run else None
            if not observed and run and not run["blocked"]:
                observed = run.get("terminal")
            item["ownership"] = ("conflict" if run and run["lifecycle"] == "ownership_conflict" else
                                 "unknown" if run and run["blocked"] and not observed else "owned" if run else "none")
            if observed:
                item.update(supervisor_state=observed["ActiveState"], supervisor_result=observed["Result"],
                            main_pid=observed["MainPID"], members=observed["members"],
                            main_exit_code=observed["ExecMainCode"], main_exit_status=observed["ExecMainStatus"],
                            observation_age_seconds=max(0, now - observed["observed_monotonic"]) if run["boot_id"] == self.boot else None)
                item["ownership_observation_age_seconds"] = max(0, now - observed.get("properties_observed_monotonic", observed["observed_monotonic"])) if run["boot_id"] == self.boot else None
            if run and run.get("stop_request_id"):
                item["stop_request_id"] = run["stop_request_id"]
                item["stop_phase"] = self.operations[run["stop_request_id"]]["phase"]
            units.append(item)
        result = dict(api_version=1, robot_id=self.profile["robot_id"], config_revision=self.revision,
                    generation=self.generation, revision=self.version, boot_id=self.boot,
                    server_time_ms=int(self.wall() * 1000), observed_monotonic=self.last_audit,
                    age_seconds=age, fresh=fresh, error=self.error, conflicts=self.foreign, units=units)
        if self.ros:
            result["ros"] = self.ros.summary()
        return result

    def _start_conflicts(self, unit_id):
        return [name for name in self.units[unit_id].get("conflicts", [])
                if (run := self.current(name)) and run["blocked"]]

    def _require_available(self, *, starting=False):
        self.audit(full=True)
        if starting and self.foreign:
            raise Fault("ownership_conflict", "Unowned or conflicting services exist in the runtime namespace")

    def command(self, request, actor):
        action = request["action"]
        fields(request, ("version", "action", "unit_id", "request_id", "expires_at_ms", "config_revision"),
               ("expected_run", "mode"))
        unit_id = text(request["unit_id"], 32)
        if unit_id not in self.units:
            raise Fault("not_found", "Unknown runtime unit")
        rid = request_id(request["request_id"])
        fingerprint = hashlib.sha256(dumps(request).encode()).hexdigest()
        old = self.operations.get(rid)
        if old:
            if old["fingerprint"] != fingerprint:
                raise Fault("request_conflict", "Request ID was used with different arguments")
            return public_operation(old)
        mode = None
        if "mode" in request:
            if action != "start" or not isinstance(request["mode"], str):
                raise Fault("invalid_request", "Only Start accepts a declared launch mode")
            mode = next((item for item in self.units[unit_id].get("launch_modes", []) if item["id"] == request["mode"]), None)
            if mode is None:
                raise Fault("invalid_request", "Unknown launch mode")
        self.store.capacity()
        expiry = request["expires_at_ms"]
        remaining = expiry - self.wall() * 1000 if type(expiry) is int else -1
        if not 0 < remaining <= 30000:
            raise Fault("expired", "Command expiry must be in the next 30 seconds")
        dispatch_deadline = self.clock() + remaining / 1000
        if request["config_revision"] != self.revision:
            raise Fault("stale_config", "Profile revision changed")
        if action == "start" and "expected_run" in request:
            raise Fault("invalid_request", "Start cannot carry an expected run")
        if action in ("stop", "resolve") and not isinstance(request.get("expected_run"), str):
            raise Fault("invalid_request", "Expected run is required")
        self._require_available(starting=action == "start")
        current = self.current(unit_id)
        if action == "start":
            if not self.units[unit_id].get("available", True):
                raise Fault("unit_unavailable", self.units[unit_id]["unavailable_reason"])
            if current and current["blocked"]:
                raise Fault("busy", "Existing run is populated or unresolved", run_id=current["run_id"])
            conflicts = self._start_conflicts(unit_id)
            if conflicts:
                raise Fault("unit_conflict", "A conflicting workload is populated or unresolved", unit_ids=conflicts)
            for dependency in self.units[unit_id]["requires_running"]:
                prerequisite = self.current(dependency)
                if not prerequisite or prerequisite["lifecycle"] != "running":
                    raise Fault("precondition", "A prerequisite is not running", unit_id=dependency)
            spec = dict(self.units[unit_id])
            if mode:
                spec["argv"] = mode["argv"]
            check_launch(spec)
            run_id = uuid.uuid4().hex
            current = dict(run_id=run_id, unit_id=unit_id, name=self.prefix + unit_id + "-" + run_id + ".service",
                           boot_id=self.boot, invocation=None, lifecycle="prepared", blocked=True,
                           spec=spec, config_revision=self.revision,
                           launch_mode={"id": mode["id"], "label": mode["label"]} if mode else None)
        else:
            if not current or current["run_id"] != request["expected_run"]:
                raise Fault("stale_run", "The requested run is no longer current")
            if current["lifecycle"] == "ownership_conflict":
                raise Fault("ownership_conflict", "Run identity does not match supervisor evidence")
            if action == "resolve":
                if self.supervisor.snapshot(current["name"], full=True) is not None:
                    raise Fault("precondition", "Resolution requires a collected, absent service")
            elif current["blocked"]:
                observed = self.observations.get(current["run_id"])
                if not owns(current, observed):
                    raise Fault("outcome_unknown", "Cannot signal without current ownership evidence")
                if any(op["action"] == "stop" and op["run_id"] == current["run_id"] for op in self.operations.values()):
                    raise Fault("stop_already_requested", "This run already has a stop request; no automatic repeat is permitted")
        # A slow audit can consume the caller's expiry. Never dispatch an expired command.
        if self.wall() * 1000 >= expiry or self.clock() >= dispatch_deadline:
            raise Fault("expired", "Command expired during validation")
        operation = dict(request_id=rid, action=action, unit_id=unit_id, run_id=current["run_id"],
                         fingerprint=fingerprint, actor=actor, phase="prepared", delivery="unknown",
                         created_at_ms=int(self.wall() * 1000))
        if action == "stop":
            operation["deadline_monotonic"] = self.clock() + current["spec"]["stop"]["timeout_seconds"]
            operation["boot_id"] = self.boot
            current["stop_request_id"] = rid
        self.store.save(run=current, operation=operation)
        self.runs[current["run_id"]] = current
        self.operations[rid] = operation
        try:
            if action == "start":
                self.supervisor.start(current)
            elif action == "stop" and current["blocked"]:
                self.supervisor.signal(current)
            elif action == "resolve":
                self._save_run(current, blocked=False, lifecycle="resolved_missing")
                self._save_operation(operation, phase="complete", delivery="not_applicable", outcome="absence_resolved")
                self.changed()
                return public_operation(operation)
            else:
                self._save_operation(operation, phase="complete", delivery="not_needed", outcome="already_stopped")
                self.changed()
                return public_operation(operation)
            self._save_operation(operation, phase="in_progress", delivery="acknowledged")
            self.audit(full=True)
        except Exception:
            # Includes reply loss after dispatch. Never replay it during recovery.
            self._save_operation(operation, phase="outcome_unknown")
            self.error = "Command outcome needs reconciliation"
            self.changed()
        return public_operation(operation)

    def handle(self, request, actor="local"):
        if not isinstance(request, dict) or type(request.get("version")) is not int or request["version"] != 1:
            raise Fault("protocol_version", "Expected IPC version 1")
        action = request.get("action")
        if action in ("av_catalog", "av_target"):
            from .av_profile import catalog, current_target
            if action == "av_catalog":
                fields(request, ("version", "action"))
                return catalog(self)
            fields(request, ("version", "action", "capability_id"), ("fresh",))
            if type(request.get("fresh", False)) is not bool:
                raise Fault("invalid_request", "fresh must be a boolean")
            return current_target(self, text(request["capability_id"], 32), fresh=request.get("fresh", False))
        if isinstance(action, str) and action.startswith("ros_") and self.ros:
            return self.ros.handle(request, actor)
        if action in ("start", "stop", "resolve"):
            return self.command(request, actor)
        if action == "status":
            fields(request, ("version", "action"), ("fresh",))
            if type(request.get("fresh", False)) is not bool:
                raise Fault("invalid_request", "fresh must be a boolean")
            if request.get("fresh"):
                self.audit(full=True)
            return self.status()
        if action == "operation":
            fields(request, ("version", "action", "request_id"))
            operation = self.operations.get(request_id(request["request_id"]))
            if not operation:
                raise Fault("not_found", "No retained operation with this ID")
            return public_operation(operation)
        if action == "log_target":
            fields(request, ("version", "action", "run_id"))
            run = self.runs.get(text(request["run_id"], 32))
            if not run or not run.get("invocation"):
                raise Fault("not_found", "No retained supervisor invocation for this run")
            return dict(invocation=run["invocation"], boot_id=run["boot_id"], public_logs=run["spec"]["public_logs"])
        if action == "inspect":
            fields(request, ("version", "action", "run_id"))
            run = self.runs.get(text(request["run_id"], 32))
            if not run:
                raise Fault("not_found", "No retained run with this ID")
            return {k: run.get(k) for k in ("run_id", "unit_id", "name", "invocation", "boot_id", "lifecycle", "blocked", "config_revision")}
        raise Fault("invalid_request", "Unsupported action")
