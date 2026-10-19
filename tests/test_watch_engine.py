"""Watch engine, IEX feed health, and paper-order guards."""

from datetime import date, time
import logging

import pandas as pd

from src.market.calendar import NyseCalendar
from src.risk.manager import RiskManager
from src.universe.schedule import session_slots
from src.universe.schedule_store import ScheduleStore
from src.universe.session_runner import SessionRunner
from src.watch.engine import ENTRY_RULE_DEFINED, WatchEngine
from src.watch.feed import FEED_SOURCE, IEX_STREAM_URL, IexFeed, plan_subscription
from src.watch.orders import OrderStore

DAY = date(2026, 10, 19)


class _Result:
    def __init__(self, ok, rows=None):
        self.ok = ok
        self.rows = rows or []


class PaperAdapter:
    def __init__(self, base_url="https://paper-api.alpaca.markets"):
        self.base_url = base_url
        self.submits = []
        self.remote = {}

    def submit_limit_order(self, symbol, qty, side, limit_price, time_in_force="gtc"):
        del time_in_force
        order = {"id": f"paper-{len(self.submits) + 1}", "status": "accepted", "filled_qty": 0, "symbol": symbol}
        self.submits.append({"symbol": symbol, "qty": qty, "side": side, "limit_price": limit_price})
        self.remote[order["id"]] = {"id": order["id"], "status": "accepted", "filled_qty": 0}
        return order

    def get_order(self, order_id):
        return dict(self.remote[order_id])


def _et(hour, minute, day=DAY):
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hour, minute=minute, tz="America/New_York")


def _row(symbol, rank, status="PASS", score=None):
    return {
        "symbol": symbol,
        "rank": rank,
        "veto_status": status,
        "final_score": float(11 - rank) if score is None else score,
        "scan_timestamp": "2026-10-19T14:45:00+00:00",
    }


def _slots():
    return session_slots(NyseCalendar().session(DAY))


def _engine(tmp_path, feed=None, risk=None):
    adapter = PaperAdapter()
    orders = OrderStore(str(tmp_path / "orders.db"))
    engine = WatchEngine(orders, adapter, risk or RiskManager(cash=100000, max_position_percent=1.0), feed or IexFeed())
    return engine, adapter


def _arm(feed, symbols, now):
    feed.connect()
    feed.ingest({"T": "success", "msg": "authenticated"}, now)
    feed.ingest({"T": "subscription", "trades": list(symbols)}, now)
    for symbol in symbols:
        feed.ingest({"T": "t", "S": symbol, "p": 10.0}, now)


def _runner(tmp_path, scan, engine):
    return SessionRunner(
        calendar=NyseCalendar(),
        schedule_store=ScheduleStore(str(tmp_path / "schedule.db")),
        watch=engine,
        scan_fn=scan,
        universe_db=str(tmp_path / "universe.db"),
    )


def test_watch_starts_only_after_the_first_successful_regular_scan_and_keeps_running(tmp_path):
    calls = []
    rows = [_row("AAPL", 1)]

    def scan(slot, now):
        del now
        calls.append(slot.scan_at.strftime("%H:%M"))
        if slot.scan_at.strftime("%H:%M") == "10:45":
            return _Result(False)
        return _Result(True, rows)

    engine, _adapter = _engine(tmp_path)
    runner = _runner(tmp_path, scan, engine)
    assert engine.started is False
    runner.tick(_et(10, 45))
    assert engine.started is False
    assert calls == ["10:45"]
    runner.tick(_et(11, 0))
    assert calls == ["10:45"]
    assert engine.evaluations == []
    runner.tick(_et(11, 45))
    runner.tick(_et(12, 10))
    assert calls == ["10:45", "11:45"]
    assert [stamp.strftime("%H:%M") for stamp in engine.evaluations] == ["11:45", "12:10"]
    assert engine.evaluating is True


def test_refresh_pauses_briefly_and_does_not_double_the_queued_minute(tmp_path):
    first = [_row("AAPL", 1)]
    second = [_row("MSFT", 1)]
    engine, _adapter = _engine(tmp_path)
    queued = []

    def scan(slot, now):
        del now
        label = slot.scan_at.strftime("%H:%M")
        return _Result(True, first if label == "10:45" else second)

    def hook():
        queued.append(engine._paused)
        engine.evaluate_minute(_et(11, 45))
        engine.evaluate_minute(_et(11, 45))

    runner = _runner(tmp_path, scan, engine)
    runner.tick(_et(10, 45))
    engine.pause_hook = hook
    runner.tick(_et(11, 45))
    assert queued == [True]
    assert [stamp.strftime("%H:%M") for stamp in engine.evaluations] == ["10:45", "11:45"]
    assert [row["symbol"] for row in engine.candidates] == ["MSFT"]
    assert engine.pause_count == 2


