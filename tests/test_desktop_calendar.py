from datetime import date
import threading

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.session_download import DownloadResult, calendar_session_dates, session_month
from src.desktop.calendar_panel import (
    PARTIAL_BG,
    STORED_BG,
    SessionCalendarPanel,
    completion_percent,
    session_activity_line,
)

TODAY = date(2026, 10, 16)


def test_calendar_downloads_only_the_selected_market_days(tmp_path):
    tk = pytest.importorskip("tkinter")
    store = BarStore(str(tmp_path / "bars.db"))
    store.upsert_30m(
        "AAPL",
        _bar(pd.Timestamp("2026-10-14 13:30", tz="UTC"), 8.0),
        pd.Timestamp("2026-10-14 20:00", tz="UTC"),
    )
    requested = []

    def load_month(year, month, today):
        return session_month(year, month, store, today)

    def download(dates, on_progress=None):
        requested.append(list(dates))
        if on_progress is not None:
            on_progress(len(dates), len(dates))
        return DownloadResult(bars=2, failed_symbols=())

    notes = []
    root = tk.Tk()
    root.withdraw()
    panel = SessionCalendarPanel(root, today=TODAY, load_month=load_month, download=download, on_log=notes.append)
    try:
        assert panel.title_var.get() == "Οκτώβριος 2026"
        assert str(panel.day_buttons[date(2026, 10, 17)].cget("state")) == "disabled"
        assert str(panel.day_buttons[date(2026, 10, 19)].cget("state")) == "disabled"
        assert "•" in panel.day_buttons[date(2026, 10, 14)].cget("text")
        assert panel.day_buttons[date(2026, 10, 14)].cget("bg") == PARTIAL_BG
        panel.toggle(date(2026, 10, 16))
        panel.toggle(date(2026, 10, 14))
        panel.toggle(date(2026, 10, 17))
        assert panel.download_button.cget("text") == "Λήψη (2)"
        panel.start_download()
        panel._worker.join(2)
        panel._poll()
        assert requested == [[date(2026, 10, 14), date(2026, 10, 16)]]
        assert notes == [session_activity_line("download", [date(2026, 10, 14), date(2026, 10, 16)])]
        assert panel.status_var.get() == "Αποθηκεύτηκαν 2 bars."
    finally:
        root.destroy()


def test_complete_day_is_green_and_partial_day_is_amber():
    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    root.withdraw()
    panel = SessionCalendarPanel(
        root,
        today=TODAY,
        load_month=lambda year, month, today: {
            "days": [
                {"date": "2026-10-14", "market": True, "close": "16:00", "stored_symbols": 2, "complete": True, "partial": False, "selectable": True},
                {"date": "2026-10-16", "market": True, "close": "16:00", "stored_symbols": 2, "complete": False, "partial": True, "selectable": True},
            ],
            "symbols": 2,
        },
        download=lambda dates, on_progress=None: None,
    )
    try:
        assert panel.day_buttons[date(2026, 10, 14)].cget("bg") == STORED_BG
        assert panel.day_buttons[date(2026, 10, 16)].cget("bg") == PARTIAL_BG
        assert "1 συνεδρίες πλήρεις" in panel.status_var.get()
        assert "1 με ελλιπή δεδομένα" in panel.status_var.get()
    finally:
        root.destroy()


def test_calendar_deletes_only_the_selected_days():
    tk = pytest.importorskip("tkinter")
    removed = []

    def delete(dates):
        removed.append(list(dates))
        return 1

    notes = []
    root = tk.Tk()
    root.withdraw()
    panel = SessionCalendarPanel(
        root,
        today=TODAY,
        load_month=lambda year, month, today: {"days": _days(), "symbols": 1},
        download=lambda dates, on_progress=None: None,
        delete=delete,
        on_log=notes.append,
    )
    try:
        assert str(panel.delete_button.cget("state")) == "disabled"
        assert panel.download_button.grid_info()["row"] == panel.delete_button.grid_info()["row"] == 0
        assert panel.download_button.grid_info()["column"] == 0
        assert panel.delete_button.grid_info()["column"] == 1
        seen = []
        panel.delete_button.set_progress = lambda percent, _fill=panel.delete_button.set_progress: seen.append(percent) or _fill(percent)
        panel.toggle(date(2026, 10, 14))
        panel.toggle(date(2026, 10, 16))
        panel.start_delete()
        panel._worker.join(2)
        panel._poll()
        assert removed == [[date(2026, 10, 14)], [date(2026, 10, 16)]]
        assert [value for value in seen if value] == [completion_percent(1, 2), completion_percent(2, 2)]
        assert panel.status_var.get() == "Διαγράφηκαν 2 bars."
        assert notes == [session_activity_line("delete", [date(2026, 10, 14), date(2026, 10, 16)])]
        assert panel.selected == set()
    finally:
        root.destroy()


