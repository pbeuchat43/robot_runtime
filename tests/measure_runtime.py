"""One bounded 20-unit/five-viewer cost capture. Run only through ssh jah."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiohttp

from robot_runtime.auth import write_credential
from robot_runtime.ipc import request
from integration_browser import port, wait_for

HZ = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def process(pid):
    raw = (Path("/proc") / str(pid) / "stat").read_text()
    values = raw[raw.rfind(")") + 2:].split()
    return dict(birth=int(values[19]), ticks=int(values[11]) + int(values[12]), rss_mib=int(values[21]) * PAGE / 1048576)


def infrastructure():
    result = {os.getpid(): "sampler_and_viewers"}
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = path.read_text().strip()
            if name in ("systemd", "dbus-daemon", "dbus-broker", "systemd-journal"):
                result[int(path.parent.name)] = name
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            pass
    return result


async def capture(label, pids, seconds):
    await asyncio.sleep(10)
    start = time.monotonic()
    initial = {pid: process(pid) for pid in pids}
    samples = []
    for offset in range(seconds + 1):
        await asyncio.sleep(max(0, start + offset - time.monotonic()))
        values = {pid: process(pid) for pid in pids}
        assert all(values[pid]["birth"] == initial[pid]["birth"] for pid in pids), "PID replacement invalidates capture"
        samples.append(dict(monotonic=time.monotonic(), processes=values))
    elapsed = samples[-1]["monotonic"] - samples[0]["monotonic"]
    summary = {pid: dict(role=role, cpu_one_core_percent=(samples[-1]["processes"][pid]["ticks"] - samples[0]["processes"][pid]["ticks"]) / HZ / elapsed * 100,
                         max_rss_mib=max(sample["processes"][pid]["rss_mib"] for sample in samples)) for pid, role in pids.items()}
    management = [value for value in summary.values() if value["role"] != "fixture"]
    result = dict(label=label, duration_seconds=elapsed, summary=summary, samples=samples,
                  conservative_cpu_one_core_percent=sum(value["cpu_one_core_percent"] for value in management),
                  conservative_max_rss_mib=sum(value["max_rss_mib"] for value in management))
    print(json.dumps({k: result[k] for k in ("label", "conservative_cpu_one_core_percent", "conservative_max_rss_mib")}), flush=True)
    return result


async def main(root, seconds, resources=False):
    root = root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-slice-[0-9a-f]{12}", root.name)
    token = uuid.uuid4().hex[:10]
    area = root / ("measurement-" + token)
    area.mkdir(mode=0o700)
    state = area / "state"
    profile = dict(schema_version=1, robot_id="cost-fixture", namespace="m" + token, units=[])
    for index in range(20):
        profile["units"].append(dict(id="idle" + str(index), label="Idle " + str(index),
                                     argv=[sys.executable, str(Path(__file__).with_name("workload.py").resolve()), "benchmark", str(area / ("idle" + str(index) + ".jsonl"))],
                                     working_directory=str(area), environment={}, stop=dict(signal="SIGINT", timeout_seconds=2), public_logs=False))
    (area / "profile.json").write_text(json.dumps(profile))
    write_credential(area / "credential.json", secrets.token_urlsafe(24))
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(area / "key.pem"),
                    "-out", str(area / "cert.pem"), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.chmod(area / "key.pem", 0o600)
    context = ssl.create_default_context(cafile=str(area / "cert.pem"))
    listen = port()
    origin = "https://localhost:" + str(listen)
    output = (area / "services.log").open("w")
    manager = subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(area / "profile.json"), "--state", str(state)], stdout=output, stderr=output)
    web = None
    viewers = []
    drains = []
    report = dict(token=token, seconds=seconds, warmup_seconds=10, tick_hz=HZ, boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                  phases=[], source_sha256={str(p.relative_to(Path(__file__).resolve().parent.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in sorted(Path(__file__).resolve().parent.parent.joinpath("src").rglob("*")) if p.is_file() and "__pycache__" not in p.parts},
                  power=subprocess.check_output(["nvpmodel", "-q"], text=True).strip(),
                  scope="20 finite idle Python workloads; five SSE clients in the sampler process; no renderer, ROS or hardware")

    async def local(action, **values):
        return await asyncio.to_thread(request, state, action, **values)

    async def ready():
        return (await local("status"))["fresh"]

    async def stop_all():
        status = await local("status", fresh=True)
        for unit in status["units"]:
            if unit["blocked"] and not unit.get("stop_request_id"):
                await local("stop", unit_id=unit["unit_id"], request_id=str(uuid.uuid4()), config_revision=status["config_revision"],
                            expires_at_ms=int(time.time() * 1000) + 10000, expected_run=unit["run_id"])
        async def empty():
            return not any(unit["blocked"] for unit in (await local("status", fresh=True))["units"])
        await wait_for(empty)

    try:
        await wait_for(ready)
        web = subprocess.Popen([sys.executable, "-m", "robot_runtime.web", "--state", str(state), "--origin", origin,
                                "--credential", str(area / "credential.json"), "--cert", str(area / "cert.pem"), "--key", str(area / "key.pem"),
                                "--bind", "127.0.0.1", "--port", str(listen)], stdout=output, stderr=output)
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as client:
            async def web_ready():
                async with client.get(origin + "/api/status") as response:
                    return response.status == 200 and (await response.json())["fresh"]
            await wait_for(web_ready)
            known = infrastructure()
            known.update({manager.pid: "runtime_manager", web.pid: "web_adapter"})
            if not resources:
                report["phases"].append(await capture("empty_manager_web", known, seconds))
            revision = (await local("status"))["config_revision"]
            for index in range(20):
                await local("start", unit_id="idle" + str(index), request_id=str(uuid.uuid4()), config_revision=revision,
                            expires_at_ms=int(time.time() * 1000) + 10000)
            status = await local("status", fresh=True)
            assert all(unit["lifecycle"] == "running" and len(unit["members"]) == 1 for unit in status["units"])
            loaded = dict(known)
            loaded.update({unit["members"][0]: "fixture" for unit in status["units"]})
            report["phases"].append(await capture("20_units_no_viewers", loaded, seconds))
            for index in range(5):
                response = await client.get(origin + ("/api/resources/events" if resources else "/api/events"))
                assert response.status == 200
                viewers.append(response)
                assert (await response.content.readline()).startswith(b"data: ")
                if resources:
                    async def drain(stream):
                        async for _line in stream.content:
                            pass
                    drains.append(asyncio.create_task(drain(response)))
                if resources and index == 0:
                    async def telemetry():
                        value = await local("resource_status")
                        return value.get("latest") and value["latest"]["gpu"]["activity_percent"] is not None
                    await wait_for(telemetry)
                    # This Jetson kernel omits task/*/children. Discover the
                    # owned helper once using PPID and its exact executable.
                    helpers = []
                    executable = Path(shutil.which("tegrastats")).resolve()
                    for file in Path("/proc").glob("[0-9]*/stat"):
                        try:
                            fields = file.read_text().rsplit(")", 1)[1].split()
                            if int(fields[1]) == manager.pid and file.with_name("exe").resolve() == executable:
                                helpers.append(int(file.parent.name))
                        except (FileNotFoundError, PermissionError, ProcessLookupError):
                            pass
                    assert len(helpers) == 1
                    loaded[helpers[0]] = "gpu_telemetry"
                    report["phases"].append(await capture("20_units_one_resource_viewer", loaded, seconds))
            # Each stream sends at most a coalesced state every audit here; buffered
            # data remains bounded below the client's transport high-water mark.
            report["phases"].append(await capture("20_units_five_resource_viewers" if resources else "20_units_five_viewers", loaded, seconds))
            if resources:
                assert all(not task.done() for task in drains)
                assert (await local("resource_status"))["consumers"] == 1
            for response in viewers:
                response.close()
            viewers.clear()
            if resources:
                async def released():
                    value = await local("resource_status")
                    return not value["collecting"] and not Path(f"/proc/{helpers[0]}").exists()
                await wait_for(released)
                report["resource_demand_released"] = True
                report["scope"] = "20 finite idle Python workloads; zero/one/five resource SSE clients in sampler process; includes owned GPU telemetry helper; no renderer, ROS or hardware workload"
            latencies = []
            for _ in range(50):
                start = time.monotonic()
                async with client.get(origin + "/api/status") as response:
                    assert response.status == 200 and (await response.json())["fresh"]
                latencies.append((time.monotonic() - start) * 1000)
                await asyncio.sleep(0.02)
            report["cached_https_read_ms"] = dict(samples=latencies, p95=sorted(latencies)[47])
            await stop_all()
            if not resources:
                report["phases"].append(await capture("return_empty_manager_web", known, seconds))
        report["passed"] = True
    except BaseException as exc:
        report.update(passed=False, error=repr(exc))
        raise
    finally:
        errors = []
        for response in viewers:
            response.close()
        for task in drains:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, aiohttp.ClientError):
                await task
        try:
            await stop_all()
            report["final_status"] = await local("status", fresh=True)
        except Exception as exc:
            errors.append(repr(exc))
        for process_handle in (web, manager):
            if process_handle and process_handle.poll() is None:
                process_handle.terminate()
                try:
                    await asyncio.to_thread(process_handle.wait, timeout=10)
                except subprocess.TimeoutExpired as exc:
                    errors.append(repr(exc))
        output.close()
        report["cleanup_errors"] = errors
        (area / "report.json").write_text(json.dumps(report, indent=2))
        (root / "latest-measurement.txt").write_text(area.name)
        if errors:
            raise RuntimeError("Inspect measurement cleanup report")
    print(json.dumps(dict(passed=report["passed"], path=str(area / "report.json")), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--seconds", type=int, default=60, choices=(30, 60, 90))
    parser.add_argument("--resources", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.root, args.seconds, resources=args.resources))