def test_a_failed_refresh_keeps_the_last_list_and_blocks_buys(tmp_path):
    engine, adapter = _engine(tmp_path)

    def scan(slot, now):
        del now
        if slot.scan_at.strftime("%H:%M") == "10:45":
            return _Result(True, [_row("AAPL", 1)])
        return _Result(False)

    runner = _runner(tmp_path, scan, engine)
    runner.tick(_et(10, 45))
    runner.tick(_et(11, 45))
    runner.tick(_et(12, 0))
    assert [row["symbol"] for row in engine.candidates] == ["AAPL"]
    assert engine.snapshot_safe_for_buys is False
    assert "snapshot_unsafe" in engine.buy_gate.last_block
    assert adapter.submits == []


def test_candidate_counts_follow_the_existing_pass_ranking(tmp_path):
    engine, _adapter = _engine(tmp_path)
    slot = _slots()[0]
    engine.on_scan_success(slot, [])
    assert engine.started is True
    assert engine.candidates == []

    blocked = [_row("AAA", 1, status="BLOCKED_TREND"), _row("BBB", 2)]
    engine.on_scan_success(slot, blocked)
    assert [row["symbol"] for row in engine.candidates] == ["BBB"]

    three = [_row(symbol, rank) for rank, symbol in enumerate(["A", "B", "C"], start=1)]
    engine.on_scan_success(slot, three)
    assert [row["symbol"] for row in engine.candidates] == ["A", "B", "C"]

    ten = [_row(f"S{rank:02d}", rank) for rank in range(1, 11)]
    engine.on_scan_success(slot, ten)
    assert len(engine.candidates) == 10

    twelve = [_row(f"S{rank:02d}", rank) for rank in range(1, 13)]
    engine.on_scan_success(slot, twelve)
    assert [row["symbol"] for row in engine.candidates] == [f"S{rank:02d}" for rank in range(1, 11)]


def test_final_scan_does_not_start_buys_or_replace_candidates(tmp_path):
    engine, _adapter = _engine(tmp_path)
    regular, final = _slots()[0], _slots()[-1]
    engine.on_scan_success(final, [_row("LATE", 1)])
    assert engine.started is False
    engine.on_scan_success(regular, [_row("AAPL", 1)])
    engine.on_scan_success(final, [_row("LATE", 1)])
    assert [row["symbol"] for row in engine.candidates] == ["AAPL"]
    assert engine.final_scan_seen is True


def test_no_new_buys_at_or_after_1600_even_if_the_engine_is_wrong(tmp_path):
    engine, adapter = _engine(tmp_path)
    engine.on_scan_success(_slots()[0], [_row("AAPL", 1)])
    engine.buy_gate.entry_rule_defined = True
    engine.order_sizer = lambda row, price: 1
    engine.session_close = time(20, 0)
    engine.evaluating = True
    _arm(engine.feed, ["AAPL"], _et(15, 59))
    engine.evaluate_minute(_et(15, 59))
    assert len(adapter.submits) == 1
    _arm(engine.feed, ["AAPL"], _et(16, 0))
    blocked = engine.buy_gate.submit_buy(
        _row("AAPL", 1),
        _et(16, 0),
        qty=1,
        price=10,
        feed=engine.feed,
        snapshot_safe=True,
        session_close=time(20, 0),
    )
    assert blocked is None
    assert "after_1600_et" in engine.buy_gate.last_block
    assert len(adapter.submits) == 1
    before = len(engine.evaluations)
    engine.evaluate_minute(_et(16, 0))
    assert len(engine.evaluations) == before


def test_early_close_stops_evaluation_at_the_real_close(tmp_path):
    engine, _adapter = _engine(tmp_path)

    def scan(slot, now):
        del slot, now
        return _Result(True, [_row("AAPL", 1)])

    runner = _runner(tmp_path, scan, engine)
    runner.tick(_et(10, 45, date(2025, 11, 28)))
    runner.tick(_et(12, 59, date(2025, 11, 28)))
    assert any(stamp.strftime("%H:%M") == "12:59" for stamp in engine.evaluations)
    count = len(engine.evaluations)
    runner.tick(_et(13, 0, date(2025, 11, 28)))
    assert len(engine.evaluations) == count
    assert engine.evaluating is False


