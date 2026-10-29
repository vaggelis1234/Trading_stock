"""Drive every desktop button through download, delete, pause, stop, and repeat."""

from datetime import date, timedelta
import threading
import time
import tkinter as tk

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.provider import BarsBatch
from src.data.session_download import (
    DownloadResult,
    calendar_session_dates,
    delete_sessions,
    download_sessions,
    session_month,
)
from src.desktop.calendar_panel import session_activity_line

TODAY = date(2026, 10, 16)
CLOCK = pd.Timestamp("2026-10-16 21:00", tz="UTC")
SYMBOLS = ["AAPL", "MSFT"]
DAY_A = date(2026, 10, 14)
DAY_B = date(2026, 10, 16)


class _Provider:
    def __init__(self):
        self.calls = []

    def get_bars_many(self, symbols, start, end, interval):
        self.calls.append(interval)
        start_ts = pd.Timestamp(start)
        if interval == "1d":
            index = pd.DatetimeIndex([start_ts])
        else:
            index = pd.DatetimeIndex([start_ts + pd.Timedelta(minutes=30 * step) for step in range(13)])
        frame = pd.DataFrame(
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0},
            index=index,
        )
        return BarsBatch({symbol: frame for symbol in symbols}, ())


def test_every_button_downloads_deletes_pauses_stops_and_repeats(tmp_path):
    tk = pytest.importorskip("tkinter")
    from src.desktop.app import TradingBotApp

    store = BarStore(str(tmp_path / "bars.db"))
    provider = _Provider()
    gate = threading.Event()
    gate.set()
    seen = {"download": [], "delete": [], "sixty_download": [], "sixty_delete": [], "web": 0}

    def download(dates, on_progress=None, should_stop=None, wait_if_paused=None):
        seen["download"].append(list(dates))
        return _gated(
            dates,
            provider,
            store,
            gate,
            on_progress=on_progress,
            should_stop=should_stop,
            wait_if_paused=wait_if_paused,
        )

    def download_sixty(dates, on_progress=None, should_stop=None, wait_if_paused=None):
        seen["sixty_download"].append(list(dates))
        return _gated(
            dates,
            provider,
            store,
            gate,
            on_progress=on_progress,
            should_stop=should_stop,
            wait_if_paused=wait_if_paused,
        )

    def delete_selected(dates):
        seen["delete"].append(list(dates))
        return delete_sessions(list(dates), store=store)

    def delete_sixty(dates):
        seen["sixty_delete"].append(list(dates))
        return delete_sessions(list(dates), store=store)

    root = tk.Tk()
    root.withdraw()
    app = TradingBotApp(
        root,
        calendar_today=TODAY,
        load_month=lambda year, month, today: session_month(year, month, store, today),
        download_sessions=download,
        download_sixty=download_sixty,
        delete_sixty=delete_sixty,
        web_opener=lambda: seen.__setitem__("web", seen["web"] + 1) or "opened",
    )
    app.calendar._delete = delete_selected
    try:
        _assert_idle(app)
        _press(app.calendar.download_button)
        _press(app.calendar.delete_button)
        assert seen["download"] == []
        assert seen["delete"] == []

        app.calendar.toggle(date(2026, 10, 17))
        app.calendar.toggle(date(2026, 10, 18))
        assert app.calendar.selected == set()
        app.calendar.toggle(DAY_A)
        app.calendar.toggle(DAY_B)
        assert app.calendar.download_button.cget("text") == "Λήψη (2)"
        assert app.calendar.download_button.cget("state") == "normal"
        assert app.calendar.delete_button.cget("state") == "normal"

        gate.clear()
        _press(app.calendar.download_button)
        _wait_until(lambda: app._mode == "sessions" and str(app.pause_button.cget("state")) == "normal")
        assert app.calendar.delete_button.cget("state") == "disabled"
        assert app.start_button.cget("state") == "disabled"
        assert app.sixty_delete_button.cget("state") == "disabled"
        assert str(app.stop_button.cget("state")) == "normal"
        _press(app.calendar.delete_button)
        _press(app.start_button)
        _press(app.sixty_delete_button)
        assert seen["delete"] == []
        assert seen["sixty_download"] == []
        month = app.calendar.title_var.get()
        app.calendar.shift(-1)
        assert app.calendar.title_var.get() == month
        _press(app.pause_button)
        assert "η λήψη σε παύση" in _log(app)
        _press(app.pause_button)
        assert "η λήψη συνεχίζει" in _log(app)
        _press(app.stop_button)
        _settle(app)
        assert "η λήψη σταμάτησε" in _log(app)
        assert session_activity_line("download", [DAY_A, DAY_B]) in _log(app)
        assert _bars(store, DAY_A) == 0
        gate.set()

        _press(app.calendar.download_button)
        _settle(app)
        assert _bars(store, DAY_A) == 13
        assert _bars(store, DAY_B) == 13
        assert "Αποθηκεύτηκαν" in app.calendar.status_var.get()
        assert app.calendar.selected == set()
        calls_after_first = len(provider.calls)

        app.calendar.toggle(DAY_A)
        app.calendar.toggle(DAY_B)
        _press(app.calendar.download_button)
        _settle(app)
        assert len(provider.calls) == calls_after_first
        assert app.calendar.status_var.get() == "Οι μέρες είναι ήδη στη βάση."
        assert _log(app).count(session_activity_line("download", [DAY_A, DAY_B])) == 3

        app.calendar.toggle(DAY_A)
        _press(app.calendar.delete_button)
        _settle(app)
        assert _bars(store, DAY_A) == 0
        assert _bars(store, DAY_B) == 13
        assert "Διαγράφηκαν" in app.calendar.status_var.get()
        assert session_activity_line("delete", [DAY_A]) in _log(app)

        app.calendar.toggle(DAY_A)
        _press(app.calendar.download_button)
        _settle(app)
        assert _bars(store, DAY_A) == 13
        assert len(provider.calls) > calls_after_first

        before_sixty = len(provider.calls)
        _press(app.start_button)
        _settle(app)
        dates = calendar_session_dates(TODAY, 60)
        assert seen["sixty_download"][-1] == dates
        assert session_activity_line("download", dates) in _log(app)
        assert len(provider.calls) > before_sixty
        filled = len(provider.calls)

        _press(app.start_button)
        _settle(app)
        assert len(provider.calls) == filled
        assert "οι μέρες είναι ήδη στη βάση" in _log(app)

        gate.clear()
        _press(app.start_button)
        _wait_until(lambda: app._mode == "sixty" and str(app.pause_button.cget("state")) == "normal")
        _press(app.pause_button)
        _press(app.stop_button)
        _settle(app)
        assert _log(app).count("η λήψη σταμάτησε") >= 2
        gate.set()
        assert len(provider.calls) == filled

        _press(app.sixty_delete_button)
        _settle(app)
        assert seen["sixty_delete"]
        assert _bars(store, DAY_A) == 0
        assert _bars(store, DAY_B) == 0
        assert session_activity_line("delete", dates) in _log(app)
        assert str(app.pause_button.cget("state")) == "disabled"
        assert "διαγράφηκαν" in _log(app)

        _press(app.sixty_delete_button)
        _settle(app)
        assert _log(app).count(session_activity_line("delete", dates)) == 2

        _press(app.start_button)
        _settle(app)
        assert len(provider.calls) > filled
        assert _bars(store, DAY_B) == 13

        _press(app.calendar.prev_button)
        assert app.calendar.month == 9
        _press(app.calendar.next_button)
        assert app.calendar.month == 10

        _press(_find_button(app.root, "HELP"))
        assert app.help_window is not None
        app.help_window.destroy()
        _press(_find_button(app.root, "STATUS"))
        assert app.status_window is not None
        app.status_window.destroy()
        _press(_find_button(app.root, "WEB UI"))
        assert seen["web"] == 1
        _assert_idle(app)
    finally:
        gate.set()
        app.close()


