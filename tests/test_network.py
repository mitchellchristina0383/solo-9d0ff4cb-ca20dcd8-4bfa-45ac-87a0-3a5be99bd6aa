from __future__ import annotations

import random
import sys
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.network import NetworkEngine  # noqa: E402


class FakeClock:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, ms: int) -> None:
        self.value += ms


def make_engine() -> tuple[NetworkEngine, FakeClock]:
    clock = FakeClock(1_000)
    return NetworkEngine(now_func=clock), clock


def reached_component(engine: NetworkEngine, source: str) -> set[str]:
    snapshot = engine.snapshot()
    adjacency: dict[str, set[str]] = defaultdict(set)
    for link in snapshot["links"]:
        if link["state"] == "forwarding":
            adjacency[link["source"]].add(link["target"])
            adjacency[link["target"]].add(link["source"])
    seen = {source}
    stack = [source]
    while stack:
        node = stack.pop()
        for neighbor in adjacency[node]:
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    return seen


def assert_forest_and_probe_matches(engine: NetworkEngine) -> None:
    snapshot = engine.snapshot()
    assert snapshot["topology"]["loop_free"] is True

    parent = {switch["id"]: switch["id"] for switch in snapshot["switches"]}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for link in snapshot["links"]:
        if link["state"] != "forwarding":
            continue
        a, b = find(link["source"]), find(link["target"])
        assert a != b, f"forwarding cycle at {link['id']}"
        parent[b] = a

    for source in ("s1", "s2", "s3", "s4"):
        probe = engine.send_broadcast(source)
        expected = reached_component(engine, source)
        assert set(probe["reached"]) == expected
        assert probe["loop_free"] is True


def test_initial_ring_blocks_designated_backup_and_broadcast_reaches_all() -> None:
    engine, _ = make_engine()
    snapshot = engine.snapshot()

    states = {link["id"]: link["state"] for link in snapshot["links"]}
    assert states == {"l12": "forwarding", "l23": "forwarding", "l34": "forwarding", "l41": "blocked"}
    assert snapshot["topology"]["loop_free"] is True

    probe = engine.send_broadcast("s1")
    assert set(probe["reached"]) == {"s1", "s2", "s3", "s4"}
    assert set(probe["forwarding_link_ids"]) == {"l12", "l23", "l34"}
    assert "l41" not in probe["forwarding_link_ids"]


def test_unplug_stops_immediately_then_backup_opens_at_original_deadline() -> None:
    engine, clock = make_engine()
    engine.set_delays("l41", open_delay_ms=5000, block_delay_ms=2000)
    engine.set_delays("l23", open_delay_ms=3000)

    snapshot = engine.unplug("l23")
    l23 = next(link for link in snapshot["links"] if link["id"] == "l23")
    assert l23["state"] == "down"

    backup_command = next(command for command in snapshot["commands"] if command["link_id"] == "l41")
    assert backup_command["status"] == "pending"
    assert backup_command["execute_at_clock_ms"] == 5000

    probe = engine.send_broadcast("s1")
    assert set(probe["reached"]) == {"s1", "s2"}
    assert set(probe["unreached"]) == {"s3", "s4"}

    clock.advance(4999)
    engine.process_due_commands()
    assert engine._link_locked("l41")["state"] == "blocked"

    clock.advance(1)
    executed = engine.process_due_commands()
    assert any(command["id"] == backup_command["id"] and command["result"] == "forwarding" for command in executed)
    assert engine._link_locked("l41")["state"] == "forwarding"

    probe = engine.send_broadcast("s2")
    assert set(probe["reached"]) == {"s1", "s2", "s3", "s4"}
    assert set(probe["forwarding_link_ids"]) == {"l12", "l34", "l41"}
    assert_forest_and_probe_matches(engine)


def test_repair_waits_for_block_delay_then_opens_original_line() -> None:
    engine, clock = make_engine()
    engine.set_delays("l41", open_delay_ms=100, block_delay_ms=2000)
    engine.set_delays("l23", open_delay_ms=3000)

    engine.unplug("l23")
    clock.advance(100)
    engine.process_due_commands()
    assert engine._link_locked("l41")["state"] == "forwarding"

    clock.advance(100)  # t=200 relative drill clock
    snapshot = engine.plug("l23")
    l23 = next(link for link in snapshot["links"] if link["id"] == "l23")
    assert l23["state"] == "blocked"

    block_command = next(
        command
        for command in snapshot["commands"]
        if command["link_id"] == "l41" and command["action"] == "block" and command["status"] == "pending"
    )
    assert block_command["execute_at_clock_ms"] == 2200

    # Before the block deadline the old backup remains forward, while the
    # reinserted original port stays blocked: there is no loop.
    clock.value = engine._start_ms + 2199
    engine.process_due_commands()
    assert engine._link_locked("l41")["state"] == "forwarding"
    assert engine._link_locked("l23")["state"] == "blocked"
    assert engine.snapshot()["topology"]["loop_free"] is True

    clock.value = engine._start_ms + 2200
    engine.process_due_commands()
    assert engine._link_locked("l41")["state"] == "blocked"
    open_command = next(
        command
        for command in engine.snapshot()["commands"]
        if command["link_id"] == "l23" and command["action"] == "open" and command["status"] == "pending"
    )
    assert open_command["execute_at_clock_ms"] == 5200

    clock.value = engine._start_ms + 5200
    engine.process_due_commands()
    final = engine.snapshot()
    states = {link["id"]: link["state"] for link in final["links"]}
    assert states == {"l12": "forwarding", "l23": "forwarding", "l34": "forwarding", "l41": "blocked"}
    assert_forest_and_probe_matches(engine)


