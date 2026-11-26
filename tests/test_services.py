from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.network import LINK_CAPACITY_MBPS, NetworkEngine  # noqa: E402


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


def zero_delays(engine: NetworkEngine) -> None:
    for link_id in ("l12", "l23", "l34", "l41"):
        engine.set_delays(link_id, open_delay_ms=0, block_delay_ms=0)


def services_by_id(engine: NetworkEngine) -> dict[str, dict]:
    return {service["id"]: service for service in engine.snapshot()["services"]}


def test_registration_returns_sequential_ids_and_registration_order() -> None:
    engine, _ = make_engine()
    first = engine.register_service("s1", "s2", 10)
    second = engine.register_service("s2", "s3", 20)
    ids = [service["id"] for service in first["services"]]
    assert ids == ["svc1"]
    assert [service["id"] for service in second["services"]] == ["svc1", "svc2"]
    assert [service["order"] for service in second["services"]] == [1, 2]


def test_reserves_unique_forwarding_path_and_reports_utilization() -> None:
    engine, _ = make_engine()
    snapshot = engine.register_service("s1", "s3", 50)
    service = snapshot["services"][0]
    assert service["status"] == "reserved"
    assert service["path_switch_ids"] == ["s1", "s2", "s3"]
    assert service["path_link_ids"] == ["l12", "l23"]
    assert service["waiting_reason"] is None

    links = {link["id"]: link for link in snapshot["links"]}
    assert links["l12"]["reserved_mbps"] == 50
    assert links["l23"]["reserved_mbps"] == 50
    assert links["l34"]["reserved_mbps"] == 0
    assert links["l41"]["reserved_mbps"] == 0
    assert links["l12"]["available_mbps"] == 50
    assert links["l12"]["capacity_mbps"] == LINK_CAPACITY_MBPS
    assert snapshot["service_summary"] == {"total": 1, "reserved": 1, "waiting": 0}


def test_reverse_direction_shares_one_link_capacity_bucket() -> None:
    engine, _ = make_engine()
    engine.register_service("s1", "s3", 50)
    # Reverse direction traverses the same links; capacity is shared both ways.
    snapshot = engine.register_service("s3", "s1", 51)
    second = snapshot["services"][1]
    assert second["status"] == "waiting"
    assert second["path_link_ids"] is None
    assert second["candidate_path_link_ids"] == ["l23", "l12"]
    assert "L12" in second["waiting_reason"] or "L23" in second["waiting_reason"]
    assert "50" in second["waiting_reason"]

    links = {link["id"]: link for link in snapshot["links"]}
    # The waiting service occupied nothing.
    assert links["l12"]["reserved_mbps"] == 50
    assert links["l23"]["reserved_mbps"] == 50

    # Exactly fitting residual capacity is admitted.
    engine2, _ = make_engine()
    engine2.register_service("s1", "s3", 50)
    exact = engine2.register_service("s3", "s1", 50)["services"][1]
    assert exact["status"] == "reserved"
    assert {link["id"]: link["reserved_mbps"] for link in engine2.snapshot()["links"]}["l12"] == 100


def test_whole_path_must_fit_and_leaves_no_partial_occupation() -> None:
    engine, _ = make_engine()
    engine.register_service("s1", "s2", 40)  # l12
    engine.register_service("s2", "s3", 40)  # l23
    good = engine.register_service("s1", "s3", 30)  # l12 + l23 both fit
    assert good["services"][2]["status"] == "reserved"

    blocked = engine.register_service("s1", "s3", 40)["services"][3]
    assert blocked["status"] == "waiting"
    assert "带宽不足" in blocked["waiting_reason"]

    links = {link["id"]: link for link in engine.snapshot()["links"]}
    # Failed request must not leave 40 Mbps on the first-checked link.
    assert links["l12"]["reserved_mbps"] == 70
    assert links["l23"]["reserved_mbps"] == 70
    assert links["l34"]["reserved_mbps"] == 0


def test_earlier_waiting_service_does_not_block_later_registrations() -> None:
    engine, _ = make_engine()
    huge = engine.register_service("s1", "s2", 150)["services"][0]
    assert huge["status"] == "waiting"
    assert "带宽不足" in huge["waiting_reason"]

    later = engine.register_service("s2", "s3", 30)["services"][1]
    assert later["status"] == "reserved"
    assert later["path_link_ids"] == ["l23"]

    rows = services_by_id(engine)
    assert rows[huge["id"]]["status"] == "waiting"


