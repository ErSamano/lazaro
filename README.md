# First Hour Premarket Gap Bot (Alpaca + TradingView Webhook)

A runnable Python 3.11+ project for a **$1,000 cash-account-style first-hour strategy**:

- TradingView alert webhook receiver (Flask)
- HMAC authentication + timestamp drift validation
- Thread-safe event queue
- Background bot engine that consumes events and trades through Alpaca (paper by default)
- Strict risk controls and kill-switch behavior

> Source of premarket movers is **only TradingView webhook events** (no scraping / unofficial APIs).

## Project Structure

```text
.
├── .env.example
├── requirements.txt
├── README.md
└── src
    ├── bot_engine.py
    ├── config.py
    ├── event_queue.py
    ├── main.py
    ├── security.py
    └── webhook_server.py
```

## Features

- **Webhook security**
  - Validates `X-Signature` as `hex(HMAC_SHA256(secret, raw_body))`
  - Validates payload timestamp drift (max 5 minutes)
- **Concurrency model**
  - Flask server thread/process handles incoming webhooks
  - Bot engine runs in background thread and continuously:
    1. drains webhook queue
    2. updates trade universe
    3. reconciles broker positions
    4. runs entry/exit logic
- **Trading window in America/Chicago (CT)**
  - Entry window: `08:33` to `09:30` CT
  - Flatten all positions by `09:35` CT
- **Risk controls (cash-account style)**
  - `notional_per_trade` default `$25` (supports `$10` override)
  - max positions `3`
  - max trades/day `10`
  - daily loss limit `$20`
  - `daily_buying_power_budget` (defaults `$250`) decremented on entries; proceeds are **not** recycled intraday
- **Entry logic**
  - Symbol in `trade_universe` comes from webhook event
  - Enter if: `price > VWAP` AND (`break premarket high` OR `VWAP reclaim`) with 1m volume spike
- **Exit logic**
  - stop = `max(2.5%, 1.2 * ATR(1m))` from entry
  - TP1 `+4%` sell 50%
  - TP2 `+8%` sell 25%
  - runner 25% with ~4% trailing stop
- **Criteria decrease block (no new entries for symbol)**
  - spread > 1.0%, OR
  - 1m volume < 30% of previous 5m average, OR
  - loses VWAP and fails reclaim within 3 minutes
- **Safety controls**
  - kill switch on stale data / reconciliation failure / daily loss breach
  - position reconciliation vs broker truth each loop
  - never enters outside trade window
  - LIMIT entry orders using midpoint bias

## Setup (Mac / Linux / Windows)

### 1) Create virtual environment

Mac/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2) Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3) Configure environment

```bash
cp .env.example .env
```

Fill `.env`:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_BASE_URL=https://paper-api.alpaca.markets`
- `WEBHOOK_SECRET=...`
- optional risk overrides

### 4) Run app (webhook + bot engine)

```bash
python -m src.main
```

Server starts on `http://0.0.0.0:8000` by default.

Health endpoint:

```bash
curl http://127.0.0.1:8000/healthz
```

## TradingView Webhook Payload

Send JSON payload:

```json
{
  "symbol": "TSLA",
  "event": "PREMARKET_MOVER",
  "pm_high": 252.30,
  "gap_pct": 0.12,
  "premarket_dollar_vol": 3500000,
  "spread_pct": 0.004,
  "ts": 1700000000
}
```

Required header:

- `X-Signature: <hex hmac sha256>`

Where signature is:

```text
hex(HMAC_SHA256(WEBHOOK_SECRET, raw_request_body_bytes))
```

### Example: generate HMAC and test webhook with curl

Linux/macOS bash example:

```bash
BODY='{"symbol":"TSLA","event":"PREMARKET_MOVER","pm_high":252.30,"gap_pct":0.12,"premarket_dollar_vol":3500000,"spread_pct":0.004,"ts":'"$(date +%s)"'}'
SIG=$(python - <<'PY'
import hmac, hashlib, os
secret=os.getenv("WEBHOOK_SECRET","replace_with_strong_shared_secret")
body=os.getenv("BODY")
print(hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest())
PY
)
curl -X POST "http://127.0.0.1:8000/webhook/tradingview" \
  -H "Content-Type: application/json" \
  -H "X-Signature: ${SIG}" \
  -d "$BODY"
```

Windows PowerShell example (simplified): build JSON string, compute HMAC-SHA256 with .NET, then POST with `Invoke-RestMethod`.

## TradingView Alert Configuration Notes

1. In TradingView, create alert on your screener/indicator logic for premarket movers.
2. Set webhook URL to:
   - `https://<your-host>/webhook/tradingview`
3. Set alert message to valid JSON matching required fields above.
4. Your alert sender must compute and include `X-Signature` header (if your relay cannot add headers, place a tiny secure relay in front that adds header).
5. Ensure `ts` is Unix epoch seconds at send time.

## Logging

Logs are structured JSON-style messages and include:

- webhook accepted/rejected
- universe updates
- trade enter/skip decisions
- order submissions
- fill updates
- exit actions
- daily PnL snapshots
- risk blocks + kill-switch events

## Lightweight Validation Helpers

Run security self-check:

```bash
python -m src.security
```

## Notes

- Default mode is Alpaca paper trading.
- This is a starter production-style skeleton; add persistent storage, stronger order state tracking, and broker/webhook retry infrastructure before live deployment.
- Not financial advice.
