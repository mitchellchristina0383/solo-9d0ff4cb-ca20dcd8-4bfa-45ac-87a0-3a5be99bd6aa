#!/usr/bin/env python3
"""Live HTTP smoke test for bandwidth reservation / fill-after-revoke.

Run against a server started with Docker Compose or ``python wsgi.py``:

    BASE_URL=http://127.0.0.1:5000 python scripts/smoke_services_live.py

It verifies, over real HTTP:

1. integer-Mbps registrations admit only when the whole actual path fits;
2. bidirectional traffic shares the 100 Mbps per-link bucket;
3. a waiting head-of-line service does not block later registrations;
4. ring switchover triggers contention and correct re-admission order;
5. revoking an active service backfills the waiting one;
6. reset clears every service.
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
CAPACITY = 100


def request(method: str, path: str, body: dict[str, Any] | None = None, allow_error: bool = False):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if allow_error:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        raise


def wait_ready(timeout: float = 12.0) -> None:
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


def check(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} :: {json.dumps(detail, ensure_ascii=False) if detail is not None else ''}")
    print(f"  PASS  {label}")


def by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {service["id"]: service for service in data["services"]}


def link_usage(data: dict[str, Any], link_id: str) -> tuple[int, int]:
    link = next(link for link in data["links"] if link["id"] == link_id)
    return link["reserved_mbps"], link["available_mbps"]


def main() -> int:
    wait_ready()
    request("POST", "/api/reset")

    # Zero failover delays so unplug synchronously opens the backup.
    for link_id in ("l12", "l23", "l34", "l41"):
        status, _ = request(
            "PATCH",
            f"/api/links/{link_id}/delays",
            {"open_delay_seconds": 0, "block_delay_seconds": 0},
        )
        assert status == 200

    print("[1] registration and whole-path admission")
    status, data = request(
        "POST", "/api/services", {"source": "s2", "target": "s3", "demand_mbps": 60}
    )
    check(status == 200, "POST /api/services returns 200", status)
    check(by_id(data)["svc1"]["status"] == "reserved", "svc1 S2->S3 60M reserved on L23")
    check(by_id(data)["svc1"]["path_link_ids"] == ["l23"], "svc1 path is L23")

    _, data = request("POST", "/api/services", {"source": "s1", "target": "s3", "demand_mbps": 50})
    rows = by_id(data)
    check(rows["svc2"]["status"] == "waiting", "svc2 S1->S3 50M waits (L23 only has 40M left)")
    check("L23" in (rows["svc2"]["waiting_reason"] or ""), "waiting reason names bottleneck L23")
    check(link_usage(data, "l23") == (60, 40), "L23 usage is 60/100 despite failed svc2")

    _, data = request("POST", "/api/services", {"source": "s1", "target": "s2", "demand_mbps": 40})
    rows = by_id(data)
    check(rows["svc3"]["status"] == "reserved", "later svc3 S1->S2 40M still admitted (no HoL block)")
    check(rows["svc2"]["status"] == "waiting", "svc2 remains waiting")

    print("[2] bidirectional sharing")
    _, data = request("POST", "/api/services", {"source": "s3", "target": "s2", "demand_mbps": 41})
    rows = by_id(data)
    check(rows["svc4"]["status"] == "waiting", "reverse S3->S2 41M waits on shared L23 bucket")
    check(link_usage(data, "l23") == (60, 40), "reverse waiter occupies nothing")
    request("DELETE", "/api/services/svc4")

    print("[3] validation")
    for bad in (
        {"source": "s1", "target": "s1", "demand_mbps": 10},
        {"source": "s1", "target": "s2", "demand_mbps": 0},
        {"source": "s1", "target": "s2", "demand_mbps": 2.5},
        {"source": "s1", "target": "s2", "demand_mbps": "abc"},
    ):
        status, body = request("POST", "/api/services", bad, allow_error=True)
        check(status == 400 and bool(body.get("error")), f"bad request rejected: {bad}", (status, body))

    print("[4] switchover contention: unplug L23 -> L41 forwards")
    _, data = request("POST", "/api/links/l23/unplug")
    rows = by_id(data)
    check(next(link for link in data["links"] if link["id"] == "l41")["state"] == "forwarding",
          "L41 is forwarding after zero-delay failover")
    check(rows["svc3"]["status"] == "reserved" and rows["svc3"]["path_link_ids"] == ["l12"],
          "svc3 unchanged L12 path kept")
    check(rows["svc1"]["status"] == "reserved"
          and rows["svc1"]["path_link_ids"] == ["l12", "l41", "l34"],
          "svc1 re-admitted in order over failover path")
    check(rows["svc2"]["status"] == "waiting", "svc2 loses contention after switchover")
    check("L41" in (rows["svc2"]["waiting_reason"] or ""), "svc2 reason names L41 bottleneck")
    check(link_usage(data, "l12") == (100, 0), "L12 exactly saturated by svc3+svc1")
    check(link_usage(data, "l41") == (60, 40), "L41 carries svc1 60M")

    print("[5] revoke svc1 -> waiting svc2 backfills")
    _, data = request("DELETE", "/api/services/svc1")
    rows = by_id(data)
    check("svc1" not in rows, "svc1 removed")
    check(rows["svc2"]["status"] == "reserved"
          and rows["svc2"]["path_link_ids"] == ["l41", "l34"],
          "svc2 now reserved over L41+L34")
    check(rows["svc3"]["status"] == "reserved", "svc3 untouched")
    check(link_usage(data, "l41") == (50, 50), "L41 freed then occupied by svc2 50M")

    print("[6] reset clears services")
    _, data = request("POST", "/api/reset")
    check(data["services"] == [], "no services after reset")
    check(data["service_summary"]["total"] == 0, "summary reset")
    check(all(link["reserved_mbps"] == 0 for link in data["links"]), "all link quotas zero")

    print("SERVICE SMOKE PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, urllib.error.URLError, TimeoutError) as exc:
        print(f"SERVICE SMOKE FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