def _gated(dates, provider, store, gate, *, on_progress, should_stop, wait_if_paused):
    while not gate.is_set():
        if wait_if_paused is not None:
            wait_if_paused()
        if should_stop is not None and should_stop():
            return DownloadResult(bars=0, failed_symbols=(), stopped=True)
        time.sleep(0.01)
    return download_sessions(
        dates,
        provider=provider,
        store=store,
        symbols=SYMBOLS,
        clock=CLOCK,
        on_progress=on_progress,
        should_stop=should_stop,
        wait_if_paused=wait_if_paused,
    )


def _press(button) -> None:
    if str(button.cget("state")) == "disabled":
        return
    invoke = getattr(button, "invoke", None)
    if invoke is not None:
        invoke()
        return
    button._click(None)


def _log(app) -> str:
    return app.activity.get("1.0", "end")


def _bars(store, day) -> int:
    frame = store.load_window("AAPL", "30m", day.isoformat(), (day + timedelta(days=1)).isoformat())
    return len(frame)


def _settle(app, seconds: float = 20) -> None:
    jobs = {}

    def finish():
        for job in jobs.values():
            try:
                app.root.after_cancel(job)
            except tk.TclError:
                pass
        jobs.clear()
        app.root.quit()

    def check():
        if app.calendar._running() or not app.calendar._events.empty():
            app.calendar._poll()
        if not _busy(app) and app.calendar._events.empty():
            finish()
            return
        jobs["check"] = app.root.after(30, check)

    jobs["check"] = app.root.after(30, check)
    jobs["timeout"] = app.root.after(int(seconds * 1000), finish)
    app.root.mainloop()
    if _busy(app) or not app.calendar._events.empty():
        raise AssertionError(_log(app))


def _wait_until(ready, seconds: float = 3) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if ready():
            return
        time.sleep(0.01)
    raise AssertionError("the button did not reach the expected state")


def _busy(app) -> bool:
    worker = app.worker
    return (worker is not None and worker.is_alive()) or app.calendar._running()


def _assert_idle(app) -> None:
    assert not _busy(app)
    assert str(app.pause_button.cget("state")) == "disabled"
    assert str(app.stop_button.cget("state")) == "disabled"
    assert app.calendar.download_button.cget("state") == "disabled"
    assert app.calendar.delete_button.cget("state") == "disabled"
    assert app.start_button.cget("state") == "normal"
    assert app.sixty_delete_button.cget("state") == "normal"


def _find_button(widget, text: str):
    if widget.winfo_class() == "TButton" and widget.cget("text") == text:
        return widget
    for child in widget.winfo_children():
        found = _find_button(child, text)
        if found is not None:
            return found
    return None