def test_stop_forwarding_releases_path_immediately_even_before_failover_timer() -> None:
    engine, _ = make_engine()  # default l41 open delay 1500 ms
    svc = engine.register_service("s2", "s3", 50)["services"][0]
    assert svc["path_link_ids"] == ["l23"]

    snapshot = engine.unplug("l23")  # immediate stop; backup not open yet
    row = next(service for service in snapshot["services"] if service["id"] == svc["id"])
    assert row["status"] == "waiting"
    assert row["path_link_ids"] is None
    assert row["notice"] and "失效" in row["notice"]
    assert "不在同一" in row["waiting_reason"] or "连通段" in row["waiting_reason"]

    for link in snapshot["links"]:
        assert link["reserved_mbps"] == 0
        assert link["available_mbps"] == 100


def test_start_forwarding_readmits_waiting_services_in_registration_order() -> None:
    engine, clock = make_engine()
    engine.unplug("l23")  # partitions {s1,s2} | {s3,s4}; l41 opens at +1500
    waiting = engine.register_service("s2", "s4", 30)["services"][0]
    assert waiting["status"] == "waiting"
    assert "无路可走" in waiting["waiting_reason"]

    clock.advance(1500)
    engine.process_due_commands()  # l41 starts forwarding
    row = services_by_id(engine)[waiting["id"]]
    assert row["status"] == "reserved"
    assert row["path_link_ids"] == ["l12", "l41"]
    assert row["waiting_reason"] is None
    assert row["notice"] is None


def test_contention_after_switchover_then_recovery_after_revoke() -> None:
    """Headline drill: contention after switch, fill after revocation."""

    engine, _ = make_engine()
    zero_delays(engine)

    svc_a = engine.register_service("s2", "s3", 60)["services"][0]
    svc_b = engine.register_service("s1", "s3", 50)["services"][1]
    svc_c = engine.register_service("s1", "s2", 40)["services"][2]

    rows = services_by_id(engine)
    assert rows[svc_a["id"]]["status"] == "reserved"
    assert rows[svc_a["id"]]["path_link_ids"] == ["l23"]
    assert rows[svc_b["id"]]["status"] == "waiting"  # l23 only has 40 left
    assert "L23" in rows[svc_b["id"]]["waiting_reason"]
    assert rows[svc_c["id"]]["status"] == "reserved"
    assert rows[svc_c["id"]]["path_link_ids"] == ["l12"]

    # Switchover: L23 stops, L41 starts forwarding in the same zero-delay drain.
    engine.unplug("l23")
    rows = services_by_id(engine)

    # Unrelated direct path is unchanged -> reservation kept.
    assert rows[svc_c["id"]]["status"] == "reserved"
    assert rows[svc_c["id"]]["path_link_ids"] == ["l12"]

    # A is older than B and gets the residual L12 capacity exactly.
    assert rows[svc_a["id"]]["status"] == "reserved"
    assert rows[svc_a["id"]]["path_link_ids"] == ["l12", "l41", "l34"]

    # B now loses the contention on L41 (60 of 100 already taken by A).
    assert rows[svc_b["id"]]["status"] == "waiting"
    assert "L41" in rows[svc_b["id"]]["waiting_reason"]

    links = {link["id"]: link for link in engine.snapshot()["links"]}
    assert links["l12"]["reserved_mbps"] == 100
    assert links["l41"]["reserved_mbps"] == 60
    assert links["l34"]["reserved_mbps"] == 60
    assert links["l23"]["reserved_mbps"] == 0

    # Revoke A: its quota is freed and waiting B fills the slot in order.
    engine.revoke_service(svc_a["id"])
    rows = services_by_id(engine)
    assert svc_a["id"] not in rows
    assert rows[svc_b["id"]]["status"] == "reserved"
    assert rows[svc_b["id"]]["path_link_ids"] == ["l41", "l34"]
    assert rows[svc_c["id"]]["status"] == "reserved"

    links = {link["id"]: link for link in engine.snapshot()["links"]}
    assert links["l41"]["reserved_mbps"] == 50
    assert links["l34"]["reserved_mbps"] == 50
    assert links["l12"]["reserved_mbps"] == 40


def test_revoke_active_service_backfills_waiting_in_registration_order() -> None:
    engine, _ = make_engine()
    zero_delays(engine)

    a = engine.register_service("s1", "s2", 70)["services"][0]  # l12
    b = engine.register_service("s1", "s3", 40)["services"][1]  # blocked by l12
    c = engine.register_service("s2", "s3", 30)["services"][2]  # l23, still fits

    rows = services_by_id(engine)
    assert rows[a["id"]]["status"] == "reserved"
    assert rows[b["id"]]["status"] == "waiting"
    assert rows[c["id"]]["status"] == "reserved"

    engine.revoke_service(a["id"])
    b_row = services_by_id(engine)[b["id"]]
    assert b_row["status"] == "reserved"
    assert b_row["path_link_ids"] == ["l12", "l23"]


