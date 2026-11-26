"""Flask HTTP layer for the four-switch ring drill."""

from __future__ import annotations

import os
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request

from .network import NetworkEngine


def _payload() -> dict[str, Any]:
    data: dict[str, Any] = {}
    data.update(request.args.to_dict(flat=True))
    if request.form:
        data.update(request.form.to_dict(flat=True))
    json_data = request.get_json(silent=True)
    if isinstance(json_data, dict):
        data.update(json_data)
    return data


def _first(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _delay_ms(data: dict[str, Any], field: str) -> Optional[float]:
    """Accept explicit milliseconds or seconds for API convenience."""

    ms_value = _first(data, f"{field}_delay_ms", f"{field}_ms")
    if ms_value is not None:
        return float(ms_value)

    sec_value = _first(
        data,
        f"{field}_delay_seconds",
        f"{field}_delay_sec",
        f"{field}_delay",
        f"{field}_seconds",
        f"{field}_sec",
    )
    if sec_value is None:
        return None
    return float(sec_value) * 1000.0


def create_app(engine: Optional[NetworkEngine] = None, start_scheduler: bool = True) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.config["ENGINE"] = engine or NetworkEngine()
    if start_scheduler and not app.config.get("TESTING"):
        app.config["ENGINE"].start_scheduler()

    @app.get("/")
    def index() -> str:
        engine: NetworkEngine = app.config["ENGINE"]
        return render_template("index.html", initial_state=engine.snapshot())

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/api/state")
    def get_state():
        engine: NetworkEngine = app.config["ENGINE"]
        return jsonify(engine.snapshot())

    @app.post("/api/reset")
    def reset_state():
        engine: NetworkEngine = app.config["ENGINE"]
        engine.reset()
        engine.start_scheduler()
        return jsonify(engine.snapshot())

    @app.post("/api/links/<link_id>/unplug")
    def unplug_link(link_id: str):
        engine: NetworkEngine = app.config["ENGINE"]
        try:
            return jsonify(engine.unplug(link_id))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/api/links/<link_id>/plug")
    def plug_link(link_id: str):
        engine: NetworkEngine = app.config["ENGINE"]
        try:
            return jsonify(engine.plug(link_id))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/api/links/<link_id>/toggle")
    def toggle_link(link_id: str):
        engine: NetworkEngine = app.config["ENGINE"]
        data = _payload()
        try:
            snapshot = engine.snapshot()
            link = next((item for item in snapshot["links"] if item["id"] == link_id), None)
            if link is None:
                raise KeyError(f"未知链路：{link_id}")
            if "plugged" in data:
                raw = data["plugged"]
                plugged = raw if isinstance(raw, bool) else str(raw).lower() in {"1", "true", "yes", "on", "插回"}
            else:
                plugged = not link["plugged"]
            return jsonify(engine.set_plugged(link_id, plugged))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/links/<link_id>/delays", methods=["POST", "PATCH", "PUT"])
    @app.route("/api/links/<link_id>", methods=["PATCH", "PUT"])
    def update_link(link_id: str):
        engine: NetworkEngine = app.config["ENGINE"]
        data = _payload()
        open_ms = _delay_ms(data, "open")
        block_ms = _delay_ms(data, "block")
        if open_ms is None and block_ms is None:
            return jsonify({"error": "请提供 open_delay/open_delay_seconds 或 block_delay/block_delay_seconds"}), 400
        try:
            return jsonify(engine.set_delays(link_id, open_delay_ms=open_ms, block_delay_ms=block_ms))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/links/<link_id>/backup", methods=["POST", "PUT", "PATCH"])
    def set_link_backup(link_id: str):
        engine: NetworkEngine = app.config["ENGINE"]
        try:
            return jsonify(engine.set_backup(link_id))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/backup", methods=["POST", "PUT", "PATCH"])
    def set_backup():
        engine: NetworkEngine = app.config["ENGINE"]
        data = _payload()
        link_id = _first(data, "link_id", "link", "backup_link", "id")
        if not link_id:
            return jsonify({"error": "link_id is required"}), 400
        try:
            return jsonify(engine.set_backup(str(link_id)))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/services", methods=["GET", "POST"])
    @app.route("/api/service", methods=["GET", "POST"])
    @app.route("/api/reservations", methods=["GET", "POST"])
    @app.route("/api/businesses", methods=["GET", "POST"])
    @app.route("/api/business", methods=["GET", "POST"])
    @app.post("/api/services/register")
    @app.post("/api/service/register")
    def services_collection():
        engine: NetworkEngine = app.config["ENGINE"]
        if request.method == "GET":
            return jsonify(engine.snapshot())

        data = _payload()
        source = _first(data, "source", "source_id", "from", "src", "start_switch")
        target = _first(
            data, "target", "target_id", "to", "dst", "destination", "end_switch"
        )
        demand = _first(
            data,
            "demand_mbps",
            "demandMbps",
            "mbps_demand",
            "demand",
            "mbps",
            "bw",
            "bandwidth_mbps",
            "rate_mbps",
            "bandwidth",
            "rate",
        )
        if source is None or target is None:
            return jsonify({"error": "请选择起点交换机和终点交换机"}), 400
        if demand is None:
            return jsonify({"error": "请填写整数 Mbps 带宽需求（demand_mbps）"}), 400

        try:
            snapshot = engine.register_service(source, target, demand)
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        # This codebase's action endpoints return the full drill snapshot; keep
        # that shape and additionally expose the created row for clients that
        # expect the resource directly.
        row = snapshot["services"][-1]
        snapshot["service"] = dict(row)
        snapshot.update(row)
        return jsonify(snapshot), 200

    @app.route("/api/services/revoke", methods=["POST"])
    def revoke_service_by_body():
        engine: NetworkEngine = app.config["ENGINE"]
        data = _payload()
        service_id = _first(data, "service_id", "id", "service")
        if not service_id:
            return jsonify({"error": "service_id is required"}), 400
        try:
            return jsonify(engine.revoke_service(str(service_id)))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.delete("/api/services/<service_id>")
    @app.post("/api/services/<service_id>/revoke")
    @app.post("/api/services/<service_id>/delete")
    @app.post("/api/businesses/<service_id>/revoke")
    def revoke_service(service_id: str):
        engine: NetworkEngine = app.config["ENGINE"]
        try:
            return jsonify(engine.revoke_service(service_id))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.route("/api/broadcast", methods=["GET", "POST"])
    @app.route("/api/probe", methods=["GET", "POST"])
    def broadcast():
        engine: NetworkEngine = app.config["ENGINE"]
        if request.method == "GET":
            data = request.args.to_dict(flat=True)
        else:
            data = _payload()
        source = _first(data, "source", "switch_id", "switch", "from", "source_id") or "s1"
        try:
            probe = engine.send_broadcast(str(source))
            state = engine.snapshot()
            # Include probe at top level as well for simple command-line clients.
            state["probe"] = probe
            return jsonify(state)
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.after_request
    def add_local_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    @app.errorhandler(404)
    def not_found(exc):  # pragma: no cover - Flask default path
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found", "path": request.path}), 404
        return exc

    return app


# Gunicorn/waitress entry point: ``wsgi:app``.
engine = NetworkEngine()
engine.start_scheduler()
app = create_app(engine=engine, start_scheduler=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
