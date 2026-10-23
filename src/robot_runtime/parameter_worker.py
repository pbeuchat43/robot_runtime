"""ROS parameter service transactions within the single native worker."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import time

from .common import Fault
from .ipc import request
from .parameter_codec import TYPES, bounded, changes, from_message, inspection


class ParameterWorker:
    def __init__(self, worker):
        self.worker = worker
        self.specs = {spec["id"]: spec for spec in worker.config.get("parameters", [])}
        self.task = None
        self.availability = {}
        self.types = {}
        if self.specs:
            from rcl_interfaces.srv import DescribeParameters, GetParameters, SetParametersAtomically
            self.types = dict(describe_parameters=DescribeParameters, get_parameters=GetParameters,
                              set_parameters_atomically=SetParametersAtomically)

    def target(self, spec, service):
        type_name = dict(describe_parameters="DescribeParameters", get_parameters="GetParameters",
                         set_parameters_atomically="SetParametersAtomically")[service]
        self.worker.service_target(dict(node=spec["node"], name=spec["node"] + "/" + service,
                                        type="rcl_interfaces/srv/" + type_name))

    def audit(self):
        values = {}
        for key, spec in self.specs.items():
            readable, writable, error = False, False, None
            try:
                self.target(spec, "get_parameters")
                self.target(spec, "describe_parameters")
                readable = True
                if any(item["write"] for item in spec["parameters"]):
                    self.target(spec, "set_parameters_atomically")
                    writable = True
            except Fault as exc:
                error = exc.code
            values[key] = dict(available=readable, writable=writable, error=error)
        if values != self.availability:
            self.availability = values
            self.worker.status()

    def emit(self, kind, **values):
        task = self.task
        self.worker.output.send(kind + ":" + task["id"], dict(kind=kind, generation=self.worker.generation,
            request_id=task["id"], **values))

    def clear_clients(self):
        for client, future in self.task["clients"].values():
            if future is not None:
                client.remove_pending_request(future)
                future.cancel()
            self.worker.node.destroy_client(client)
        self.task["clients"] = {}

    def client(self, service):
        task = self.task
        self.target(task["spec"], service)
        client = self.worker.node.create_client(self.types[service], task["spec"]["node"] + "/" + service)
        task["clients"][service] = (client, None)
        deadline = min(task["deadline"], time.monotonic() + 0.5)
        while not client.service_is_ready() and time.monotonic() < deadline:
            self.worker.rclpy.spin_once(self.worker.node, timeout_sec=0.02)
        if not client.service_is_ready():
            raise Fault("source_unavailable", "Parameter service client did not become ready")
        return client

    def send(self, service, body, client=None):
        client = client or self.client(service)
        if time.monotonic() >= self.task["deadline"]:
            raise Fault("response_deadline_exceeded", "Parameter request deadline exceeded")
        self.target(self.task["spec"], service)
        # Once call_async begins, a transport exception cannot prove no dispatch.
        if service == "set_parameters_atomically":
            self.task["sent"] = True
        self.task["clients"][service] = (client, client.call_async(body))

    def start(self, message):
        rid = message["request_id"]
        if self.task or self.worker.call:
            if message["kind"] == "parameter_read":
                self.worker.output.send("parameter_read:" + rid, dict(kind="parameter_read", generation=self.worker.generation,
                    request_id=rid, error="busy"))
            else:
                self.worker.operation(rid, phase="failed", delivery="not_sent", outcome="busy")
            return
        spec = self.specs[message["capability_id"]]
        write = message["kind"] == "parameter_set"
        self.task = dict(id=rid, spec=spec, write=write, stage="inspect", sent=False, accepted=False,
            message=message, clients={}, deadline=message["deadline"], names=[item["name"] for item in spec["parameters"]])
        try:
            for service in ("get_parameters", "describe_parameters"):
                self.send(service, self.types[service].Request(names=self.task["names"]))
        except Exception as exc:
            self.fail(exc)

    def fail(self, error):
        task = self.task
        code = error.code if isinstance(error, Fault) else "invalid_response"
        if task["accepted"]:
            self.emit("parameter_readback", readback=dict(status="unavailable", error=code))
        elif task["write"]:
            self.worker.operation(task["id"], phase="outcome_unknown" if task["sent"] else "failed",
                delivery="sent" if task["sent"] else "not_sent", outcome=code)
        else:
            self.emit("parameter_read", error=code)
        self.cancel(task["id"])

    def cancel(self, rid):
        if self.task and self.task["id"] == rid:
            self.clear_clients()
            self.task = None

    def progress(self):
        task = self.task
        if not task:
            return
        try:
            if time.monotonic() >= task["deadline"]:
                raise Fault("response_deadline_exceeded", "Parameter request deadline exceeded")
            if not all(future is not None and future.done() for client, future in task["clients"].values()):
                return
            responses = {name: future.result() for name, (client, future) in task["clients"].items()}
            self.clear_clients()
            spec, message = task["spec"], task["message"]
            permissions = {item["name"]: item["write"] for item in spec["parameters"]}
            if task["stage"] == "inspect":
                self.target(spec, "get_parameters")
                self.target(spec, "describe_parameters")
                observed = inspection(task["names"], responses["get_parameters"].values,
                                      responses["describe_parameters"].descriptors, permissions)
                if not task["write"]:
                    self.emit("parameter_read", parameters=observed, received_monotonic=time.monotonic())
                    self.cancel(task["id"])
                    return
                values = changes(message["changes"], observed, permissions)
                if any(observed[name] != expected for name, expected in message["baseline"].items()):
                    raise Fault("parameter_conflict", "A parameter changed since inspection; refresh before editing")
                from rcl_interfaces.msg import Parameter, ParameterValue
                parameters = []
                for name, value in values.items():
                    kind = value["type"]
                    body = ParameterValue(type=TYPES.index(kind))
                    data = value["value"]
                    if kind == "byte_array":
                        data = [bytes([item]) for item in data]
                    setattr(body, kind + "_value", data)
                    parameters.append(Parameter(name=name, value=body))
                service = "set_parameters_atomically"
                client = self.client(service)
                authority = request(self.worker.state, "ros_authorize", request_id=task["id"], generation=self.worker.generation)
                if time.monotonic() >= authority["dispatch_deadline"]:
                    raise Fault("expired", "Parameter command expired before dispatch")
                task["stage"] = "set"
                self.send(service, self.types[service].Request(parameters=parameters), client)
                self.worker.operation(task["id"], phase="in_progress", delivery="sent")
            elif task["stage"] == "set":
                result = responses["set_parameters_atomically"].result
                if type(result.successful) is not bool or not isinstance(result.reason, str):
                    raise Fault("invalid_response", "Invalid parameter provider result")
                response = dict(successful=result.successful, reason=result.reason[:1024], reason_truncated=len(result.reason) > 1024)
                task["accepted"] = result.successful
                self.worker.operation(task["id"], phase="complete", delivery="responded",
                    outcome="provider_accepted" if result.successful else "provider_rejected", response=response,
                    readback=dict(status="pending" if result.successful else "not_requested"))
                if not result.successful:
                    self.cancel(task["id"])
                    return
                task["stage"] = "readback"
                self.send("get_parameters", self.types["get_parameters"].Request(names=list(message["changes"])))
            else:
                self.target(spec, "get_parameters")
                values = responses["get_parameters"].values
                if len(values) != len(message["changes"]):
                    raise Fault("invalid_response", "Incomplete parameter readback")
                observed = bounded({name: from_message(value) for name, value in zip(message["changes"], values)})
                self.emit("parameter_readback", readback=dict(status="observed", parameters=observed,
                    matches_requested=observed == message["changes"], received_monotonic=time.monotonic()))
                self.cancel(task["id"])
        except Exception as exc:
            self.fail(exc)