def test_pause_and_close_stop_the_selected_day_download():
    tk = pytest.importorskip("tkinter")
    from src.desktop.app import TradingBotApp

    entered = threading.Event()

    def download(dates, on_progress=None, should_stop=None, wait_if_paused=None):
        entered.set()
        while not should_stop():
            wait_if_paused()
        return DownloadResult(bars=1, failed_symbols=(), stopped=True)

    root = tk.Tk()
    root.withdraw()
    app = TradingBotApp(
        root,
        calendar_today=TODAY,
        load_month=lambda year, month, today: {"days": _days(), "symbols": 1},
        download_sessions=download,
    )
    app.calendar.toggle(date(2026, 10, 14))
    app.calendar.start_download()
    assert entered.wait(2)
    assert str(app.pause_button.cget("state")) == "normal"
    assert str(app.stop_button.cget("state")) == "normal"
    app.pause_universe()
    assert app._pause_download.is_set()
    app.close()
    assert not app.calendar._worker.is_alive()


def test_sixty_day_button_downloads_calendar_sessions_without_scanning():
    tk = pytest.importorskip("tkinter")
    from src.desktop.app import TradingBotApp

    seen = {}

    def download(dates, on_progress=None, should_stop=None, wait_if_paused=None):
        seen["dates"] = list(dates)
        if on_progress is not None:
            on_progress(len(dates), len(dates))
        return DownloadResult(bars=4, failed_symbols=())

    def factory(*args, **kwargs):
        raise AssertionError("universe scan")

    root = tk.Tk()
    root.withdraw()
    app = TradingBotApp(
        root,
        service_factory=factory,
        calendar_today=TODAY,
        load_month=lambda year, month, today: {"days": [], "symbols": 0},
        download_sixty=download,
    )
    try:
        def finish():
            if app.worker is not None and app.worker.is_alive():
                app.root.after(20, finish)
                return
            app.root.after(20, app.root.quit)

        app.download_sixty_days()
        app.root.after(20, finish)
        app.root.after(3000, app.root.quit)
        app.root.mainloop()
        assert seen["dates"] == calendar_session_dates(TODAY, 60)
        assert seen["dates"][-1] == TODAY
        text = app.activity.get("1.0", "end")
        assert session_activity_line("download", seen["dates"]) in text
        assert "αποθηκεύτηκαν 4 bars" in text
        assert app.service is None
    finally:
        app.close()


def test_sixty_day_buttons_sit_under_the_session_actions_and_scale():
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk

    from src.desktop.app import SURFACE, TradingBotApp

    root = tk.Tk()
    root.withdraw()
    app = TradingBotApp(root, load_month=lambda year, month, today: {"days": [], "symbols": 0})
    try:
        assert app.root.cget("background") == SURFACE
        assert app.calendar.grid_info()["row"] == 0
        buttons = (
            app.calendar.download_button,
            app.calendar.delete_button,
            app.start_button,
            app.sixty_delete_button,
        )
        assert app.start_button is app.calendar.sixty_download_button
        assert [button.master for button in buttons] == [app.calendar.actions] * 4
        assert [button.grid_info()["row"] for button in buttons] == [0, 0, 1, 1]
        assert [button.grid_info()["column"] for button in buttons] == [0, 1, 0, 1]
        assert all("ew" in button.grid_info()["sticky"] for button in buttons)
        assert app.start_button.cget("text") == "Λήψη 60 Ημερών"
        assert app.sixty_delete_button.cget("text") == "Διαγραφή 60 Ημερών"
        assert app.pause_button.master is app.calendar.controls
        assert app.stop_button.master is app.calendar.controls
        assert app.calendar.controls.master is app.calendar.actions
        assert app.calendar.controls.grid_info()["row"] == 2
        assert app.pause_button.icon == "pause"
        assert app.stop_button.icon == "stop"
        universe = app.activity.master.master
        assert universe.cget("text") == "Universe"
        assert not any(isinstance(child, ttk.Progressbar) for child in universe.winfo_children())
        root.update_idletasks()
        assert app.stop_button.winfo_reqwidth() == app.stop_button.winfo_reqheight()
        assert app.pause_button.winfo_reqwidth() == app.pause_button.winfo_reqheight()
        app._layout_base = (400, 400)
        app._icon_size = 28
        app.pause_button.resize(28)
        app.stop_button.resize(28)

        class _Resize:
            def __init__(self, widget, width, height):
                self.widget = widget
                self.width = width
                self.height = height

        app._on_resize(_Resize(app.root, 800, 800))
        assert app.pause_button._size == 56
        assert app.stop_button._size == 56
        assert app.calendar._font_size == 20
        assert app.calendar.download_button._font_size == 20
    finally:
        root.destroy()


