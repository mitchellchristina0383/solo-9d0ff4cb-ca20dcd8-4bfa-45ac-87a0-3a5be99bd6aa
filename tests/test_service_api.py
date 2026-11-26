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


def zero_delays(client) -> None:
    for link_id in ("l12", "l23", "l34", "l41"):
        response = client.patch(
            f"/api/links/{link_id}/delays",
            json={"open_delay_seconds": 0, "block_delay_seconds": 0},
        )
        assert response.status_code == 200


def test_page_contains_service_registration_controls(client) -> None:
    html = client.get("/").data.decode("utf-8")
    assert 'data-testid="service-form"' in html
    assert 'data-testid="service-source"' in html
    assert 'data-testid="service-target"' in html
    assert 'data-testid="service-demand"' in html
    assert 'data-testid="service-submit"' in html
    assert "登记业务" in html
    assert "起点交换机" in html
    assert "终点交换机" in html
    assert "整数 Mbps" in html


def test_create_service_api_returns_snapshot_with_reservation(client) -> None:
    response = client.post("/api/services", json={"source": "s1", "target": "s3", "demand_mbps": 50})
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["services"]) == 1
    service = data["services"][0]
    assert service["id"] == "svc1"
    assert service["status"] == "reserved"
    assert service["path_link_ids"] == ["l12", "l23"]
    # Convenience shapes for clients expecting the created resource directly.
    assert data["service"]["id"] == "svc1"
    assert data["id"] == "svc1"
    assert data["status"] == "reserved"

    state = client.get("/api/state").get_json()
    assert state["link_capacity_mbps"] == 100
    l12 = next(link for link in state["links"] if link["id"] == "l12")
    assert l12["reserved_mbps"] == 50
    assert l12["available_mbps"] == 50


def test_create_service_accepts_field_aliases_and_numeric_string(client) -> None:
    response = client.post(
        "/api/services",
        json={"from": "s2", "to": "s4", "mbps": "30"},
    )
    assert response.status_code == 200
    service = response.get_json()["services"][0]
    assert service["demand_mbps"] == 30
    assert service["path_link_ids"] == ["l23", "l34"]

    business = client.post(
        "/api/businesses",
        json={"source": "s1", "target": "s2", "bandwidth": 20},
    )
    assert business.status_code == 200
    assert business.get_json()["services"][1]["demand_mbps"] == 20

    listing = client.get("/api/services")
    assert listing.status_code == 200
    assert len(listing.get_json()["services"]) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "s1", "target": "s1", "demand_mbps": 10},
        {"source": "s1", "target": "s2", "demand_mbps": 0},
        {"source": "s1", "target": "s2", "demand_mbps": -5},
        {"source": "s1", "target": "s2", "demand_mbps": 12.5},
        {"source": "s1", "target": "s2", "demand_mbps": "lots"},
        {"source": "s1", "target": "s2"},
        {"target": "s2", "demand_mbps": 10},
    ],
)
def test_create_service_validation_errors(client, payload) -> None:
    response = client.post("/api/services", json=payload)
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_create_service_unknown_switch_is_404(client) -> None:
    response = client.post(
        "/api/services",
        json={"source": "s9", "target": "s1", "demand_mbps": 10},
    )
    assert response.status_code == 404


def test_revoke_delete_frees_and_backfills_waiting_service(client) -> None:
    zero_delays(client)
    client.post("/api/services", json={"source": "s1", "target": "s2", "demand_mbps": 70})
    waiting = client.post(
        "/api/services", json={"source": "s1", "target": "s3", "demand_mbps": 50}
    ).get_json()["services"][1]
    assert waiting["status"] == "waiting"

    response = client.delete("/api/services/svc1")
    assert response.status_code == 200
    services = response.get_json()["services"]
    assert len(services) == 1
    assert services[0]["id"] == "svc2"
    assert services[0]["status"] == "reserved"

    # POST alias works too and unknown ids are 404.
    response = client.post("/api/services/svc2/revoke")
    assert response.status_code == 200
    assert response.get_json()["services"] == []

    # Collection-level revoke alias accepts an id in the body.
    client.post("/api/services", json={"source": "s1", "target": "s2", "demand_mbps": 10})
    response = client.post("/api/services/revoke", json={"service_id": "svc3"})
    assert response.status_code == 200
    assert response.get_json()["services"] == []
    assert client.post("/api/services/revoke", json={}).status_code == 400

    assert client.delete("/api/services/svc2").status_code == 404
    assert client.post("/api/services/nope/revoke").status_code == 404


def test_switchover_contention_visible_through_api(client) -> None:
    zero_delays(client)
    client.post("/api/services", json={"source": "s2", "target": "s3", "demand_mbps": 60})
    client.post("/api/services", json={"source": "s1", "target": "s3", "demand_mbps": 50})
    client.post("/api/services", json={"source": "s1", "target": "s2", "demand_mbps": 40})

    data = client.post("/api/links/l23/unplug").get_json()
    by_id = {service["id"]: service for service in data["services"]}
    assert by_id["svc3"]["status"] == "reserved"
    assert by_id["svc3"]["path_link_ids"] == ["l12"]
    assert by_id["svc1"]["status"] == "reserved"
    assert by_id["svc1"]["path_link_ids"] == ["l12", "l41", "l34"]
    assert by_id["svc2"]["status"] == "waiting"
    assert "L41" in by_id["svc2"]["waiting_reason"]

    data = client.delete("/api/services/svc1").get_json()
    by_id = {service["id"]: service for service in data["services"]}
    assert by_id["svc2"]["status"] == "reserved"
    assert by_id["svc2"]["path_link_ids"] == ["l41", "l34"]


def test_reset_clears_services(client) -> None:
    client.post("/api/services", json={"source": "s1", "target": "s2", "demand_mbps": 10})
    data = client.post("/api/reset").get_json()
    assert data["services"] == []
    assert data["service_summary"]["total"] == 0
    for link in data["links"]:
        assert link["reserved_mbps"] == 0
