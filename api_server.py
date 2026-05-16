"""
Minimal Flask API for the goout-scraper VM service.

GET  /health  — liveness probe
POST /scrape   — trigger a manual scrape (force_send=True)
GET  /status   — last scrape timestamp + running flag
"""

import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request

import config

app = Flask(__name__)

_state = {
    "running": False,
    "last_scrape_at": None,
    "last_count": 0,
}
_lock = threading.Lock()

# Injected by main.py after startup
_accounts = []
_db = None
_telegram_mgr = None


def init(accounts, db, telegram_mgr):
    global _accounts, _db, _telegram_mgr
    _accounts = accounts
    _db = db
    _telegram_mgr = telegram_mgr


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "running": _state["running"],
            "last_scrape_at": _state["last_scrape_at"].isoformat() if _state["last_scrape_at"] else None,
            "last_count": _state["last_count"],
        })


@app.route("/scrape", methods=["POST"])
def scrape():
    token = request.headers.get("X-Service-Token", "")
    if token != config.SERVICE_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    with _lock:
        if _state["running"]:
            return jsonify({"error": "scrape already running"}), 409
        _state["running"] = True

    def _run():
        from orchestrator import run_daily_scrape
        try:
            run_daily_scrape(_accounts, _db, _telegram_mgr, force_send=True)
            with _lock:
                _state["last_scrape_at"] = datetime.now(timezone.utc)
        finally:
            with _lock:
                _state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"}), 202


def run_server():
    app.run(host="0.0.0.0", port=config.API_PORT, debug=False, use_reloader=False)
