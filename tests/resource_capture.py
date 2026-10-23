"""Bounded Linux resource sampling for opt-in jah measurements, not a service."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import os
from pathlib import Path
import time

HZ = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def process(pid):
    raw = Path(f"/proc/{pid}/stat").read_text()
    parts = raw[raw.rfind(")") + 2:].split()
    return dict(birth=int(parts[19]), cpu_seconds=(int(parts[11])+int(parts[12]))/HZ,
                rss_bytes=int(parts[21])*PAGE)


def service_group(name):
    import subprocess
    relative = subprocess.check_output(["systemctl", "--user", "show", name, "-p", "ControlGroup", "--value"], text=True).strip()
    assert relative.startswith("/user.slice/") and ".." not in relative
    path = Path("/sys/fs/cgroup") / relative.lstrip("/")
    assert path.is_dir(), name
    return path


def group(path):
    cpu = dict(line.split() for line in (path / "cpu.stat").read_text().splitlines())
    pids = {int(pid) for file in path.rglob("cgroup.procs") for pid in file.read_text().split()}
    values = {}
    for pid in pids:
        try: values[str(pid)] = process(pid)
        except (FileNotFoundError, ProcessLookupError): pass
    return dict(cpu_seconds=int(cpu["usage_usec"])/1e6, memory_current_bytes=int((path / "memory.current").read_text()),
                rss_bytes=sum(value["rss_bytes"] for value in values.values()), processes=values)


def sample(groups, extra_pids):
    start = time.process_time_ns()
    raw = Path("/proc/stat").read_text().splitlines()[0].split()
    cpu = list(map(int, raw[1:9]))
    memory = {key.rstrip(":"):int(value.split()[0])*1024 for key,value in
              (line.split(":",1) for line in Path("/proc/meminfo").read_text().splitlines())}
    result = dict(monotonic=time.monotonic(), host_cpu_total_ticks=sum(cpu), host_cpu_idle_ticks=cpu[3]+cpu[4],
        host_memory={key:memory[key] for key in ("MemTotal","MemAvailable","SwapTotal","SwapFree")},
        groups={key:group(path) for key,path in groups.items()},
        processes={role:process(pid) for role,pid in extra_pids.items()})
    result["collection_cpu_ms"] = (time.process_time_ns()-start)/1e6
    return result


def summarize(samples):
    first,last=samples[0],samples[-1]
    elapsed=last["monotonic"]-first["monotonic"]
    ticks=last["host_cpu_total_ticks"]-first["host_cpu_total_ticks"]
    idle=last["host_cpu_idle_ticks"]-first["host_cpu_idle_ticks"]
    result=dict(duration_seconds=elapsed,samples=len(samples),host_cpu_percent_all_cores=100*(ticks-idle)/ticks,
        host_used_memory_peak_mib=max(s["host_memory"]["MemTotal"]-s["host_memory"]["MemAvailable"] for s in samples)/1048576,
        host_swap_used_peak_mib=max(s["host_memory"]["SwapTotal"]-s["host_memory"]["SwapFree"] for s in samples)/1048576,
        groups={},processes={},collection_cpu_one_core_percent=sum(s["collection_cpu_ms"] for s in samples)/elapsed/10)
    for section in ("groups","processes"):
        for name,value in first[section].items():
            if section == "processes":
                assert all(s[section][name]["birth"]==value["birth"] for s in samples),"PID replacement"
            result[section][name]=dict(cpu_one_core_percent=(last[section][name]["cpu_seconds"]-value["cpu_seconds"])/elapsed*100,
                peak_rss_mib=max(s[section][name]["rss_bytes"] for s in samples)/1048576)
            if section == "groups":
                result[section][name]["peak_charged_memory_mib"]=max(s[section][name]["memory_current_bytes"] for s in samples)/1048576
    return result