def test_sixty_day_delete_removes_the_same_sessions_as_the_download():
    tk = pytest.importorskip("tkinter")
    from src.desktop.app import TradingBotApp

    seen = {}

    def delete(dates):
        seen.setdefault("calls", []).append(list(dates))
        return 1

    root = tk.Tk()
    root.withdraw()
    app = TradingBotApp(
        root,
        calendar_today=TODAY,
        load_month=lambda year, month, today: {"days": [], "symbols": 0},
        delete_sixty=delete,
    )
    try:
        def finish():
            if app.worker is not None and app.worker.is_alive():
                app.root.after(20, finish)
                return
            app.root.after(20, app.root.quit)

        app.delete_sixty_days()
        app.root.after(20, finish)
        app.root.after(3000, app.root.quit)
        app.root.mainloop()
        dates = calendar_session_dates(TODAY, 60)
        assert seen["calls"] == [[day] for day in dates]
        text = app.activity.get("1.0", "end")
        assert session_activity_line("delete", dates) in text
        assert f"διαγράφηκαν {len(dates)} bars" in text
        assert str(app.pause_button.cget("state")) == "disabled"
        assert str(app.sixty_delete_button.cget("state")) == "normal"
    finally:
        app.close()


def test_scheduler_box_starts_the_clock_and_logs_the_next_scan():
    tk = pytest.importorskip("tkinter")
    from src.data.scan_scheduler import next_scan_message
    from src.desktop.app import TradingBotApp

    started = threading.Event()
    release = threading.Event()

    def scheduler(**kwargs):
        kwargs["log"](next_scan_message("16:15:00", "09:30:00-10:00:00"))
        started.set()
        while not kwargs["should_stop"]() and not release.is_set():
            release.wait(0.05)

    root = tk.Tk()
    root.withdraw()
    app = TradingBotApp(
        root,
        load_month=lambda year, month, today: {"days": [], "symbols": 0},
        scheduler=scheduler,
        scheduler_symbols=lambda: ["AAPL"],
    )
    try:
        assert app.scheduler_box.cget("text") == "Scheduler"
        assert app.scheduler_button.cget("text") == "Εκκίνηση scheduler"
        app.start_scheduler()

        def see_next_scan():
            text = app.activity.get("1.0", "end")
            if "επόμενο scan 16:15:00 για την περίοδο [09:30:00-10:00:00]" in text:
                assert str(app.scheduler_button.cget("state")) == "disabled"
                app.stop_universe()
                release.set()
                app.root.after(20, wait_until_stopped)
                return
            app.root.after(20, see_next_scan)

        def wait_until_stopped():
            if app.worker is not None and app.worker.is_alive():
                app.root.after(20, wait_until_stopped)
                return
            app.root.after(20, app.root.quit)

        app.root.after(20, see_next_scan)
        app.root.after(3000, app.root.quit)
        app.root.mainloop()
        assert "επόμενο scan 16:15:00 για την περίοδο [09:30:00-10:00:00]" in app.activity.get("1.0", "end")
        assert app.activity.get("1.0", "end").count("ο scheduler σταμάτησε") == 1
        assert str(app.scheduler_button.cget("state")) == "normal"
    finally:
        release.set()
        app.close()


def _days():
    return [
        {"date": "2026-10-14", "market": True, "close": "16:00", "stored_symbols": 1, "selectable": True},
        {"date": "2026-10-16", "market": True, "close": "16:00", "stored_symbols": 1, "selectable": True},
    ]


def _bar(stamp, close):
    return pd.DataFrame(
        {"open": [close], "high": [close], "low": [close], "close": [close], "volume": [5.0]},
        index=pd.DatetimeIndex([stamp]),
    )