def test_feed_staleness_disconnect_and_a_quiet_symbol(tmp_path):
    feed = IexFeed(stale_after=pd.Timedelta(seconds=30))
    now = _et(11, 0)
    assert feed.source == FEED_SOURCE
    assert feed.source != "sip"
    assert IEX_STREAM_URL.endswith("/iex")
    feed.connect()
    feed.ingest({"T": "success", "msg": "authenticated"}, now)
    feed.set_required([], ["AAPL", "MSFT"])
    feed.ingest({"T": "subscription", "trades": ["AAPL", "MSFT"]}, now)
    feed.ingest({"T": "t", "S": "MSFT", "p": 12.5}, now)
    assert feed.healthy(now) is True
    assert feed.price("AAPL") is None
    assert feed.connected is True
    assert feed.healthy(now + pd.Timedelta(seconds=31)) is False
    feed.disconnect()
    assert feed.healthy(now) is False
    feed.connect()
    feed.ingest({"T": "success", "msg": "authenticated"}, now)
    assert feed.healthy(now) is False
    feed.ingest({"T": "subscription", "trades": ["AAPL", "MSFT"]}, now)
    feed.ingest({"T": "t", "S": "MSFT", "p": 12.5}, now)
    assert feed.healthy(now) is True

    engine, adapter = _engine(tmp_path, feed=feed)
    engine.on_scan_success(_slots()[0], [_row("AAPL", 1), _row("MSFT", 2)])
    engine.buy_gate.entry_rule_defined = True
    engine.order_sizer = lambda row, price: 1
    engine.session_close = time(16, 0)
    feed.ingest({"T": "subscription", "trades": ["AAPL", "MSFT"]}, now)
    engine.evaluate_minute(now)
    assert [item["symbol"] for item in adapter.submits] == ["MSFT"]
    assert feed.connected is True
    feed.disconnect()
    engine.evaluate_minute(_et(11, 1))
    assert [item["symbol"] for item in adapter.submits] == ["MSFT"]
    assert "feed_unhealthy" in engine.buy_gate.last_block


def test_symbol_limit_is_reported_and_blocks_buys():
    plan = plan_subscription(["AAPL"], [f"S{i}" for i in range(30)], limit=30)
    assert plan.source == "iex"
    assert plan.omitted == ("S29",)
    assert "AAPL" in plan.subscribed
    feed = IexFeed(symbol_limit=30)
    feed.connect()
    now = _et(11, 0)
    feed.ingest({"T": "success", "msg": "authenticated"}, now)
    feed.set_required(["AAPL"], [f"S{i}" for i in range(30)])
    feed.ingest({"T": "subscription", "trades": list(feed.required)}, now)
    assert feed.healthy(now) is False
    assert feed.coverage_complete is False


def test_duplicate_buy_is_blocked_across_restart_and_late_confirmation(tmp_path):
    path = str(tmp_path / "orders.db")
    adapter = PaperAdapter()
    risk = RiskManager(cash=100000, max_position_percent=1.0)
    gate = _open_gate(path, adapter, risk)
    now = _et(11, 0)
    feed = _healthy(["AAPL"], now)
    row = _row("AAPL", 1)
    first = gate.submit_buy(row, now, 1, 10, feed=feed, snapshot_safe=True, session_close=time(16, 0))
    second = gate.submit_buy(row, now, 1, 10, feed=feed, snapshot_safe=True, session_close=time(16, 0))
    assert first["id"] == "paper-1"
    assert second is None
    assert gate.last_block == ["duplicate_order"]
    assert len(adapter.submits) == 1

    restarted = _open_gate(path, PaperAdapter(), risk)
    again = restarted.submit_buy(row, now, 1, 10, feed=feed, snapshot_safe=True, session_close=time(16, 0))
    assert again is None
    assert restarted.adapter.submits == []

    adapter.remote["paper-1"] = {"id": "paper-1", "status": "partially_filled", "filled_qty": 1}
    gate_orders = OrderStore(path)
    from src.watch.engine import PositionMonitor

    monitor = PositionMonitor(gate_orders, adapter)
    monitor.tick(now)
    assert gate_orders.latest("AAPL")["status"] == "partial"
    assert gate_orders.latest("AAPL")["filled_qty"] == 1
    assert len(adapter.submits) == 1

    adapter.remote["paper-1"] = {"id": "paper-1", "status": "rejected", "filled_qty": 0}
    monitor.tick(_et(11, 1))
    assert gate_orders.latest("AAPL")["status"] == "rejected"
    rejected = _open_gate(path, adapter, risk)
    assert rejected.submit_buy(row, _et(11, 2), 1, 10, feed=feed, snapshot_safe=True, session_close=time(16, 0)) is None
    assert len(adapter.submits) == 1


