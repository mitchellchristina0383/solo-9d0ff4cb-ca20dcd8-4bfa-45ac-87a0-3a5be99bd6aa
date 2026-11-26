#!/usr/bin/env python3
"""Live HTTP smoke test for a running docker compose / Flask server.

It deliberately unplugs and reinserts a link while failover and switchback
timers are pending, then verifies the final switchback and loop-free reach.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")


def request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_ready(timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.15)
    raise RuntimeError(f"server did not become ready: {last_error!r}")


def state() -> dict[str, Any]:
    return request("GET", "/api/state")


def link_state(data: dict[str, Any], link_id: str) -> str:
    return next(link["state"] for link in data["links"] if link["id"] == link_id)


def pending_command(data: dict[str, Any], link_id: str, action: str) -> dict[str, Any] | None:
    return next(
        (
            command
            for command in data["commands"]
            if command["link_id"] == link_id
            and command["action"] == action
            and command["status"] == "pending"
        ),
        None,
    )


def wait_for(predicate, timeout: float = 8.0, interval: float = 0.05) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = state()
    while time.time() < deadline:
        last = state()
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for condition; last state={json.dumps(last, ensure_ascii=False)[:2000]}")


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> int:
    wait_ready()
    request("POST", "/api/reset")

    for link_id in ("l23", "l41"):
        request(
            "PATCH",
            f"/api/links/{link_id}/delays",
            {"open_delay_seconds": 1.2 if link_id == "l41" else 0.5, "block_delay_seconds": 0.8},
        )

    probe = request("POST", "/api/broadcast", {"source": "s1"})["last_probe"]
    assert_equal(set(probe["reached"]), {"s1", "s2", "s3", "s4"}, "initial broadcast")
    assert "l41" not in probe["forwarding_link_ids"]

    # Failure: forwarding stops immediately and backup open is scheduled.
    data = request("POST", "/api/links/l23/unplug")
    assert_equal(link_state(data, "l23"), "down", "unplug immediate state")
    backup_open = pending_command(data, "l41", "open")
    assert backup_open is not None, "backup open command was issued"
    original_open_deadline = backup_open["execute_at_clock_ms"]

    probe = request("POST", "/api/broadcast", {"source": "s1"})["last_probe"]
    assert_equal(set(probe["reached"]), {"s1", "s2"}, "partition during failover")

    # Plug back during the initial switchover delay.
    time.sleep(0.25)
    data = request("POST", "/api/links/l23/plug")
    assert_equal(link_state(data, "l23"), "blocked", "reinserted link stays blocked")
    backup_open = pending_command(data, "l41", "open")
    assert backup_open is not None and backup_open["execute_at_clock_ms"] == original_open_deadline

    # Wait for the original backup-open deadline, then capture switchback block.
    data = wait_for(lambda d: link_state(d, "l41") == "forwarding")
    block = wait_for(lambda d: pending_command(d, "l41", "block") is not None)
    block_command = pending_command(block, "l41", "block")
    assert block_command is not None
    original_block_deadline = block_command["execute_at_clock_ms"]

    # Unplug and replug again while the switchback block command is pending.
    time.sleep(0.2)
    request("POST", "/api/links/l23/unplug")
    time.sleep(0.15)
    data = request("POST", "/api/links/l23/plug")
    same_block = next(command for command in data["commands"] if command["id"] == block_command["id"])
    assert_equal(same_block["status"], "pending", "issued block remains pending")
    assert_equal(same_block["execute_at_clock_ms"], original_block_deadline, "block deadline unchanged")

    # Eventually the old block executes and L23 opens safely.
    data = wait_for(
        lambda d: link_state(d, "l23") == "forwarding" and link_state(d, "l41") == "blocked",
        timeout=8,
    )
    assert data["topology"]["loop_free"] is True

    probe = request("POST", "/api/broadcast", {"source": "s4"})["last_probe"]
    assert_equal(set(probe["reached"]), {"s1", "s2", "s3", "s4"}, "final broadcast")
    assert_equal(set(probe["forwarding_link_ids"]), {"l12", "l23", "l34"}, "final forwarding links")
    assert probe["loop_free"] is True

    print("LIVE SMOKE PASS")
    print(json.dumps({
        "original_backup_open_deadline_ms": original_open_deadline,
        "original_block_deadline_ms": original_block_deadline,
        "final_forwarding_link_ids": probe["forwarding_link_ids"],
        "reached": probe["reached_names"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, urllib.error.URLError, TimeoutError) as exc:
        print(f"LIVE SMOKE FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