def test_revoke_waiting_service_does_not_disturb_active_reservations() -> None:
    engine, _ = make_engine()
    active = engine.register_service("s1", "s2", 30)["services"][0]
    waiting = engine.register_service("s1", "s2", 90)["services"][1]
    assert waiting["status"] == "waiting"

    engine.revoke_service(waiting["id"])
    snapshot = engine.snapshot()
    assert [service["id"] for service in snapshot["services"]] == [active["id"]]
    assert snapshot["links"][0]["reserved_mbps"] == 30


def test_no_route_until_topology_reconnects_then_auto_admit() -> None:
    engine, _ = make_engine()
    zero_delays(engine)

    # Two missing edges isolate S2: L12 and L23 down.
    engine.unplug("l12")
    engine.unplug("l23")
    row = engine.register_service("s2", "s1", 20)["services"][0]
    assert row["status"] == "waiting"
    assert "无路可走" in row["waiting_reason"]

    # Plug L12 back; zero-delay open reconnects S2 in the same request.
    engine.plug("l12")
    row = services_by_id(engine)[row["id"]]
    assert row["status"] == "reserved"
    assert row["path_link_ids"] == ["l12"]


def test_reset_clears_services_and_utilization() -> None:
    engine, _ = make_engine()
    engine.register_service("s1", "s3", 50)
    engine.register_service("s2", "s4", 60)
    assert len(engine.snapshot()["services"]) == 2

    snapshot = engine.reset()
    assert snapshot["services"] == []
    assert snapshot["service_summary"] == {"total": 0, "reserved": 0, "waiting": 0}
    for link in snapshot["links"]:
        assert link["reserved_mbps"] == 0
        assert link["available_mbps"] == 100

    # Registration sequence starts over after reset.
    new_service = engine.register_service("s1", "s2", 10)["services"][0]
    assert new_service["id"] == "svc1"


@pytest.mark.parametrize(
    "value",
    [0, -1, -50, 1.5, "abc", "50.5", "", None, True, False],
)
def test_demand_must_be_positive_integer(value) -> None:
    with pytest.raises(ValueError):
        NetworkEngine.parse_demand_mbps(value)


def test_integer_like_inputs_are_accepted() -> None:
    assert NetworkEngine.parse_demand_mbps("80") == 80
    assert NetworkEngine.parse_demand_mbps(80.0) == 80
    assert NetworkEngine.parse_demand_mbps(1) == 1


def test_unknown_switch_and_same_endpoints_are_rejected() -> None:
    engine, _ = make_engine()
    with pytest.raises(KeyError):
        engine.register_service("s9", "s1", 10)
    with pytest.raises(KeyError):
        engine.register_service("s1", "s9", 10)
    with pytest.raises(ValueError):
        engine.register_service("s1", "s1", 10)
    with pytest.raises(KeyError):
        engine.revoke_service("svc404")


def test_random_mix_never_overbooks_and_reserved_paths_stay_actual() -> None:
    random.seed(20261127)
    engine, clock = make_engine()
    switch_ids = [switch["id"] for switch in engine.SWITCHES]
    link_ids = [link["id"] for link in engine.snapshot()["links"]]
    live_ids: list[str] = []

    for _ in range(300):
        clock.advance(random.randint(0, 200))
        engine.process_due_commands()

        choice = random.random()
        if choice < 0.45:
            source, target = random.sample(switch_ids, 2)
            demand = random.choice([10, 25, 40, 50, 60, 80, 100, 120])
            snapshot = engine.register_service(source, target, demand)
            live_ids.append(snapshot["services"][-1]["id"])
        elif choice < 0.62 and live_ids:
            service_id = random.choice(live_ids)
            engine.revoke_service(service_id)
            live_ids.remove(service_id)
        else:
            link_id = random.choice(link_ids)
            link = engine._link_locked(link_id)
            engine.set_plugged(link_id, not link["plugged"])

        snapshot = engine.snapshot()
        used = {link_id: 0 for link_id in link_ids}
        for service in snapshot["services"]:
            if service["status"] == "reserved":
                path = engine._forwarding_path_locked(service["source"], service["target"])
                assert path is not None
                assert path[1] == service["path_link_ids"]
                for link_id in service["path_link_ids"]:
                    used[link_id] += service["demand_mbps"]
            else:
                assert service["path_link_ids"] is None

        for link_id, reserved in used.items():
            assert reserved <= LINK_CAPACITY_MBPS, f"{link_id} overbooked: {reserved}"
        for link in snapshot["links"]:
            assert link["reserved_mbps"] == used[link["id"]]
            assert link["available_mbps"] == LINK_CAPACITY_MBPS - used[link["id"]]
