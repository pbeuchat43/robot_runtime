"""Finite journal pages; a log reader cannot block the manager event loop."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import os
import selectors
import subprocess
import time

from .common import Fault, dumps, loads, text


def capture(argv, maximum=262144):
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    data, error = bytearray(), bytearray()
    limited = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, data)
            selector.register(process.stderr, selectors.EVENT_READ, error)
            deadline = time.monotonic() + 3
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise Fault("logs_unavailable", "Journal read exceeded its deadline")
                for key, _event in selector.select(0.1):
                    part = os.read(key.fileobj.fileno(), 8192)
                    if not part:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(part)
                        if len(data) + len(error) > maximum:
                            limited = True
                            return bytes(data[:maximum]), limited, b""
        code = process.wait(timeout=1)
        if code:
            raise Fault("logs_unavailable", "Journal cursor or retained log data is unavailable; a gap may exist")
        return bytes(data), limited, bytes(error)
    finally:
        # Only this finite reader subprocess, never a managed workload.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        process.stderr.close()


def read(target, *, limit=100, cursor=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Fault("invalid_request", "Log limit must be 1–100")
    if cursor is not None:
        text(cursor, 1024)
        if "\n" in cursor or "\r" in cursor:
            raise Fault("invalid_request", "Invalid journal cursor")
    identity = ["_SYSTEMD_INVOCATION_ID=" + target["invocation"], "_BOOT_ID=" + target["boot_id"].replace("-", "")]
    gap = None
    if cursor:
        check, check_limited, _warnings = capture(["journalctl", "--no-pager", "--output=json", "--output-fields=__CURSOR",
                                                   "--lines=+1", "--cursor=" + cursor, *identity], maximum=16384)
        first = check.splitlines()
        if check_limited or not first or loads(first[0]).get("__CURSOR") != cursor:
            gap = "cursor_not_retained_or_not_in_this_run"
    argv = ["journalctl", "--no-pager", "--output=json", "--output-fields=MESSAGE", "--all",
            "--lines=" + ("+" if cursor else "") + str(limit + 1),
            *identity]
    if cursor:
        argv.append("--after-cursor=" + cursor)
    raw, limited, warnings = capture(argv)
    records = []
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            limited = True
            break
        record = loads(line)
        message = record.get("MESSAGE", "")
        if isinstance(message, list):
            message = bytes(message).decode("utf-8", errors="replace")
        message = str(message)
        records.append(dict(cursor=record["__CURSOR"], realtime_us=str(record["__REALTIME_TIMESTAMP"]),
                            message=message[:4096], message_truncated=len(message) > 4096))
    more = limited or len(records) > limit
    records = records[:limit] if cursor else records[-limit:]
    bounded = []
    size = 0
    for record in records:
        size += len(dumps(record).encode())
        if size > 65536:
            more = True
            break
        bounded.append(record)
    records = bounded
    return dict(records=records, next_cursor=records[-1]["cursor"] if records else cursor,
                truncated=more, gap=gap or ("journal reported limited access" if warnings else None),
                retention="host_journal_policy", history_complete=False)
