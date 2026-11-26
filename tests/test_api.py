from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.network import NetworkEngine  # noqa: E402
from app.server import create_app  # noqa: E402


class FixedClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        return self.value


@pytest.fixture()
def client():
    engine = NetworkEngine(now_func=FixedClock())
    app = create_app(engine=engine, start_scheduler=False)
    app.testing = True
    with app.test_client() as test_client:
        yield test_client


def test_index_and_health(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"

    response = client.get("/")
    assert response.status_code == 200
    assert "四台交换机环网".encode("utf-8") in response.data
    assert b'data-testid="broadcast-button"' in response.data


def test_state_contains_four_switches_and_links(client) -> None:
    data = client.get("/api/state").get_json()
    assert len(data["switches"]) == 4
    assert len(data["links"]) == 4
    assert data["topology"]["loop_free"] is True


def test_broadcast_api_from_each_switch(client) -> None:
    for source in ("s1", "s2", "s3", "s4"):
        data = client.post("/api/broadcast", json={"source": source}).get_json()
        assert set(data["last_probe"]["reached"]) == {"s1", "s2", "s3", "s4"}
        assert data["last_probe"]["loop_free"] is True


def test_zero_delay_unplug_and_repair_switch_back_synchronously(client) -> None:
    for link_id in ("l12", "l23", "l34", "l41"):
        response = client.patch(
            f"/api/links/{link_id}/delays",
            json={"open_delay_seconds": 0, "block_delay_seconds": 0},
        )
        assert response.status_code == 200

    response = client.post("/api/links/l23/unplug")
    assert response.status_code == 200
    data = response.get_json()
    states = {link["id"]: link["state"] for link in data["links"]}
    assert states["l23"] == "down"
    assert states["l41"] == "forwarding"
    assert data["topology"]["loop_free"] is True

    probe = client.post("/api/broadcast", json={"source": "s2"}).get_json()["last_probe"]
    assert set(probe["reached"]) == {"s1", "s2", "s3", "s4"}

    response = client.post("/api/links/l23/plug")
    data = response.get_json()
    states = {link["id"]: link["state"] for link in data["links"]}
    assert states == {"l12": "forwarding", "l23": "forwarding", "l34": "forwarding", "l41": "blocked"}
    assert data["topology"]["loop_free"] is True


def test_backup_can_be_changed_and_delay_validation(client) -> None:
    response = client.post("/api/backup", json={"link_id": "l23"})
    assert response.status_code == 200
    data = response.get_json()
    # With zero? Default delays are positive, so the safe block is pending.
    assert any(c["link_id"] == "l23" and c["action"] == "block" and c["status"] == "pending" for c in data["commands"])

    response = client.patch("/api/links/l23/delays", json={"open_delay_seconds": -1})
    assert response.status_code == 400

    response = client.post("/api/backup", json={})
    assert response.status_code == 400

    response = client.post("/api/broadcast", json={"source": "nope"})
    assert response.status_code == 404