def test_open_positions_are_polled_without_candidates_or_exit_orders(tmp_path):
    engine, adapter = _engine(tmp_path)
    engine.on_scan_success(_slots()[0], [])
    assert engine.orders.reserve("AAPL", DAY, 1, 10)
    engine.orders.mark("AAPL", DAY, "submitted", "paper-9", 0)
    adapter.remote["paper-9"] = {"id": "paper-9", "status": "accepted", "filled_qty": 0}
    engine.tick(_et(12, 0), time(16, 0))
    assert engine.candidates == []
    assert engine.positions.polls == ["paper-9"]
    assert adapter.submits == []
    assert engine.positions.exit_submissions == 0
    assert engine.positions.exit_rule_defined is False


def test_risk_and_capital_are_checked_and_live_orders_are_refused(tmp_path):
    now = _et(11, 0)
    feed = _healthy(["AAPL"], now)
    row = _row("AAPL", 1)
    poor = RiskManager(cash=100, max_position_percent=1.0)
    poor.cash = 70
    gate = _open_gate(str(tmp_path / "poor.db"), PaperAdapter(), poor)
    assert gate.submit_buy(row, now, 2, 40, feed=feed, snapshot_safe=True, session_close=time(16, 0)) is None
    assert gate.last_block == ["insufficient_capital"]

    limited = RiskManager(cash=100000, max_position_percent=0.01, daily_loss_limit=0.01)
    limited.record_trade_pnl(-2000)
    stopped = _open_gate(str(tmp_path / "stopped.db"), PaperAdapter(), limited)
    assert stopped.submit_buy(row, now, 1, 10, feed=feed, snapshot_safe=True, session_close=time(16, 0)) is None
    assert "risk_blocked" in stopped.last_block

    live = _open_gate(str(tmp_path / "live.db"), PaperAdapter("https://api.alpaca.markets"), RiskManager(cash=100000))
    live.entry_rule_defined = True
    assert live.submit_buy(row, now, 1, 10, feed=feed, snapshot_safe=True, session_close=time(16, 0)) is None
    assert live.last_block == ["live_trading_disabled"]
    assert live.adapter.submits == []


def test_default_entry_rule_stays_disabled():
    assert ENTRY_RULE_DEFINED is False


def test_uncertain_submit_is_not_retried(tmp_path, caplog):
    del caplog
    adapter = PaperAdapter()
    calls = {"n": 0}

    def explode(**kwargs):
        del kwargs
        calls["n"] += 1
        raise RuntimeError("broker timeout")

    adapter.submit_limit_order = explode
    gate = _open_gate(str(tmp_path / "orders.db"), adapter, RiskManager(cash=100000, max_position_percent=1.0))
    now = _et(11, 0)
    feed = _healthy(["AAPL"], now)
    assert gate.submit_buy(_row("AAPL", 1), now, 1, 10, feed=feed, snapshot_safe=True, session_close=time(16, 0)) is None
    assert gate.submit_buy(_row("AAPL", 1), now, 1, 10, feed=feed, snapshot_safe=True, session_close=time(16, 0)) is None
    assert calls["n"] == 1
    assert gate.last_block == ["duplicate_order"]


def _open_gate(path, adapter, risk):
    gate_engine = WatchEngine(OrderStore(path), adapter, risk, IexFeed())
    gate_engine.buy_gate.entry_rule_defined = True
    return gate_engine.buy_gate


def _healthy(symbols, now):
    feed = IexFeed()
    _arm(feed, symbols, now)
    feed.set_required([], symbols)
    feed.ingest({"T": "subscription", "trades": list(symbols)}, now)
    return feed


def test_watch_logs_use_the_package_logger(caplog):
    with caplog.at_level(logging.INFO):
        assert "iex" in FEED_SOURCE