def test_replug_during_failover_keeps_original_open_deadline_then_switches_back() -> None:
    engine, clock = make_engine()
    engine.set_delays("l41", open_delay_ms=10_000, block_delay_ms=2_000)
    engine.set_delays("l23", open_delay_ms=1_000)

    engine.unplug("l23")
    open_backup = next(c for c in engine.snapshot()["commands"] if c["link_id"] == "l41")
    assert open_backup["execute_at_clock_ms"] == 10_000

    # Reinsert during the long switchover delay.
    clock.value = engine._start_ms + 1_000
    snapshot = engine.plug("l23")
    assert next(link for link in snapshot["links"] if link["id"] == "l23")["state"] == "blocked"

    # The planner does not race a conflicting original-line open ahead of the
    # already-issued backup command, so its original deadline remains visible.
    assert not any(
        command["status"] == "pending" and command["link_id"] == "l23" and command["action"] == "open"
        for command in snapshot["commands"]
    )
    open_backup = next(command for command in snapshot["commands"] if command["id"] == open_backup["id"])
    assert open_backup["status"] == "pending"
    assert open_backup["execute_at_clock_ms"] == 10_000

    clock.value = engine._start_ms + 9_999
    engine.process_due_commands()
    assert engine._link_locked("l41")["state"] == "blocked"

    clock.value = engine._start_ms + 10_000
    engine.process_due_commands()
    open_backup = next(command for command in engine.snapshot()["commands"] if command["id"] == open_backup["id"])
    assert open_backup["status"] == "executed"
    assert open_backup["result"] == "forwarding"
    assert engine._link_locked("l41")["state"] == "forwarding"

    clock.value = engine._start_ms + 12_000
    engine.process_due_commands()
    assert engine._link_locked("l41")["state"] == "blocked"

    clock.value = engine._start_ms + 13_000
    engine.process_due_commands()
    assert engine._link_locked("l23")["state"] == "forwarding"
    assert engine.snapshot()["topology"]["loop_free"] is True
    probe = engine.send_broadcast("s4")
    assert set(probe["reached"]) == {"s1", "s2", "s3", "s4"}


def test_unplug_and_replug_during_block_delay_honors_block_then_restores() -> None:
    engine, clock = make_engine()
    engine.set_delays("l41", open_delay_ms=100, block_delay_ms=5_000)
    engine.set_delays("l23", open_delay_ms=3_000)

    engine.unplug("l23")
    clock.advance(100)
    engine.process_due_commands()
    assert engine._link_locked("l41")["state"] == "forwarding"

    clock.advance(100)
    engine.plug("l23")
    block_command = next(
        c for c in engine.snapshot()["commands"] if c["link_id"] == "l41" and c["action"] == "block" and c["status"] == "pending"
    )
    original_block_deadline = block_command["execute_at_clock_ms"]
    assert original_block_deadline == 5200

    # Operator changes their mind during the blocking delay, then repairs again.
    clock.value = engine._start_ms + 1_000
    engine.unplug("l23")
    clock.value = engine._start_ms + 2_000
    engine.plug("l23")

    block_command = next(c for c in engine.snapshot()["commands"] if c["id"] == block_command["id"])
    assert block_command["status"] == "pending"
    assert block_command["execute_at_clock_ms"] == original_block_deadline

    clock.value = engine._start_ms + original_block_deadline
    engine.process_due_commands()
    block_command = next(c for c in engine.snapshot()["commands"] if c["id"] == block_command["id"])
    assert block_command["status"] == "executed"
    assert engine._link_locked("l41")["state"] == "blocked"

    # The second plug is still blocked; after the old block job completes the
    # planner safely opens L23 using its configured open delay.
    open_l23 = next(
        c for c in engine.snapshot()["commands"] if c["link_id"] == "l23" and c["action"] == "open" and c["status"] == "pending"
    )
    clock.value = engine._start_ms + open_l23["execute_at_clock_ms"]
    engine.process_due_commands()
    assert engine._link_locked("l23")["state"] == "forwarding"
    assert_forest_and_probe_matches(engine)


def test_delay_change_does_not_reschedule_issued_command() -> None:
    engine, _ = make_engine()
    engine.set_delays("l41", open_delay_ms=5000)
    engine.unplug("l23")
    command = next(c for c in engine.snapshot()["commands"] if c["link_id"] == "l41")
    assert command["execute_at_clock_ms"] == 5000

    engine.set_delays("l41", open_delay_ms=100)
    command = next(c for c in engine.snapshot()["commands"] if c["id"] == command["id"])
    assert command["execute_at_clock_ms"] == 5000
    assert engine._link_locked("l41")["open_delay_ms"] == 100


def test_random_plug_events_never_create_forwarding_loop() -> None:
    random.seed(20261126)
    engine, clock = make_engine()
    link_ids = [link["id"] for link in engine.snapshot()["links"]]

    for step in range(500):
        clock.advance(random.randint(0, 250))
        engine.process_due_commands()

        action = random.random()
        if action < 0.55:
            link_id = random.choice(link_ids)
            link = engine._link_locked(link_id)
            engine.set_plugged(link_id, not link["plugged"])
        elif action < 0.75:
            link_id = random.choice(link_ids)
            engine.set_delays(
                link_id,
                open_delay_ms=random.choice([0, 50, 250, 1000, 5000]),
                block_delay_ms=random.choice([0, 50, 250, 1000, 5000]),
            )
        elif action < 0.9:
            engine.set_backup(random.choice(link_ids))
        else:
            engine.send_broadcast(random.choice(["s1", "s2", "s3", "s4"]))

        assert_forest_and_probe_matches(engine)

    # Drain all zero/short timers and finish in a valid state.
    for _ in range(20):
        clock.advance(5000)
        engine.process_due_commands()
        assert_forest_and_probe_matches(engine)
