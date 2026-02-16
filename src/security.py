from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


def compute_hmac_signature(secret: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return digest


def verify_hmac_signature(secret: str, raw_body: bytes, received_signature: str | None) -> bool:
    if not received_signature:
        return False
    expected = compute_hmac_signature(secret, raw_body)
    return hmac.compare_digest(expected, received_signature.strip().lower())


def validate_timestamp(payload: dict[str, Any], max_drift_seconds: int = 300, now_epoch: int | None = None) -> bool:
    ts = payload.get("ts")
    if ts is None:
        return False
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return False

    now = int(now_epoch or time.time())
    drift = abs(now - ts)
    return drift <= max_drift_seconds


def parse_and_validate_json(raw_body: bytes) -> dict[str, Any]:
    body = json.loads(raw_body.decode("utf-8"))
    required = {
        "symbol",
        "event",
        "pm_high",
        "gap_pct",
        "premarket_dollar_vol",
        "spread_pct",
        "ts",
    }
    missing = required - set(body.keys())
    if missing:
        raise ValueError(f"Missing keys: {sorted(missing)}")
    return body


def _self_check() -> None:
    # Lightweight unit-test-like validation helpers (no pytest dependency).
    secret = "abc123"
    raw = b'{"symbol":"TSLA","ts":1700000000}'
    sig = compute_hmac_signature(secret, raw)
    assert verify_hmac_signature(secret, raw, sig)
    assert not verify_hmac_signature(secret, raw, "deadbeef")
    assert validate_timestamp({"ts": 100}, max_drift_seconds=10, now_epoch=108)
    assert not validate_timestamp({"ts": 100}, max_drift_seconds=5, now_epoch=110)


if __name__ == "__main__":
    _self_check()
    print("security self-check passed")
