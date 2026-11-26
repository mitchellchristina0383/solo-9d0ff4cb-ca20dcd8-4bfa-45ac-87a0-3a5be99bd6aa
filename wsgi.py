"""WSGI entry point for Docker/waitress and local runs."""

from app.server import app, engine

__all__ = ["app", "engine"]


if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True, use_reloader=False)
