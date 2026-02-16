from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

from .config import Settings
from .event_queue import EventQueue, WebhookEvent


@dataclass
class UniverseSymbol:
    symbol: str
    pm_high: float
    gap_pct: float
    premarket_dollar_vol: float
    spread_pct: float
    last_event_ts: int
    active: bool = True
    vwap_fail_since: datetime | None = None


@dataclass
class PositionPlan:
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    runner_trail_pct: float = 0.04
    peak_price: float = 0.0
    tp1_done: bool = False
    tp2_done: bool = False


@dataclass
class RuntimeRisk:
    trades_today: int = 0
    daily_realized_pnl: float = 0.0
    budget_remaining: float = 0.0
    killed: bool = False


class FirstHourGapBotEngine:
    def __init__(self, settings: Settings, event_queue: EventQueue, logger: logging.Logger) -> None:
        self.settings = settings
        self.event_queue = event_queue
        self.logger = logger

        paper = "paper" in settings.alpaca_base_url
        self.trading_client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=paper)
        self.data_client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)

        self.tz = ZoneInfo(settings.timezone)
        self.stop_event = threading.Event()
        self.universe: dict[str, UniverseSymbol] = {}
        self.position_plans: dict[str, PositionPlan] = {}
        self.risk = RuntimeRisk(budget_remaining=settings.daily_buying_power_budget)
        self._last_reconcile_ok = True
        self._current_day = datetime.now(self.tz).date()
        self._day_start_equity: float | None = None

    def run_forever(self) -> None:
        self.logger.info(json.dumps({"msg": "engine_started"}))
        while not self.stop_event.is_set():
            try:
                self._roll_day_if_needed()
                self._consume_events(max_events=25)
                self._reconcile_positions()
                self._check_daily_limits()
                self._evaluate_entries()
                self._manage_positions()
                self._flatten_if_needed()
            except Exception as exc:
                self.logger.exception(json.dumps({"msg": "engine_loop_error", "error": str(exc)}))
            time.sleep(1)

    def shutdown(self) -> None:
        self.stop_event.set()

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _roll_day_if_needed(self) -> None:
        now_day = self._now().date()
        if now_day != self._current_day:
            self.logger.info(json.dumps({"msg": "new_trading_day", "day": str(now_day)}))
            self._current_day = now_day
            self.risk = RuntimeRisk(budget_remaining=self.settings.daily_buying_power_budget)
            self.position_plans.clear()
            self.universe.clear()
            self._day_start_equity = None

    def _consume_events(self, max_events: int) -> None:
        processed = 0
        while processed < max_events:
            ev = self.event_queue.get(timeout=0.01)
            if ev is None:
                break
            processed += 1
            self._handle_event(ev)

    def _handle_event(self, event: WebhookEvent) -> None:
        if event.event != "PREMARKET_MOVER":
            self.logger.info(json.dumps({"msg": "event_ignored", "reason": "unknown_event", "event": event.event}))
            return
        liquid = event.premarket_dollar_vol >= 1_000_000 and event.spread_pct <= 0.01 and event.gap_pct >= 0.02
        if not liquid:
            self.logger.info(json.dumps({"msg": "universe_reject", "symbol": event.symbol, "reason": "criteria"}))
            return
        self.universe[event.symbol] = UniverseSymbol(
            symbol=event.symbol,
            pm_high=event.pm_high,
            gap_pct=event.gap_pct,
            premarket_dollar_vol=event.premarket_dollar_vol,
            spread_pct=event.spread_pct,
            last_event_ts=event.ts,
        )
        self.logger.info(json.dumps({"msg": "universe_add", "symbol": event.symbol, "size": len(self.universe)}))

    def _reconcile_positions(self) -> None:
        try:
            broker_positions = self.trading_client.get_all_positions()
            broker_symbols = {p.symbol for p in broker_positions}
            local_symbols = set(self.position_plans.keys())
            stale_local = local_symbols - broker_symbols
            for sym in stale_local:
                self.position_plans.pop(sym, None)
            self._last_reconcile_ok = True
        except Exception as exc:
            self._last_reconcile_ok = False
            self.risk.killed = True
            self.logger.error(json.dumps({"msg": "kill_switch", "reason": "reconcile_failed", "error": str(exc)}))

    def _check_daily_limits(self) -> None:
        account = self.trading_client.get_account()
        equity = float(account.equity)
        if self._day_start_equity is None:
            self._day_start_equity = equity
        self.risk.daily_realized_pnl = equity - self._day_start_equity
        if self.risk.daily_realized_pnl <= -abs(self.settings.daily_loss_limit):
            self.risk.killed = True
            self.logger.error(json.dumps({"msg": "kill_switch", "reason": "daily_loss_limit", "pnl": self.risk.daily_realized_pnl}))
        self.logger.info(json.dumps({"msg": "daily_pnl", "pnl": round(self.risk.daily_realized_pnl, 2)}))

    def _in_trade_window(self) -> bool:
        now = self._now().time()
        return self.settings.trade_start_ct <= now <= self.settings.trade_end_ct

    def _after_flatten_window(self) -> bool:
        return self._now().time() >= self.settings.flatten_time_ct

    def _evaluate_entries(self) -> None:
        if not self._in_trade_window() or self.risk.killed or not self._last_reconcile_ok:
            return
        if self.risk.trades_today >= self.settings.max_trades_per_day:
            self.logger.warning(json.dumps({"msg": "risk_block", "reason": "max_trades_day"}))
            return
        if len(self.position_plans) >= self.settings.max_positions:
            self.logger.warning(json.dumps({"msg": "risk_block", "reason": "max_positions"}))
            return

        for symbol, sym_data in list(self.universe.items()):
            if symbol in self.position_plans or not sym_data.active:
                continue
            if self.risk.budget_remaining < self.settings.notional_per_trade:
                self.logger.warning(json.dumps({"msg": "risk_block", "reason": "budget_exhausted"}))
                break

            signal = self._entry_signal(symbol, sym_data)
            if not signal["enter"]:
                self.logger.info(json.dumps({"msg": "trade_skip", "symbol": symbol, "reason": signal["reason"]}))
                continue

            ok = self._submit_entry_order(symbol, signal["limit_price"], signal["atr"], signal["vwap"])
            if ok:
                self.risk.trades_today += 1
                self.risk.budget_remaining -= self.settings.notional_per_trade
                if len(self.position_plans) >= self.settings.max_positions:
                    break

    def _entry_signal(self, symbol: str, sym_data: UniverseSymbol) -> dict:
        bars = self._get_recent_bars(symbol, limit=30)
        if bars is None or len(bars) < 10:
            return {"enter": False, "reason": "insufficient_data"}

        quote = self.data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol])).get(symbol)
        if quote is None:
            return {"enter": False, "reason": "no_quote"}

        spread_pct = float((quote.ask_price - quote.bid_price) / max(quote.ask_price, 0.0001))
        bars["vwap_live"] = (bars["close"] * bars["volume"]).cumsum() / bars["volume"].replace(0, np.nan).cumsum().ffill()
        latest = bars.iloc[-1]
        prev5_avg = bars["volume"].iloc[-6:-1].mean() if len(bars) >= 6 else bars["volume"].mean()
        vol_spike = latest["volume"] >= 1.5 * max(prev5_avg, 1)

        atr = self._calc_atr(bars)
        price = float(latest["close"])
        vwap = float(latest["vwap_live"])

        broke_pm_high = price > sym_data.pm_high
        vwap_reclaim = price > vwap and float(bars.iloc[-2]["close"]) <= float(bars.iloc[-2]["vwap_live"])
        above_vwap = price > vwap

        if spread_pct > 0.01:
            sym_data.active = False
            return {"enter": False, "reason": "criteria_decrease_spread"}
        if latest["volume"] < 0.3 * max(prev5_avg, 1):
            sym_data.active = False
            return {"enter": False, "reason": "criteria_decrease_volume"}

        if not above_vwap:
            if sym_data.vwap_fail_since is None:
                sym_data.vwap_fail_since = self._now()
            elif self._now() - sym_data.vwap_fail_since > timedelta(minutes=3):
                sym_data.active = False
                return {"enter": False, "reason": "criteria_decrease_vwap_fail"}
        else:
            sym_data.vwap_fail_since = None

        if above_vwap and vol_spike and (broke_pm_high or vwap_reclaim):
            midpoint = float((quote.ask_price + quote.bid_price) / 2)
            limit_price = round(min(midpoint * 1.001, quote.ask_price), 2)
            return {
                "enter": True,
                "reason": "confirmed",
                "limit_price": limit_price,
                "atr": atr,
                "vwap": vwap,
            }
        return {"enter": False, "reason": "entry_not_confirmed"}

    def _submit_entry_order(self, symbol: str, limit_price: float, atr: float, vwap: float) -> bool:
        qty = round(self.settings.notional_per_trade / max(limit_price, 0.01), 4)
        if qty <= 0:
            return False
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            limit_price=limit_price,
            time_in_force=TimeInForce.DAY,
        )
        try:
            order = self.trading_client.submit_order(order_data=req)
            stop_distance = max(0.025 * limit_price, 1.2 * max(atr, 0.01))
            stop_price = round(limit_price - stop_distance, 2)
            self.position_plans[symbol] = PositionPlan(
                symbol=symbol,
                qty=qty,
                entry_price=limit_price,
                stop_price=stop_price,
                tp1_price=round(limit_price * 1.04, 2),
                tp2_price=round(limit_price * 1.08, 2),
                peak_price=limit_price,
            )
            self.logger.info(
                json.dumps(
                    {
                        "msg": "entry_submitted",
                        "symbol": symbol,
                        "qty": qty,
                        "limit_price": limit_price,
                        "order_id": str(order.id),
                        "atr": round(atr, 4),
                        "vwap": round(vwap, 4),
                    }
                )
            )
            return True
        except Exception as exc:
            self.logger.error(json.dumps({"msg": "entry_submit_failed", "symbol": symbol, "error": str(exc)}))
            return False

    def _manage_positions(self) -> None:
        if not self.position_plans:
            return
        orders = self.trading_client.get_orders(filter=GetOrdersRequest(status="all", limit=200, nested=True))
        for symbol, plan in list(self.position_plans.items()):
            quote = self.data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol])).get(symbol)
            if quote is None:
                self.risk.killed = True
                self.logger.error(json.dumps({"msg": "kill_switch", "reason": "stale_data", "symbol": symbol}))
                continue
            last = float((quote.ask_price + quote.bid_price) / 2)
            plan.peak_price = max(plan.peak_price, last)
            trailing_stop = round(plan.peak_price * (1 - plan.runner_trail_pct), 2)

            if last <= plan.stop_price or last <= trailing_stop:
                self._submit_market_exit(symbol, plan.qty, "stop_or_trailing")
                continue

            if not plan.tp1_done and last >= plan.tp1_price:
                self._submit_market_exit(symbol, round(plan.qty * 0.5, 4), "tp1")
                plan.tp1_done = True
            if not plan.tp2_done and last >= plan.tp2_price:
                self._submit_market_exit(symbol, round(plan.qty * 0.25, 4), "tp2")
                plan.tp2_done = True

            self._log_fill_updates(symbol, orders)

    def _submit_market_exit(self, symbol: str, qty: float, reason: str) -> None:
        qty = max(round(qty, 4), 0)
        if qty <= 0:
            return
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        try:
            order = self.trading_client.submit_order(order_data=req)
            self.logger.info(json.dumps({"msg": "exit_submitted", "symbol": symbol, "qty": qty, "reason": reason, "order_id": str(order.id)}))
            if reason in {"stop_or_trailing", "flatten"}:
                self.position_plans.pop(symbol, None)
        except Exception as exc:
            self.logger.error(json.dumps({"msg": "exit_submit_failed", "symbol": symbol, "error": str(exc), "reason": reason}))

    def _flatten_if_needed(self) -> None:
        if not self._after_flatten_window() or not self.position_plans:
            return
        for symbol, plan in list(self.position_plans.items()):
            self._submit_market_exit(symbol, plan.qty, "flatten")

    def _log_fill_updates(self, symbol: str, orders: list) -> None:
        for order in orders:
            if getattr(order, "symbol", "") == symbol and getattr(order, "filled_avg_price", None):
                self.logger.info(
                    json.dumps(
                        {
                            "msg": "fill_update",
                            "symbol": symbol,
                            "status": str(order.status),
                            "filled_avg_price": str(order.filled_avg_price),
                            "filled_qty": str(order.filled_qty),
                        }
                    )
                )

    def _get_recent_bars(self, symbol: str, limit: int = 30) -> pd.DataFrame | None:
        end = self._now()
        start = end - timedelta(minutes=max(limit * 2, 60))
        req = StockBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start, end=end)
        bars = self.data_client.get_stock_bars(req)
        rows = bars.data.get(symbol, [])
        if not rows:
            return None
        df = pd.DataFrame(
            {
                "timestamp": [r.timestamp for r in rows],
                "open": [float(r.open) for r in rows],
                "high": [float(r.high) for r in rows],
                "low": [float(r.low) for r in rows],
                "close": [float(r.close) for r in rows],
                "volume": [float(r.volume) for r in rows],
            }
        )
        return df.tail(limit).reset_index(drop=True)

    @staticmethod
    def _calc_atr(df: pd.DataFrame, n: int = 14) -> float:
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                (df["high"] - df["low"]).abs(),
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(window=n, min_periods=1).mean().iloc[-1]
        return float(atr)
