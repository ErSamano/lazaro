from __future__ import annotations

import json
import logging
from queue import Full

from flask import Flask, jsonify, request

from .config import Settings
from .event_queue import EventQueue, WebhookEvent
from .security import parse_and_validate_json, validate_timestamp, verify_hmac_signature


def create_app(settings: Settings, event_queue: EventQueue, logger: logging.Logger) -> Flask:
    app = Flask(__name__)

    @app.route("/healthz", methods=["GET"])
    def healthz() -> tuple[str, int]:
        return "ok", 200

    @app.route("/webhook/tradingview", methods=["POST"])
    def tradingview_webhook():
        raw_body = request.get_data(cache=False)
        signature = request.headers.get("X-Signature")

        if not verify_hmac_signature(settings.webhook_secret, raw_body, signature):
            logger.warning(json.dumps({"msg": "webhook_rejected", "reason": "bad_signature"}))
            return jsonify({"ok": False, "error": "invalid signature"}), 401

        try:
            payload = parse_and_validate_json(raw_body)
        except Exception as exc:
            logger.warning(json.dumps({"msg": "webhook_rejected", "reason": "invalid_payload", "error": str(exc)}))
            return jsonify({"ok": False, "error": "invalid payload"}), 400

        if not validate_timestamp(payload, max_drift_seconds=300):
            logger.warning(json.dumps({"msg": "webhook_rejected", "reason": "stale_timestamp", "ts": payload.get("ts")}))
            return jsonify({"ok": False, "error": "stale timestamp"}), 400

        event = WebhookEvent(
            symbol=str(payload["symbol"]).upper(),
            event=str(payload["event"]),
            pm_high=float(payload["pm_high"]),
            gap_pct=float(payload["gap_pct"]),
            premarket_dollar_vol=float(payload["premarket_dollar_vol"]),
            spread_pct=float(payload["spread_pct"]),
            ts=int(payload["ts"]),
            raw=payload,
        )

        try:
            event_queue.put(event)
        except Full:
            logger.error(json.dumps({"msg": "webhook_rejected", "reason": "queue_full"}))
            return jsonify({"ok": False, "error": "queue full"}), 503

        logger.info(
            json.dumps(
                {
                    "msg": "webhook_accepted",
                    "symbol": event.symbol,
                    "event": event.event,
                    "queue_size": event_queue.size(),
                }
            )
        )
        return jsonify({"ok": True}), 200

    return app
